"""Schema definition and the migration runner.

This module is the SINGLE SOURCE OF TRUTH for the Nexus database schema.

It deliberately imports nothing outside the standard library, so it can be used
by both the async bot (via core.database, on aiosqlite) and the one-time
migration script (via plain sqlite3) without dragging in dependencies.

To evolve the schema in a later phase: append a new (version, name, statements)
tuple to MIGRATIONS. Never edit a migration that has already shipped.
"""

import sqlite3
import time

# ============================================================
#  V1 -- FOUNDATION
# ============================================================
_V1 = (
    # ---------- THE CENTRAL PLAYER TABLE ----------
    # Every future system hangs off players.user_id. Nothing reinvents storage.
    """
    CREATE TABLE IF NOT EXISTS players (
        user_id           INTEGER PRIMARY KEY,
        balance           INTEGER NOT NULL DEFAULT 0,
        xp                INTEGER NOT NULL DEFAULT 0,
        level             INTEGER NOT NULL DEFAULT 1,
        last_daily        INTEGER NOT NULL DEFAULT 0,

        class_id          TEXT,
        clan_id           INTEGER,
        pet_id            INTEGER,
        battle_pass_tier  INTEGER NOT NULL DEFAULT 0,
        battle_pass_xp    INTEGER NOT NULL DEFAULT 0,
        prestige          INTEGER NOT NULL DEFAULT 0,

        created_at        INTEGER NOT NULL,
        updated_at        INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_players_balance ON players(balance DESC)",
    "CREATE INDEX IF NOT EXISTS idx_players_level ON players(level DESC)",

    # ---------- GENERIC PER-PLAYER KEY/VALUE ----------
    # Escape hatch: a new system can store state on day one with no migration,
    # then graduate to a real table once its shape settles.
    """
    CREATE TABLE IF NOT EXISTS player_data (
        user_id   INTEGER NOT NULL REFERENCES players(user_id) ON DELETE CASCADE,
        namespace TEXT    NOT NULL,
        key       TEXT    NOT NULL,
        value     TEXT    NOT NULL,
        PRIMARY KEY (user_id, namespace, key)
    ) WITHOUT ROWID
    """,

    # ---------- ITEMS ----------
    """
    CREATE TABLE IF NOT EXISTS shop_items (
        code         TEXT PRIMARY KEY,
        display_name TEXT NOT NULL,
        category     TEXT NOT NULL,
        price        INTEGER NOT NULL,
        role_name    TEXT,
        enabled      INTEGER NOT NULL DEFAULT 1,
        sort_order   INTEGER NOT NULL DEFAULT 0
    )
    """,

    # inventory.item_code is intentionally NOT a foreign key: future quest/event
    # drops may exist outside the shop, and migrating an old inventory must never
    # fail on an item code the shop no longer sells.
    """
    CREATE TABLE IF NOT EXISTS inventory (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL REFERENCES players(user_id) ON DELETE CASCADE,
        item_code   TEXT    NOT NULL,
        quantity    INTEGER NOT NULL DEFAULT 1,
        acquired_at INTEGER NOT NULL,
        UNIQUE(user_id, item_code)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_inventory_user ON inventory(user_id)",

    # ---------- PER-DISCORD-SERVER CONFIG ----------
    # guild_id here always means a DISCORD server. The RPG clan lives on
    # players.clan_id, deliberately named differently to avoid the collision.
    """
    CREATE TABLE IF NOT EXISTS guild_config (
        guild_id        INTEGER PRIMARY KEY,
        welcome_channel INTEGER,
        log_channel     INTEGER,
        ai_enabled      INTEGER NOT NULL DEFAULT 1,
        prefix          TEXT    NOT NULL DEFAULT '!',
        created_at      INTEGER NOT NULL,
        updated_at      INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS guild_self_roles (
        guild_id  INTEGER NOT NULL REFERENCES guild_config(guild_id) ON DELETE CASCADE,
        role_name TEXT    NOT NULL,
        PRIMARY KEY (guild_id, role_name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS guild_banned_words (
        guild_id INTEGER NOT NULL REFERENCES guild_config(guild_id) ON DELETE CASCADE,
        word     TEXT    NOT NULL,
        added_by INTEGER,
        added_at INTEGER NOT NULL,
        PRIMARY KEY (guild_id, word)
    )
    """,

    # ---------- MODERATION ----------
    # clearwarnings sets active = 0 rather than deleting, so the mod audit trail
    # survives. Reads filter on active = 1, so users see exactly what they saw before.
    """
    CREATE TABLE IF NOT EXISTS warnings (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id     INTEGER NOT NULL,
        user_id      INTEGER NOT NULL,
        moderator_id INTEGER,
        reason       TEXT    NOT NULL,
        created_at   INTEGER NOT NULL,
        active       INTEGER NOT NULL DEFAULT 1
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_warnings_lookup ON warnings(guild_id, user_id, active)",

    # ---------- REMINDERS ----------
    """
    CREATE TABLE IF NOT EXISTS reminders (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id    INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        guild_id   INTEGER,
        remind_at  INTEGER NOT NULL,
        text       TEXT    NOT NULL,
        created_at INTEGER NOT NULL,
        fired      INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(fired, remind_at)",

    # ---------- AUDIT ----------
    """
    CREATE TABLE IF NOT EXISTS economy_log (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id       INTEGER NOT NULL,
        delta         INTEGER NOT NULL,
        reason        TEXT    NOT NULL,
        related_id    INTEGER,
        balance_after INTEGER NOT NULL,
        created_at    INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_economy_log_user ON economy_log(user_id, created_at DESC)",
)

# ============================================================
#  V2 -- RPG CORE (classes, gear, dungeons)
# ============================================================
_V2 = (
    # ---------- CLASSES ----------
    # Kept as data, not code, so balance tuning is an UPDATE not a deploy.
    """
    CREATE TABLE IF NOT EXISTS classes (
        class_id          TEXT PRIMARY KEY,
        name              TEXT NOT NULL,
        description       TEXT NOT NULL,
        emoji             TEXT NOT NULL,
        base_power        INTEGER NOT NULL DEFAULT 0,
        base_thermals     INTEGER NOT NULL DEFAULT 0,
        base_clock        INTEGER NOT NULL DEFAULT 0,
        base_bandwidth    INTEGER NOT NULL DEFAULT 0,
        ram_multiplier    REAL    NOT NULL DEFAULT 1.0,
        xp_multiplier     REAL    NOT NULL DEFAULT 1.0,
        cooldown_modifier REAL    NOT NULL DEFAULT 1.0,
        unlock_level      INTEGER NOT NULL DEFAULT 1,
        enabled           INTEGER NOT NULL DEFAULT 1,
        sort_order        INTEGER NOT NULL DEFAULT 0
    )
    """,

    # ---------- GEAR ----------
    # Stats are fixed per item code rather than rolled per instance: a 5090 is
    # always a 5090. That keeps inventory's UNIQUE(user_id, item_code) valid and
    # needs no migration of existing rows.
    """
    CREATE TABLE IF NOT EXISTS gear_stats (
        item_code     TEXT PRIMARY KEY REFERENCES shop_items(code) ON DELETE CASCADE,
        slot          TEXT    NOT NULL,
        power         INTEGER NOT NULL DEFAULT 0,
        thermals      INTEGER NOT NULL DEFAULT 0,
        clock         INTEGER NOT NULL DEFAULT 0,
        bandwidth     INTEGER NOT NULL DEFAULT 0,
        rarity        TEXT    NOT NULL DEFAULT 'common',
        salvage_value INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_gear_stats_slot ON gear_stats(slot)",

    # PK (user_id, slot) enforces one item per slot with no app-level check.
    """
    CREATE TABLE IF NOT EXISTS equipment (
        user_id     INTEGER NOT NULL REFERENCES players(user_id) ON DELETE CASCADE,
        slot        TEXT    NOT NULL,
        item_code   TEXT    NOT NULL,
        equipped_at INTEGER NOT NULL,
        PRIMARY KEY (user_id, slot)
    )
    """,

    # ---------- DUNGEONS ----------
    """
    CREATE TABLE IF NOT EXISTS dungeons (
        dungeon_id       TEXT PRIMARY KEY,
        name             TEXT NOT NULL,
        description      TEXT NOT NULL,
        emoji            TEXT NOT NULL,
        min_level        INTEGER NOT NULL DEFAULT 1,
        difficulty       INTEGER NOT NULL,
        encounters       INTEGER NOT NULL DEFAULT 3,
        cooldown_seconds INTEGER NOT NULL,
        ram_reward_min   INTEGER NOT NULL,
        ram_reward_max   INTEGER NOT NULL,
        xp_reward        INTEGER NOT NULL,
        drop_chance      REAL    NOT NULL DEFAULT 0.10,
        enabled          INTEGER NOT NULL DEFAULT 1,
        sort_order       INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS dungeon_loot (
        dungeon_id TEXT    NOT NULL REFERENCES dungeons(dungeon_id) ON DELETE CASCADE,
        item_code  TEXT    NOT NULL,
        weight     INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY (dungeon_id, item_code)
    )
    """,

    # cooldown_until is STORED, not recomputed. Class switching is free, so a
    # player could otherwise clear as Data Miner for the loot bonus and then
    # switch to Sysadmin to shorten the cooldown, banking both bonuses.
    """
    CREATE TABLE IF NOT EXISTS dungeon_runs (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id          INTEGER NOT NULL REFERENCES players(user_id) ON DELETE CASCADE,
        dungeon_id       TEXT    NOT NULL,
        class_id         TEXT,
        started_at       INTEGER NOT NULL,
        cooldown_until   INTEGER NOT NULL,
        outcome          TEXT    NOT NULL,
        encounters_won   INTEGER NOT NULL,
        encounters_total INTEGER NOT NULL,
        hp_remaining     INTEGER NOT NULL,
        ram_earned       INTEGER NOT NULL,
        xp_earned        INTEGER NOT NULL,
        item_dropped     TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_dungeon_runs_user "
    "ON dungeon_runs(user_id, cooldown_until DESC)",

    # ---------- PRESTIGE ----------
    # A prestige is a deep reset: balance, xp, level, inventory and equipment
    # all go. This records what was burned, so the wipe is auditable.
    """
    CREATE TABLE IF NOT EXISTS prestige_log (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id         INTEGER NOT NULL REFERENCES players(user_id) ON DELETE CASCADE,
        new_prestige    INTEGER NOT NULL,
        level_at_reset  INTEGER NOT NULL,
        balance_burned  INTEGER NOT NULL,
        items_burned    INTEGER NOT NULL,
        created_at      INTEGER NOT NULL
    )
    """,
)


# ============================================================
#  V3 -- PVP + CLANS
# ============================================================
# Naming: these are CLANS, not guilds. discord.py already uses "guild" for a
# Discord server and guild_config/guild_self_roles/guild_banned_words are all
# keyed by server id. The player-facing command is !guild; the data is a clan.
_V3 = (
    """
    CREATE TABLE IF NOT EXISTS clans (
        clan_id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name             TEXT    NOT NULL,
        tag              TEXT    NOT NULL,
        emoji            TEXT    NOT NULL DEFAULT '🛡️',
        description      TEXT    NOT NULL DEFAULT '',
        leader_id        INTEGER NOT NULL,
        discord_guild_id INTEGER,
        is_open          INTEGER NOT NULL DEFAULT 1,
        max_members      INTEGER NOT NULL DEFAULT 20,
        created_at       INTEGER NOT NULL,
        disbanded_at     INTEGER
    )
    """,
    # Partial unique indexes: disbanding is a soft delete (history in duel_log
    # and dungeon_runs stays readable), and it frees the name and tag for reuse.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_clans_name "
    "ON clans(name COLLATE NOCASE) WHERE disbanded_at IS NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_clans_tag "
    "ON clans(tag COLLATE NOCASE) WHERE disbanded_at IS NULL",

    # user_id is the PRIMARY KEY, so "one clan per player" is enforced by the
    # database rather than by an application check that can be raced.
    # players.clan_id is kept in sync in the same transaction as a fast pointer.
    """
    CREATE TABLE IF NOT EXISTS clan_members (
        user_id      INTEGER PRIMARY KEY REFERENCES players(user_id) ON DELETE CASCADE,
        clan_id      INTEGER NOT NULL REFERENCES clans(clan_id) ON DELETE CASCADE,
        role         TEXT    NOT NULL DEFAULT 'member',
        joined_at    INTEGER NOT NULL,
        contribution INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_clan_members_clan "
    "ON clan_members(clan_id, contribution DESC)",

    # ---------- DUELS ----------
    """
    CREATE TABLE IF NOT EXISTS duel_stats (
        user_id      INTEGER PRIMARY KEY REFERENCES players(user_id) ON DELETE CASCADE,
        rating       INTEGER NOT NULL DEFAULT 1000,
        wins         INTEGER NOT NULL DEFAULT 0,
        losses       INTEGER NOT NULL DEFAULT 0,
        draws        INTEGER NOT NULL DEFAULT 0,
        streak       INTEGER NOT NULL DEFAULT 0,
        best_streak  INTEGER NOT NULL DEFAULT 0,
        last_duel_at INTEGER NOT NULL DEFAULT 0,
        updated_at   INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_duel_stats_rating ON duel_stats(rating DESC)",

    """
    CREATE TABLE IF NOT EXISTS duel_log (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        challenger_id INTEGER NOT NULL,
        opponent_id   INTEGER NOT NULL,
        winner_id     INTEGER,
        wager         INTEGER NOT NULL DEFAULT 0,
        rounds        INTEGER NOT NULL,
        challenger_hp INTEGER NOT NULL,
        opponent_hp   INTEGER NOT NULL,
        rating_change INTEGER NOT NULL DEFAULT 0,
        created_at    INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_duel_log_challenger "
    "ON duel_log(challenger_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_duel_log_opponent "
    "ON duel_log(opponent_id, created_at DESC)",
)


# ============================================================
#  V4 -- PETS
# ============================================================
_V4 = (
    """
    CREATE TABLE IF NOT EXISTS pet_species (
        species_id     TEXT PRIMARY KEY,
        name           TEXT NOT NULL,
        emoji          TEXT NOT NULL,
        description    TEXT NOT NULL,
        rarity         TEXT NOT NULL DEFAULT 'common',
        base_power     INTEGER NOT NULL DEFAULT 0,
        base_thermals  INTEGER NOT NULL DEFAULT 0,
        base_clock     INTEGER NOT NULL DEFAULT 0,
        base_bandwidth INTEGER NOT NULL DEFAULT 0,
        hatch_weight   INTEGER NOT NULL DEFAULT 0,
        enabled        INTEGER NOT NULL DEFAULT 1,
        sort_order     INTEGER NOT NULL DEFAULT 0
    )
    """,

    # Owned instances. Releasing is a soft delete so hatch history survives, and
    # players.pet_id (reserved since Phase 1) points at the active one.
    """
    CREATE TABLE IF NOT EXISTS pets (
        pet_id      INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL REFERENCES players(user_id) ON DELETE CASCADE,
        species_id  TEXT    NOT NULL REFERENCES pet_species(species_id),
        name        TEXT,
        level       INTEGER NOT NULL DEFAULT 1,
        xp          INTEGER NOT NULL DEFAULT 0,
        hatched_at  INTEGER NOT NULL,
        released_at INTEGER
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pets_user ON pets(user_id, released_at)",

    # inventory.quantity has existed since v1 but nothing ever incremented it,
    # because add_item used ON CONFLICT DO NOTHING. Eggs need to stack, so this
    # flag marks which items increment instead of being treated as one-of-a-kind.
    # ALTER TABLE has no IF NOT EXISTS -- the runner tolerates a duplicate column.
    "ALTER TABLE shop_items ADD COLUMN stackable INTEGER NOT NULL DEFAULT 0",
)

# Peripherals and mascots: classes are people, gear is components, so pets get
# the thematic space nobody else is using.
#
# POWER is deliberately capped at 2 on every species. Duels are opposed rolls
# and therefore hypersensitive to POWER -- an early build gave the legendary
# +14 POWER, which won 79.5% of mirror matches and turned PvP into a hatch-luck
# lottery. Legendaries express their value through THERMALS/CLOCK/BANDWIDTH
# instead, which scale far more gently in PvP. See scripts/simulate_pets.py.
PET_SPECIES_SEED = [
    # id, name, emoji, rarity, P, T, C, B, hatch_weight, sort, description
    ("cache_hamster", "Cache Hamster", "🐹", "common", 0, 1, 1, 3, 26, 1,
     "Stuffs everything into its cheeks. Retrieval is instant, eviction is a mystery."),
    ("fan_sprite", "Case Fan Sprite", "🌀", "common", 0, 3, 1, 0, 26, 2,
     "Spins up the moment things get warm. Slightly whiny under load."),
    ("optical_mouse", "Optical Mouse", "🐁", "uncommon", 1, 0, 3, 1, 18, 3,
     "1000Hz polling rate. Refuses to work on glass."),
    ("silicon_bug", "Silicon Bug", "🐛", "uncommon", 2, 0, 2, 0, 18, 4,
     "Technically a feature. Hits considerably harder than documented."),
    ("router_owl", "Router Owl", "🦉", "rare", 1, 1, 2, 4, 7, 5,
     "Sees every packet. Judges you for the ones you drop."),
    ("heatsink_tortoise", "Heatsink Tortoise", "🐢", "rare", 0, 6, 0, 0, 7, 6,
     "Enormous thermal mass, zero urgency. Nothing gets through the shell."),
    ("daemon", "Daemon", "👹", "epic", 2, 2, 2, 2, 3, 7,
     "Runs in the background, never asks for anything, quietly does everything."),
    ("thermal_dragon", "Thermal Dragon", "🐉", "legendary", 2, 5, 3, 2, 1, 8,
     "Born in a case with no intake fans. Exhales at 94 degrees."),
]

EGG_SEED = [
    # code, display_name, category, price, sort_order
    ("EGG", "Mystery Egg", "egg", 3_000, 30),
]

# Eggs join the existing loot tables at a low weight -- roughly 5-9% of drops.
EGG_LOOT_SEED = [
    ("throttle", "EGG", 6),
    ("render", "EGG", 7),
    ("cryptomine", "EGG", 8),
    ("bsod", "EGG", 9),
]

_PET_SPECIES_SQL = (
    "INSERT OR IGNORE INTO pet_species "
    "(species_id, name, emoji, rarity, base_power, base_thermals, base_clock, "
    " base_bandwidth, hatch_weight, sort_order, description) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

_EGG_ITEM_SQL = (
    "INSERT OR IGNORE INTO shop_items "
    "(code, display_name, category, price, role_name, enabled, sort_order, stackable) "
    "VALUES (?, ?, ?, ?, NULL, 1, ?, 1)"
)


# ============================================================
#  V5 -- MARKETPLACE
# ============================================================
# One table. The listing row IS the escrow -- a listed item is removed from the
# seller's inventory and lives here until it sells, is cancelled, or expires.
# Leaving it in inventory would let the seller equip it, salvage it for $RAM, or
# relist it while it was still on the market.
#
# Rows with status='sold' double as the price history, so there is no separate
# escrow, sales, or history table.
_V5 = (
    """
    CREATE TABLE IF NOT EXISTS market_listings (
        listing_id  INTEGER PRIMARY KEY AUTOINCREMENT,
        seller_id   INTEGER NOT NULL REFERENCES players(user_id) ON DELETE CASCADE,
        item_code   TEXT    NOT NULL,
        quantity    INTEGER NOT NULL DEFAULT 1,
        price       INTEGER NOT NULL,
        listed_at   INTEGER NOT NULL,
        expires_at  INTEGER NOT NULL,
        status      TEXT    NOT NULL DEFAULT 'active',
        buyer_id    INTEGER,
        sold_at     INTEGER,
        fee_paid    INTEGER NOT NULL DEFAULT 0,
        tax_paid    INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_market_active "
    "ON market_listings(status, expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_market_seller "
    "ON market_listings(seller_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_market_item "
    "ON market_listings(item_code, status)",
)


# ============================================================
#  V6 -- PET HOUSES
# ============================================================
_V6 = (
    # Mirrors gear_stats: catalogue data keyed to a shop_items row, so houses
    # and furniture are ordinary items that the marketplace can already trade.
    """
    CREATE TABLE IF NOT EXISTS home_items (
        item_code   TEXT PRIMARY KEY REFERENCES shop_items(code) ON DELETE CASCADE,
        slot        TEXT    NOT NULL,
        tier        INTEGER NOT NULL DEFAULT 1,
        sleep_bonus REAL    NOT NULL DEFAULT 0,
        description TEXT    NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_home_items_slot ON home_items(slot)",

    # Mirrors equipment: PK enforces one item per slot with no app-level check.
    # Keyed by player, not by pet -- one home, whichever pet is active sleeps in
    # it. Per-pet would mean buying ten houses with no gameplay behind the cost.
    """
    CREATE TABLE IF NOT EXISTS pet_home (
        user_id   INTEGER NOT NULL REFERENCES players(user_id) ON DELETE CASCADE,
        slot      TEXT    NOT NULL,
        item_code TEXT    NOT NULL,
        placed_at INTEGER NOT NULL,
        PRIMARY KEY (user_id, slot)
    )
    """,

    # Sleep state. Note there is deliberately NO bonus_power column -- see the
    # note in config.SLEEP_BONUS_STATS. The absence is the enforcement.
    "ALTER TABLE pets ADD COLUMN sleep_count     INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE pets ADD COLUMN last_slept_at   INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE pets ADD COLUMN bonus_thermals  INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE pets ADD COLUMN bonus_clock     INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE pets ADD COLUMN bonus_bandwidth INTEGER NOT NULL DEFAULT 0",
)

# Houses are PC cases. A pet living in a repurposed 5.25" bay is the correct joke.
HOME_ITEM_SEED = [
    # code, display_name, category, price, sort_order
    ("DRIVEBAY", "Drive Bay", "house", 4_000, 40),
    ("MINIITX", "Mini-ITX Case", "house", 12_000, 41),
    ("FULLTOWER", "Full Tower", "house", 30_000, 42),
    ("FOAM", "Anti-Static Foam", "bed", 1_500, 43),
    ("THERMALPAD", "Thermal Pad", "bed", 5_000, 44),
    ("LIQUIDBED", "Liquid Cooling Bed", "bed", 14_000, 45),
    ("SILICA", "Silica Gel Packet", "food", 1_200, 46),
    ("PASTE", "Thermal Paste", "food", 4_500, 47),
    ("NITRO", "Liquid Nitrogen", "food", 13_000, 48),
]

HOME_STATS_SEED = [
    # code, slot, tier, sleep_bonus, description
    ("DRIVEBAY", "house", 1, 0.03, "A repurposed 5.25\" bay. Cosy, if you're small."),
    ("MINIITX", "house", 2, 0.07, "Compact, tidy airflow, surprisingly premium."),
    ("FULLTOWER", "house", 3, 0.12, "Nine fan mounts and a tempered glass side panel."),
    ("FOAM", "bed", 1, 0.03, "The pink stuff motherboards ship on."),
    ("THERMALPAD", "bed", 2, 0.07, "Squishy, conductive, weirdly comfortable."),
    ("LIQUIDBED", "bed", 3, 0.12, "A closed loop that runs quiet all night."),
    ("SILICA", "food", 1, 0.03, "DO NOT EAT. They eat it."),
    ("PASTE", "food", 2, 0.07, "Pea-sized serving. Never spread it."),
    ("NITRO", "food", 3, 0.12, "Served at -196C. Crunchy."),
]

_HOME_ITEM_SQL = (
    "INSERT OR IGNORE INTO shop_items "
    "(code, display_name, category, price, role_name, enabled, sort_order, stackable) "
    "VALUES (?, ?, ?, ?, NULL, 1, ?, 0)"
)

_HOME_STATS_SQL = (
    "INSERT OR IGNORE INTO home_items "
    "(item_code, slot, tier, sleep_bonus, description) VALUES (?, ?, ?, ?, ?)"
)


# Seed the shop with exactly the Nexus 1.x catalogue and prices.
# sort_order preserves the original dict ordering so !shop renders identically.
SHOP_SEED = [
    # (code, display_name, category, price, sort_order)
    ("5090", "RTX 5090", "gpu", 5000, 1),
    ("5080", "RTX 5080", "gpu", 4000, 2),
    ("5070", "RTX 5070", "gpu", 2500, 3),
    ("4090", "RTX 4090", "gpu", 3500, 4),
    ("4080", "RTX 4080", "gpu", 2500, 5),
    ("4070", "RTX 4070", "gpu", 1500, 6),
    ("9950X3D", "Ryzen 9950X3D", "cpu", 4500, 7),
    ("9900X", "Ryzen 9900X", "cpu", 3500, 8),
    ("7950X3D", "Ryzen 7950X3D", "cpu", 3000, 9),
    ("9700X", "Ryzen 9700X", "cpu", 2000, 10),
    ("7800X3D", "Ryzen 7800X3D", "cpu", 1800, 11),
    ("7700X", "Ryzen 7700X", "cpu", 1200, 12),
]

_SEED_SQL = (
    "INSERT OR IGNORE INTO shop_items "
    "(code, display_name, category, price, role_name, enabled, sort_order) "
    "VALUES (?, ?, ?, ?, ?, 1, ?)"
)

# ============================================================
#  V2 SEED DATA
# ============================================================
# Four classes, each owning exactly one niche so the pick is a real choice.
# Switching is free and unlimited, so these compete per-run rather than
# locking anyone in.
CLASS_SEED = [
    # id, name, emoji, desc, POW, THRM, CLK, BW, ram x, xp x, cd x, sort
    ("overclocker", "Overclocker", "⚡",
     "Glass cannon. Huge damage and crit, almost no thermal headroom -- "
     "you win fast or you cook.",
     18, 4, 14, 4, 1.15, 1.00, 1.00, 1),
    ("thermal_engineer", "Thermal Engineer", "❄️",
     "Tank. Enormous cooling headroom, modest output. You will almost never "
     "wipe, you will just earn a little less.",
     12, 35, 6, 4, 0.95, 1.00, 1.00, 2),
    ("data_miner", "Data Miner", "⛏️",
     "Loot specialist. Best $RAM haul and the best drop odds in the game. "
     "Mediocre in an actual fight.",
     11, 8, 8, 20, 1.30, 1.00, 1.00, 3),
    ("sysadmin", "Sysadmin", "🖥️",
     "Uptime build. 30% shorter dungeon cooldowns and +25% XP. Wins on "
     "volume, not on raw power.",
     13, 12, 10, 10, 1.00, 1.25, 0.70, 4),
]

# Gear that did not exist in Nexus 1.x, added to fill the COOLER and RAM slots.
# Everything is buyable AND droppable -- no dungeon exclusivity.
NEW_ITEM_SEED = [
    # code, display_name, category, price, sort_order
    ("AIO360", "360mm AIO", "cooler", 2200, 13),
    ("AIO240", "240mm AIO", "cooler", 1400, 14),
    ("TOWERAIR", "Tower Air Cooler", "cooler", 700, 15),
    ("LN2POT", "LN2 Pot", "cooler", 6000, 16),
    ("DDR5-8000", "DDR5-8000 CL30", "ram", 5500, 17),
    ("DDR5-6000", "DDR5-6000 CL30", "ram", 1600, 18),
    ("DDR5-5200", "DDR5-5200", "ram", 1000, 19),
    ("DDR4-3600", "DDR4-3600", "ram", 500, 20),
]

# Combat stats per item code. Salvage is 25% of shop price, rounded down.
GEAR_SEED = [
    # code, slot, POW, THRM, CLK, BW, rarity, price (salvage derived)
    ("5090", "gpu", 45, 5, 5, 10, "epic", 5000),
    ("5080", "gpu", 38, 5, 4, 8, "rare", 4000),
    ("5070", "gpu", 28, 4, 3, 6, "uncommon", 2500),
    ("4090", "gpu", 35, 4, 4, 8, "rare", 3500),
    ("4080", "gpu", 28, 4, 3, 6, "uncommon", 2500),
    ("4070", "gpu", 20, 3, 2, 4, "common", 1500),
    ("9950X3D", "cpu", 20, 4, 30, 8, "epic", 4500),
    ("9900X", "cpu", 16, 4, 24, 6, "rare", 3500),
    ("7950X3D", "cpu", 15, 4, 22, 6, "rare", 3000),
    ("9700X", "cpu", 12, 3, 18, 4, "uncommon", 2000),
    ("7800X3D", "cpu", 11, 3, 17, 4, "uncommon", 1800),
    ("7700X", "cpu", 9, 3, 13, 3, "common", 1200),
    ("LN2POT", "cooler", 10, 55, 15, 0, "legendary", 6000),
    ("AIO360", "cooler", 0, 30, 5, 0, "rare", 2200),
    ("AIO240", "cooler", 0, 20, 3, 0, "uncommon", 1400),
    ("TOWERAIR", "cooler", 0, 12, 1, 0, "common", 700),
    ("DDR5-8000", "ram", 0, 0, 12, 45, "legendary", 5500),
    ("DDR5-6000", "ram", 0, 0, 5, 25, "rare", 1600),
    ("DDR5-5200", "ram", 0, 0, 3, 18, "uncommon", 1000),
    ("DDR4-3600", "ram", 0, 0, 2, 10, "common", 500),
]

SALVAGE_RATE = 0.25

DUNGEON_SEED = [
    # id, name, emoji, desc, min_lvl, difficulty, encounters,
    # cooldown_s, ram_min, ram_max, xp, drop_chance, sort
    ("throttle", "Thermal Throttle Test", "🌡️",
     "A stress test that will not quit. Entry-level, but it bites if you are naked.",
     1, 45, 3, 3600, 80, 160, 40, 0.08, 1),
    ("render", "Render Farm Raid", "🎬",
     "Twelve hours of frames in one pass. Bring cooling.",
     5, 85, 3, 7200, 200, 400, 90, 0.12, 2),
    ("cryptomine", "Crypto Mine Collapse", "⛏️",
     "The rig farm is on fire and the loot is still warm.",
     10, 130, 4, 10800, 450, 800, 180, 0.16, 3),
    ("bsod", "The Blue Screen Abyss", "💀",
     "0x0000007B. Nobody who goes in explains what they saw.",
     20, 180, 4, 14400, 1000, 1800, 350, 0.22, 4),
]

# Tier-appropriate loot. Higher weight = more common within that dungeon.
LOOT_SEED = [
    ("throttle", "4070", 30), ("throttle", "7700X", 30),
    ("throttle", "TOWERAIR", 25), ("throttle", "DDR4-3600", 25),
    ("throttle", "9700X", 5), ("throttle", "5070", 4),

    ("render", "5070", 25), ("render", "4080", 22), ("render", "9700X", 25),
    ("render", "7800X3D", 22), ("render", "AIO240", 20), ("render", "DDR5-5200", 20),
    ("render", "4090", 6), ("render", "AIO360", 5),

    ("cryptomine", "4090", 22), ("cryptomine", "5080", 20),
    ("cryptomine", "9900X", 22), ("cryptomine", "7950X3D", 20),
    ("cryptomine", "AIO360", 18), ("cryptomine", "DDR5-6000", 18),
    ("cryptomine", "5090", 5), ("cryptomine", "9950X3D", 5),

    ("bsod", "5090", 25), ("bsod", "9950X3D", 25),
    ("bsod", "5080", 15), ("bsod", "9900X", 15),
    ("bsod", "LN2POT", 8), ("bsod", "DDR5-8000", 8),
]

_CLASS_SQL = (
    "INSERT OR IGNORE INTO classes "
    "(class_id, name, emoji, description, base_power, base_thermals, base_clock, "
    " base_bandwidth, ram_multiplier, xp_multiplier, cooldown_modifier, sort_order) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

_GEAR_SQL = (
    "INSERT OR IGNORE INTO gear_stats "
    "(item_code, slot, power, thermals, clock, bandwidth, rarity, salvage_value) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)

_DUNGEON_SQL = (
    "INSERT OR IGNORE INTO dungeons "
    "(dungeon_id, name, emoji, description, min_level, difficulty, encounters, "
    " cooldown_seconds, ram_reward_min, ram_reward_max, xp_reward, drop_chance, sort_order) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

_LOOT_SQL = (
    "INSERT OR IGNORE INTO dungeon_loot (dungeon_id, item_code, weight) VALUES (?, ?, ?)"
)


# (version, name, statements) -- statements are plain SQL strings.
MIGRATIONS = [
    (1, "foundation", _V1),
    (2, "rpg_core", _V2),
    (3, "pvp_clans", _V3),
    (4, "pets", _V4),
    (5, "marketplace", _V5),
    (6, "pet_houses", _V6),
]

# (version, sql, list_of_param_tuples) -- parameterised seed data per version.
SEEDS = {
    1: [(_SEED_SQL, [(c, n, cat, p, c, o) for c, n, cat, p, o in SHOP_SEED])],
    2: [
        # New cooler/RAM items land in shop_items first so gear_stats' FK holds.
        (_SEED_SQL, [(c, n, cat, p, c, o) for c, n, cat, p, o in NEW_ITEM_SEED]),
        (_CLASS_SQL, [
            (cid, name, emoji, desc, pw, th, ck, bw, rm, xm, cd, sort)
            for cid, name, emoji, desc, pw, th, ck, bw, rm, xm, cd, sort in CLASS_SEED
        ]),
        (_GEAR_SQL, [
            (code, slot, pw, th, ck, bw, rarity, int(price * SALVAGE_RATE))
            for code, slot, pw, th, ck, bw, rarity, price in GEAR_SEED
        ]),
        (_DUNGEON_SQL, DUNGEON_SEED),
        (_LOOT_SQL, LOOT_SEED),
    ],
    4: [
        (_EGG_ITEM_SQL, [(c, n, cat, p, o) for c, n, cat, p, o in EGG_SEED]),
        (_PET_SPECIES_SQL, [
            (sid, name, emoji, rarity, pw, th, ck, bw, weight, sort, desc)
            for sid, name, emoji, rarity, pw, th, ck, bw, weight, sort, desc
            in PET_SPECIES_SEED
        ]),
        (_LOOT_SQL, EGG_LOOT_SEED),
    ],
    6: [
        # shop_items first so home_items' foreign key holds
        (_HOME_ITEM_SQL, HOME_ITEM_SEED),
        (_HOME_STATS_SQL, HOME_STATS_SEED),
    ],
}

LATEST_VERSION = max(v for v, _, _ in MIGRATIONS)

BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at INTEGER NOT NULL
)
"""


def pending(current_version):
    """Yield the migrations that still need applying, in order."""
    for version, name, statements in MIGRATIONS:
        if version > current_version:
            yield version, name, statements


def is_benign_ddl_error(exc) -> bool:
    """True for a DDL failure that means 'already applied', not 'broken'.

    CREATE ... IF NOT EXISTS covers most statements, but ALTER TABLE has no such
    form. Without this, a database that already has the column would fail the
    migration, never record the version, and retry forever on every boot.
    """
    message = str(exc).lower()
    return "duplicate column name" in message


def apply_sync(conn: sqlite3.Connection, verbose: bool = True) -> int:
    """Bring a plain sqlite3 connection up to LATEST_VERSION.

    Used by scripts/migrate_json.py. core.database holds the async equivalent.
    Returns the version the database ended up at.
    """
    conn.execute(BOOTSTRAP)
    row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()
    current = row[0] if row else 0

    for version, name, statements in pending(current):
        for statement in statements:
            try:
                conn.execute(statement)
            except sqlite3.OperationalError as e:
                if not is_benign_ddl_error(e):
                    raise
        for sql, rows in SEEDS.get(version, []):
            conn.executemany(sql, rows)
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (version, int(time.time())),
        )
        conn.commit()
        if verbose:
            print(f"  applied migration v{version} ({name})")
        current = version

    return current
