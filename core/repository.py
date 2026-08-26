"""All data access for Nexus 2.0.

Cogs call these methods and never write SQL themselves. That way a schema change
in a later phase touches exactly one file, and every feature reads the player
record the same way.

Balance arithmetic keeps the Nexus 1.x rule: a balance can never go below zero.
Multi-row money movement (!give, !rob) runs inside a transaction, which closes
the lost-update race the old JSON storage had.
"""

import json
import time

from core.models import (
    Clan, ClanMember, DuelStats, Dungeon, GearItem, GuildConfig, Pet, PetSpecies,
    Player, PlayerClass, Reminder, ShopItem, Warning,
)


def _now() -> int:
    return int(time.time())


class Repository:
    def __init__(self, db):
        self.db = db
        # guild config changes rarely but is read on every message, so cache it.
        self._guild_cache: dict[int, GuildConfig] = {}

    # ========================================================
    #  PLAYERS
    # ========================================================
    async def ensure_player(self, user_id: int) -> None:
        """Create the player row if it does not exist yet.

        Mirrors Nexus 1.x get_user(), which auto-created on first read.
        """
        now = _now()
        await self.db.execute(
            "INSERT OR IGNORE INTO players (user_id, created_at, updated_at) VALUES (?, ?, ?)",
            (user_id, now, now),
        )

    async def get_player(self, user_id: int) -> Player:
        await self.ensure_player(user_id)
        row = await self.db.fetchone("SELECT * FROM players WHERE user_id = ?", (user_id,))
        return Player.from_row(row)

    async def get_balance(self, user_id: int) -> int:
        await self.ensure_player(user_id)
        return await self.db.fetchval(
            "SELECT balance FROM players WHERE user_id = ?", (user_id,), default=0
        )

    async def adjust_balance(self, user_id: int, amount: int, reason: str,
                             related_id: int | None = None) -> int:
        """Move a balance by `amount`, flooring at zero. Returns the new balance."""
        await self.ensure_player(user_id)
        now = _now()
        async with self.db.transaction() as conn:
            await conn.execute(
                "UPDATE players SET balance = MAX(0, balance + ?), updated_at = ? "
                "WHERE user_id = ?",
                (amount, now, user_id),
            )
            async with conn.execute(
                "SELECT balance FROM players WHERE user_id = ?", (user_id,)
            ) as cur:
                row = await cur.fetchone()
            new_balance = row["balance"]
            await conn.execute(
                "INSERT INTO economy_log "
                "(user_id, delta, reason, related_id, balance_after, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, amount, reason, related_id, new_balance, now),
            )
        return new_balance

    async def transfer(self, from_id: int, to_id: int, amount: int, reason: str) -> None:
        """Move RAM between two players atomically (!give, !rob)."""
        await self.ensure_player(from_id)
        await self.ensure_player(to_id)
        now = _now()
        async with self.db.transaction() as conn:
            for uid, delta, other in ((from_id, -amount, to_id), (to_id, amount, from_id)):
                await conn.execute(
                    "UPDATE players SET balance = MAX(0, balance + ?), updated_at = ? "
                    "WHERE user_id = ?",
                    (delta, now, uid),
                )
                async with conn.execute(
                    "SELECT balance FROM players WHERE user_id = ?", (uid,)
                ) as cur:
                    row = await cur.fetchone()
                await conn.execute(
                    "INSERT INTO economy_log "
                    "(user_id, delta, reason, related_id, balance_after, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (uid, delta, reason, other, row["balance"], now),
                )

    async def add_xp(self, user_id: int, amount: int, xp_for_level, bonus_per_level: int):
        """Grant XP and level up at most once, exactly as Nexus 1.x did.

        Returns (levelled_up, new_level, bonus_awarded).
        """
        player = await self.get_player(user_id)
        xp = player.xp + amount
        level = player.level
        needed = xp_for_level(level)
        now = _now()

        if xp >= needed:
            xp -= needed
            level += 1
            bonus = level * bonus_per_level        # note: uses the NEW level
            await self.db.execute(
                "UPDATE players SET xp = ?, level = ?, balance = MAX(0, balance + ?), "
                "updated_at = ? WHERE user_id = ?",
                (xp, level, bonus, now, user_id),
            )
            await self.db.execute(
                "INSERT INTO economy_log "
                "(user_id, delta, reason, related_id, balance_after, created_at) "
                "VALUES (?, ?, ?, ?, "
                "(SELECT balance FROM players WHERE user_id = ?), ?)",
                (user_id, bonus, "levelup", None, user_id, now),
            )
            return True, level, bonus

        await self.db.execute(
            "UPDATE players SET xp = ?, updated_at = ? WHERE user_id = ?",
            (xp, now, user_id),
        )
        return False, level, 0

    async def set_last_daily(self, user_id: int, when: int) -> None:
        await self.db.execute(
            "UPDATE players SET last_daily = ?, updated_at = ? WHERE user_id = ?",
            (when, _now(), user_id),
        )

    async def top_players(self, limit: int) -> list[Player]:
        rows = await self.db.fetchall(
            "SELECT * FROM players ORDER BY balance DESC, user_id ASC LIMIT ?", (limit,)
        )
        return [Player.from_row(r) for r in rows]

    # ========================================================
    #  PLAYER KEY/VALUE (for future systems -- unused in Phase 1)
    # ========================================================
    async def get_player_data(self, user_id: int, namespace: str, key: str, default=None):
        raw = await self.db.fetchval(
            "SELECT value FROM player_data WHERE user_id = ? AND namespace = ? AND key = ?",
            (user_id, namespace, key),
        )
        return default if raw is None else json.loads(raw)

    async def set_player_data(self, user_id: int, namespace: str, key: str, value) -> None:
        await self.ensure_player(user_id)
        await self.db.execute(
            "INSERT INTO player_data (user_id, namespace, key, value) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, namespace, key) DO UPDATE SET value = excluded.value",
            (user_id, namespace, key, json.dumps(value)),
        )

    # ========================================================
    #  INVENTORY
    # ========================================================
    async def get_inventory(self, user_id: int) -> list[str]:
        rows = await self.db.fetchall(
            "SELECT item_code FROM inventory WHERE user_id = ? ORDER BY acquired_at ASC, id ASC",
            (user_id,),
        )
        return [r["item_code"] for r in rows]

    async def add_item(self, user_id: int, item_code: str, quantity: int = 1,
                       stackable: bool = False) -> None:
        """Add an item.

        Gear is one-of-a-kind, exactly as Nexus 1.x behaved, so a repeat is a
        no-op. Stackable items (eggs) increment the quantity column instead --
        it has existed since v1 but nothing ever used it.
        """
        await self.ensure_player(user_id)
        conflict = (
            "DO UPDATE SET quantity = quantity + excluded.quantity"
            if stackable else "DO NOTHING"
        )
        await self.db.execute(
            "INSERT INTO inventory (user_id, item_code, quantity, acquired_at) "
            f"VALUES (?, ?, ?, ?) ON CONFLICT(user_id, item_code) {conflict}",
            (user_id, item_code, quantity, _now()),
        )

    async def is_stackable(self, item_code: str) -> bool:
        return bool(await self.db.fetchval(
            "SELECT stackable FROM shop_items WHERE code = ?", (item_code,), default=0
        ))

    async def get_item_quantity(self, user_id: int, item_code: str) -> int:
        return await self.db.fetchval(
            "SELECT quantity FROM inventory WHERE user_id = ? AND item_code = ?",
            (user_id, item_code), default=0,
        ) or 0

    async def get_inventory_counts(self, user_id: int) -> list:
        """[(item_code, quantity)] in acquisition order, for !inventory."""
        rows = await self.db.fetchall(
            "SELECT item_code, quantity FROM inventory WHERE user_id = ? "
            "ORDER BY acquired_at ASC, id ASC",
            (user_id,),
        )
        return [(r["item_code"], r["quantity"]) for r in rows]

    async def consume_item(self, user_id: int, item_code: str, amount: int = 1) -> bool:
        """Spend a stackable item. Removes the row when the stack empties."""
        have = await self.get_item_quantity(user_id, item_code)
        if have < amount:
            return False
        if have == amount:
            await self.db.execute(
                "DELETE FROM inventory WHERE user_id = ? AND item_code = ?",
                (user_id, item_code),
            )
        else:
            await self.db.execute(
                "UPDATE inventory SET quantity = quantity - ? "
                "WHERE user_id = ? AND item_code = ?",
                (amount, user_id, item_code),
            )
        return True

    async def has_item(self, user_id: int, item_code: str) -> bool:
        found = await self.db.fetchval(
            "SELECT 1 FROM inventory WHERE user_id = ? AND item_code = ?", (user_id, item_code)
        )
        return found is not None

    # ========================================================
    #  SHOP
    # ========================================================
    async def get_shop_items(self) -> list[ShopItem]:
        rows = await self.db.fetchall(
            "SELECT * FROM shop_items WHERE enabled = 1 ORDER BY sort_order ASC"
        )
        return [ShopItem.from_row(r) for r in rows]

    async def get_shop_item(self, code: str) -> ShopItem | None:
        row = await self.db.fetchone(
            "SELECT * FROM shop_items WHERE code = ? AND enabled = 1", (code,)
        )
        return ShopItem.from_row(row) if row else None

    # ========================================================
    #  GUILD CONFIG (per DISCORD server)
    # ========================================================
    async def ensure_guild(self, guild_id: int) -> None:
        now = _now()
        await self.db.execute(
            "INSERT OR IGNORE INTO guild_config (guild_id, created_at, updated_at) "
            "VALUES (?, ?, ?)",
            (guild_id, now, now),
        )

    async def get_guild_config(self, guild_id: int) -> GuildConfig:
        cached = self._guild_cache.get(guild_id)
        if cached is not None:
            return cached

        await self.ensure_guild(guild_id)
        row = await self.db.fetchone(
            "SELECT * FROM guild_config WHERE guild_id = ?", (guild_id,)
        )
        # ORDER BY rowid preserves the order roles were added, matching the
        # append-order of the old config.json self_roles list.
        role_rows = await self.db.fetchall(
            "SELECT role_name FROM guild_self_roles WHERE guild_id = ? ORDER BY rowid",
            (guild_id,),
        )
        word_rows = await self.db.fetchall(
            "SELECT word FROM guild_banned_words WHERE guild_id = ? ORDER BY word",
            (guild_id,),
        )
        cfg = GuildConfig.from_row(
            row,
            self_roles=[r["role_name"] for r in role_rows],
            extra_banned=[r["word"] for r in word_rows],
        )
        self._guild_cache[guild_id] = cfg
        return cfg

    def invalidate_guild(self, guild_id: int) -> None:
        self._guild_cache.pop(guild_id, None)

    async def set_guild_field(self, guild_id: int, field: str, value) -> None:
        if field not in {"welcome_channel", "log_channel", "ai_enabled", "prefix"}:
            raise ValueError(f"refusing to set unknown guild_config field: {field}")
        await self.ensure_guild(guild_id)
        await self.db.execute(
            f"UPDATE guild_config SET {field} = ?, updated_at = ? WHERE guild_id = ?",
            (value, _now(), guild_id),
        )
        self.invalidate_guild(guild_id)

    async def add_self_role(self, guild_id: int, role_name: str) -> None:
        await self.ensure_guild(guild_id)
        await self.db.execute(
            "INSERT OR IGNORE INTO guild_self_roles (guild_id, role_name) VALUES (?, ?)",
            (guild_id, role_name),
        )
        self.invalidate_guild(guild_id)

    async def add_banned_word(self, guild_id: int, word: str,
                              added_by: int | None = None) -> None:
        await self.ensure_guild(guild_id)
        await self.db.execute(
            "INSERT OR IGNORE INTO guild_banned_words (guild_id, word, added_by, added_at) "
            "VALUES (?, ?, ?, ?)",
            (guild_id, word.lower(), added_by, _now()),
        )
        self.invalidate_guild(guild_id)

    # ========================================================
    #  WARNINGS
    # ========================================================
    async def add_warning(self, guild_id: int, user_id: int, reason: str,
                          moderator_id: int | None = None) -> int:
        """Record a warning. Returns the player's active warning count."""
        await self.db.execute(
            "INSERT INTO warnings (guild_id, user_id, moderator_id, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (guild_id, user_id, moderator_id, reason, _now()),
        )
        return await self.db.fetchval(
            "SELECT COUNT(*) FROM warnings WHERE guild_id = ? AND user_id = ? AND active = 1",
            (guild_id, user_id),
            default=0,
        )

    async def get_warnings(self, guild_id: int, user_id: int) -> list[Warning]:
        rows = await self.db.fetchall(
            "SELECT * FROM warnings WHERE guild_id = ? AND user_id = ? AND active = 1 "
            "ORDER BY created_at ASC, id ASC",
            (guild_id, user_id),
        )
        return [Warning.from_row(r) for r in rows]

    async def clear_warnings(self, guild_id: int, user_id: int) -> None:
        """Soft-clear: users see an empty list, mods keep the audit trail."""
        await self.db.execute(
            "UPDATE warnings SET active = 0 WHERE guild_id = ? AND user_id = ? AND active = 1",
            (guild_id, user_id),
        )

    # ========================================================
    #  REMINDERS
    # ========================================================
    async def add_reminder(self, user_id: int, channel_id: int, remind_at: int,
                           text: str, guild_id: int | None = None) -> int:
        return await self.db.execute(
            "INSERT INTO reminders (user_id, channel_id, guild_id, remind_at, text, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, channel_id, guild_id, remind_at, text, _now()),
        )

    async def due_reminders(self, now: int) -> list[Reminder]:
        rows = await self.db.fetchall(
            "SELECT * FROM reminders WHERE fired = 0 AND remind_at <= ? ORDER BY remind_at ASC",
            (now,),
        )
        return [Reminder.from_row(r) for r in rows]

    async def mark_reminders_fired(self, ids: list[int]) -> None:
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        await self.db.execute(
            f"UPDATE reminders SET fired = 1 WHERE id IN ({placeholders})", tuple(ids)
        )

    # ========================================================
    #  PHASE 2 -- CLASSES
    # ========================================================
    async def get_classes(self) -> list:
        rows = await self.db.fetchall(
            "SELECT * FROM classes WHERE enabled = 1 ORDER BY sort_order ASC"
        )
        return [PlayerClass.from_row(r) for r in rows]

    async def get_class(self, class_id: str):
        row = await self.db.fetchone(
            "SELECT * FROM classes WHERE class_id = ? AND enabled = 1", (class_id,)
        )
        return PlayerClass.from_row(row) if row else None

    async def find_class(self, query: str):
        """Resolve a user-typed name to a class: exact id, exact name, or prefix."""
        raw = query.strip().lower()
        q = raw.replace(" ", "_")
        classes = await self.get_classes()
        for klass in classes:
            if q in (klass.class_id, klass.name.lower().replace(" ", "_")):
                return klass
        matches = [
            k for k in classes
            if k.class_id.startswith(q) or k.name.lower().startswith(raw)
        ]
        return matches[0] if len(matches) == 1 else None

    async def set_class(self, user_id: int, class_id: str) -> None:
        await self.ensure_player(user_id)
        await self.db.execute(
            "UPDATE players SET class_id = ?, updated_at = ? WHERE user_id = ?",
            (class_id, _now(), user_id),
        )

    # ========================================================
    #  PHASE 2 -- GEAR
    # ========================================================
    async def get_gear(self, item_code: str):
        row = await self.db.fetchone(
            "SELECT g.*, s.display_name, s.price FROM gear_stats g "
            "JOIN shop_items s ON s.code = g.item_code WHERE g.item_code = ?",
            (item_code,),
        )
        return GearItem.from_row(row) if row else None

    async def get_equipped(self, user_id: int) -> list:
        rows = await self.db.fetchall(
            "SELECT g.*, s.display_name, s.price FROM equipment e "
            "JOIN gear_stats g ON g.item_code = e.item_code "
            "JOIN shop_items s ON s.code = e.item_code "
            "WHERE e.user_id = ?",
            (user_id,),
        )
        return [GearItem.from_row(r) for r in rows]

    async def get_equipped_map(self, user_id: int) -> dict:
        return {item.slot: item for item in await self.get_equipped(user_id)}

    async def equip(self, user_id: int, item_code: str, slot: str) -> None:
        await self.ensure_player(user_id)
        await self.db.execute(
            "INSERT INTO equipment (user_id, slot, item_code, equipped_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, slot) DO UPDATE SET "
            "  item_code = excluded.item_code, equipped_at = excluded.equipped_at",
            (user_id, slot, item_code, _now()),
        )

    async def unequip(self, user_id: int, slot: str) -> bool:
        existing = await self.db.fetchval(
            "SELECT item_code FROM equipment WHERE user_id = ? AND slot = ?",
            (user_id, slot),
        )
        if existing is None:
            return False
        await self.db.execute(
            "DELETE FROM equipment WHERE user_id = ? AND slot = ?", (user_id, slot)
        )
        return True

    async def is_equipped(self, user_id: int, item_code: str) -> bool:
        found = await self.db.fetchval(
            "SELECT 1 FROM equipment WHERE user_id = ? AND item_code = ?",
            (user_id, item_code),
        )
        return found is not None

    async def remove_item(self, user_id: int, item_code: str) -> bool:
        """Drop one item from the inventory. Used by !salvage."""
        owned = await self.db.fetchval(
            "SELECT 1 FROM inventory WHERE user_id = ? AND item_code = ?",
            (user_id, item_code),
        )
        if owned is None:
            return False
        await self.db.execute(
            "DELETE FROM inventory WHERE user_id = ? AND item_code = ?",
            (user_id, item_code),
        )
        return True

    # ========================================================
    #  PHASE 2 -- DUNGEONS
    # ========================================================
    async def get_dungeons(self) -> list:
        rows = await self.db.fetchall(
            "SELECT * FROM dungeons WHERE enabled = 1 ORDER BY sort_order ASC"
        )
        return [Dungeon.from_row(r) for r in rows]

    async def get_dungeon(self, dungeon_id: str):
        row = await self.db.fetchone(
            "SELECT * FROM dungeons WHERE dungeon_id = ? AND enabled = 1", (dungeon_id,)
        )
        return Dungeon.from_row(row) if row else None

    async def find_dungeon(self, query: str):
        q = query.strip().lower()
        dungeons = await self.get_dungeons()
        for d in dungeons:
            if q in (d.dungeon_id, d.name.lower()):
                return d
        matches = [
            d for d in dungeons
            if d.dungeon_id.startswith(q) or d.name.lower().startswith(q)
        ]
        return matches[0] if len(matches) == 1 else None

    async def get_loot_table(self, dungeon_id: str) -> list:
        rows = await self.db.fetchall(
            "SELECT item_code, weight FROM dungeon_loot WHERE dungeon_id = ?",
            (dungeon_id,),
        )
        return [(r["item_code"], r["weight"]) for r in rows]

    async def dungeon_cooldown_remaining(self, user_id: int) -> int:
        """Seconds until this player may run another dungeon. 0 if ready.

        Reads the STORED cooldown_until rather than recomputing from the
        player's current class -- switching is free, so recomputing would let
        someone clear as Data Miner then switch to Sysadmin to shorten it.
        """
        until = await self.db.fetchval(
            "SELECT MAX(cooldown_until) FROM dungeon_runs WHERE user_id = ?",
            (user_id,),
            default=0,
        ) or 0
        return max(0, int(until) - _now())

    async def record_dungeon_run(self, user_id, dungeon_id, class_id, result) -> int:
        now = _now()
        return await self.db.execute(
            "INSERT INTO dungeon_runs "
            "(user_id, dungeon_id, class_id, started_at, cooldown_until, outcome, "
            " encounters_won, encounters_total, hp_remaining, ram_earned, xp_earned, "
            " item_dropped) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, dungeon_id, class_id, now, now + result.cooldown_seconds,
             result.outcome, result.wins, result.total, result.hp_remaining,
             result.ram_earned, result.xp_earned, result.item_dropped),
        )

    # ========================================================
    #  PHASE 2 -- PRESTIGE (deep reset)
    # ========================================================
    async def prestige_reset(self, user_id: int) -> dict:
        """Burn everything, bank a prestige level. Atomic.

        Wipes balance, xp, level, inventory and equipment. Deliberately does NOT
        touch last_daily (a cooldown, not progress -- resetting it would hand out
        a free daily) or warnings (moderation data, unrelated to progression).
        """
        player = await self.get_player(user_id)
        now = _now()
        items_burned = await self.db.fetchval(
            "SELECT COUNT(*) FROM inventory WHERE user_id = ?", (user_id,), default=0
        )

        async with self.db.transaction() as conn:
            await conn.execute("DELETE FROM equipment WHERE user_id = ?", (user_id,))
            await conn.execute("DELETE FROM inventory WHERE user_id = ?", (user_id,))
            await conn.execute(
                "UPDATE players SET balance = 0, xp = 0, level = 1, "
                "prestige = prestige + 1, updated_at = ? WHERE user_id = ?",
                (now, user_id),
            )
            await conn.execute(
                "INSERT INTO prestige_log "
                "(user_id, new_prestige, level_at_reset, balance_burned, items_burned, "
                " created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, player.prestige + 1, player.level, player.balance,
                 items_burned, now),
            )
            await conn.execute(
                "INSERT INTO economy_log "
                "(user_id, delta, reason, related_id, balance_after, created_at) "
                "VALUES (?, ?, ?, NULL, 0, ?)",
                (user_id, -player.balance, "prestige:reset", now),
            )

        return {
            "new_prestige": player.prestige + 1,
            "level_burned": player.level,
            "balance_burned": player.balance,
            "items_burned": items_burned,
        }

    # ========================================================
    #  PHASE 3 -- CLANS
    # ========================================================
    _CLAN_SELECT = (
        "SELECT c.*, (SELECT COUNT(*) FROM clan_members m WHERE m.clan_id = c.clan_id) "
        "AS member_count FROM clans c"
    )

    async def get_clan(self, clan_id: int):
        row = await self.db.fetchone(
            f"{self._CLAN_SELECT} WHERE c.clan_id = ? AND c.disbanded_at IS NULL",
            (clan_id,),
        )
        return Clan.from_row(row) if row else None

    async def find_clan(self, query: str):
        """Resolve a clan by exact name, exact tag, or unique prefix."""
        q = query.strip()
        row = await self.db.fetchone(
            f"{self._CLAN_SELECT} WHERE c.disbanded_at IS NULL AND "
            "(c.name = ? COLLATE NOCASE OR c.tag = ? COLLATE NOCASE)",
            (q, q),
        )
        if row:
            return Clan.from_row(row)
        rows = await self.db.fetchall(
            f"{self._CLAN_SELECT} WHERE c.disbanded_at IS NULL AND "
            "c.name LIKE ? COLLATE NOCASE LIMIT 2",
            (f"{q}%",),
        )
        return Clan.from_row(rows[0]) if len(rows) == 1 else None

    async def list_clans(self, limit: int) -> list:
        rows = await self.db.fetchall(
            f"{self._CLAN_SELECT} WHERE c.disbanded_at IS NULL "
            "ORDER BY member_count DESC, c.created_at ASC LIMIT ?",
            (limit,),
        )
        return [Clan.from_row(r) for r in rows]

    async def name_or_tag_taken(self, name: str, tag: str) -> str | None:
        """Returns 'name' or 'tag' if either is already in use by a live clan."""
        row = await self.db.fetchone(
            "SELECT name, tag FROM clans WHERE disbanded_at IS NULL AND "
            "(name = ? COLLATE NOCASE OR tag = ? COLLATE NOCASE) LIMIT 1",
            (name, tag),
        )
        if not row:
            return None
        return "name" if row["name"].lower() == name.lower() else "tag"

    async def get_membership(self, user_id: int):
        row = await self.db.fetchone(
            "SELECT * FROM clan_members WHERE user_id = ?", (user_id,)
        )
        return ClanMember.from_row(row) if row else None

    async def get_clan_members(self, clan_id: int) -> list:
        rows = await self.db.fetchall(
            "SELECT * FROM clan_members WHERE clan_id = ? "
            "ORDER BY CASE role WHEN 'leader' THEN 0 WHEN 'officer' THEN 1 ELSE 2 END, "
            "contribution DESC, joined_at ASC",
            (clan_id,),
        )
        return [ClanMember.from_row(r) for r in rows]

    async def create_clan(self, user_id, name, tag, emoji, discord_guild_id,
                          max_members: int = 20) -> int:
        """Create a clan and install the founder as leader. Atomic."""
        now = _now()
        await self.ensure_player(user_id)
        async with self.db.transaction() as conn:
            cur = await conn.execute(
                "INSERT INTO clans (name, tag, emoji, leader_id, discord_guild_id, "
                " max_members, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, tag, emoji, user_id, discord_guild_id, max_members, now),
            )
            clan_id = cur.lastrowid
            await conn.execute(
                "INSERT INTO clan_members (user_id, clan_id, role, joined_at) "
                "VALUES (?, ?, 'leader', ?)",
                (user_id, clan_id, now),
            )
            # players.clan_id is a synced pointer; clan_members is the truth.
            await conn.execute(
                "UPDATE players SET clan_id = ?, updated_at = ? WHERE user_id = ?",
                (clan_id, now, user_id),
            )
        return clan_id

    async def join_clan(self, user_id: int, clan_id: int) -> None:
        now = _now()
        await self.ensure_player(user_id)
        async with self.db.transaction() as conn:
            await conn.execute(
                "INSERT INTO clan_members (user_id, clan_id, role, joined_at) "
                "VALUES (?, ?, 'member', ?)",
                (user_id, clan_id, now),
            )
            await conn.execute(
                "UPDATE players SET clan_id = ?, updated_at = ? WHERE user_id = ?",
                (clan_id, now, user_id),
            )

    async def remove_from_clan(self, user_id: int) -> None:
        async with self.db.transaction() as conn:
            await conn.execute("DELETE FROM clan_members WHERE user_id = ?", (user_id,))
            await conn.execute(
                "UPDATE players SET clan_id = NULL, updated_at = ? WHERE user_id = ?",
                (_now(), user_id),
            )

    async def set_clan_role(self, user_id: int, role: str) -> None:
        await self.db.execute(
            "UPDATE clan_members SET role = ? WHERE user_id = ?", (role, user_id)
        )

    async def transfer_leadership(self, clan_id: int, old_leader: int,
                                  new_leader: int) -> None:
        async with self.db.transaction() as conn:
            await conn.execute(
                "UPDATE clan_members SET role = 'officer' WHERE user_id = ?", (old_leader,)
            )
            await conn.execute(
                "UPDATE clan_members SET role = 'leader' WHERE user_id = ?", (new_leader,)
            )
            await conn.execute(
                "UPDATE clans SET leader_id = ? WHERE clan_id = ?", (new_leader, clan_id)
            )

    async def disband_clan(self, clan_id: int) -> int:
        """Soft delete: frees the name/tag, keeps duel and dungeon history readable."""
        members = await self.db.fetchall(
            "SELECT user_id FROM clan_members WHERE clan_id = ?", (clan_id,)
        )
        async with self.db.transaction() as conn:
            await conn.execute(
                "UPDATE players SET clan_id = NULL WHERE clan_id = ?", (clan_id,)
            )
            await conn.execute("DELETE FROM clan_members WHERE clan_id = ?", (clan_id,))
            await conn.execute(
                "UPDATE clans SET disbanded_at = ? WHERE clan_id = ?", (_now(), clan_id)
            )
        return len(members)

    async def update_clan_field(self, clan_id: int, field: str, value) -> None:
        if field not in {"emoji", "description", "tag", "is_open"}:
            raise ValueError(f"refusing to set unknown clan field: {field}")
        await self.db.execute(
            f"UPDATE clans SET {field} = ? WHERE clan_id = ?", (value, clan_id)
        )

    async def add_contribution(self, user_id: int, amount: int) -> None:
        """Credit earned $RAM to the player's clan record. No-op if clanless.

        Contribution is EARNED, never deposited -- so there is no bank to drain
        and no new command surface to abuse.
        """
        if amount <= 0:
            return
        await self.db.execute(
            "UPDATE clan_members SET contribution = contribution + ? WHERE user_id = ?",
            (amount, user_id),
        )

    async def clan_war_scores(self, window_days: int, weights: dict) -> list:
        """7-day rolling clan leaderboard, aggregated from data already recorded.

        No separate war table and no separate combat system -- this is a query
        over dungeon_runs, duel_log and clan_members.contribution.
        """
        since = _now() - window_days * 86400
        rows = await self.db.fetchall(
            """
            SELECT c.clan_id, c.name, c.tag, c.emoji,
                   (SELECT COUNT(*) FROM clan_members m WHERE m.clan_id = c.clan_id)
                       AS member_count,
                   COALESCE((SELECT SUM(m.contribution) FROM clan_members m
                             WHERE m.clan_id = c.clan_id), 0) AS contribution,
                   COALESCE((SELECT COUNT(*) FROM dungeon_runs r
                             JOIN clan_members m ON m.user_id = r.user_id
                             WHERE m.clan_id = c.clan_id AND r.started_at >= ?
                               AND r.outcome IN ('cleared','flawless','partial')), 0)
                       AS clears,
                   COALESCE((SELECT COUNT(*) FROM dungeon_runs r
                             JOIN clan_members m ON m.user_id = r.user_id
                             WHERE m.clan_id = c.clan_id AND r.started_at >= ?
                               AND r.outcome = 'flawless'), 0) AS flawless,
                   COALESCE((SELECT COUNT(*) FROM duel_log d
                             JOIN clan_members m ON m.user_id = d.winner_id
                             WHERE m.clan_id = c.clan_id AND d.created_at >= ?), 0)
                       AS duel_wins
            FROM clans c
            WHERE c.disbanded_at IS NULL
            """,
            (since, since, since),
        )

        scored = []
        for r in rows:
            score = (
                r["clears"] * weights["clear"]
                + r["flawless"] * weights["flawless"]
                + r["duel_wins"] * weights["duel_win"]
                + r["contribution"] // weights["contribution_divisor"]
            )
            scored.append({
                "clan_id": r["clan_id"], "name": r["name"], "tag": r["tag"],
                "emoji": r["emoji"], "member_count": r["member_count"],
                "clears": r["clears"], "flawless": r["flawless"],
                "duel_wins": r["duel_wins"], "contribution": r["contribution"],
                "score": score,
            })
        scored.sort(key=lambda x: (-x["score"], x["name"].lower()))
        return scored

    # ========================================================
    #  PHASE 3 -- DUELS
    # ========================================================
    async def get_duel_stats(self, user_id: int, start_rating: int = 1000) -> DuelStats:
        await self.ensure_player(user_id)
        now = _now()
        await self.db.execute(
            "INSERT OR IGNORE INTO duel_stats (user_id, rating, updated_at) "
            "VALUES (?, ?, ?)",
            (user_id, start_rating, now),
        )
        row = await self.db.fetchone(
            "SELECT * FROM duel_stats WHERE user_id = ?", (user_id,)
        )
        return DuelStats.from_row(row)

    async def duel_cooldown_remaining(self, user_id: int, cooldown: int) -> int:
        last = await self.db.fetchval(
            "SELECT last_duel_at FROM duel_stats WHERE user_id = ?", (user_id,),
            default=0,
        ) or 0
        return max(0, int(last) + cooldown - _now())

    async def apply_duel_result(self, user_id: int, rating_delta: int,
                                outcome: str) -> int:
        """Update one player's duel record. outcome is 'win' | 'loss' | 'draw'."""
        stats = await self.get_duel_stats(user_id)
        now = _now()

        if outcome == "win":
            wins, losses, draws = stats.wins + 1, stats.losses, stats.draws
            streak = max(1, stats.streak + 1) if stats.streak >= 0 else 1
        elif outcome == "loss":
            wins, losses, draws = stats.wins, stats.losses + 1, stats.draws
            streak = min(-1, stats.streak - 1) if stats.streak <= 0 else -1
        else:
            wins, losses, draws = stats.wins, stats.losses, stats.draws + 1
            streak = 0

        new_rating = max(0, stats.rating + rating_delta)
        best = max(stats.best_streak, streak)

        await self.db.execute(
            "UPDATE duel_stats SET rating = ?, wins = ?, losses = ?, draws = ?, "
            "streak = ?, best_streak = ?, last_duel_at = ?, updated_at = ? "
            "WHERE user_id = ?",
            (new_rating, wins, losses, draws, streak, best, now, now, user_id),
        )
        return new_rating

    async def record_duel(self, challenger_id, opponent_id, winner_id, wager,
                          result, rating_change) -> int:
        return await self.db.execute(
            "INSERT INTO duel_log (challenger_id, opponent_id, winner_id, wager, "
            " rounds, challenger_hp, opponent_hp, rating_change, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (challenger_id, opponent_id, winner_id, wager, len(result.rounds),
             result.a_hp, result.b_hp, rating_change, _now()),
        )

    # ========================================================
    #  PHASE 4 -- PETS
    # ========================================================
    _PET_SELECT = (
        "SELECT p.*, s.name AS species_name, s.emoji, s.description, s.rarity, "
        "       s.base_power, s.base_thermals, s.base_clock, s.base_bandwidth, "
        "       s.hatch_weight, s.sort_order "
        "FROM pets p JOIN pet_species s ON s.species_id = p.species_id"
    )

    async def get_species_list(self) -> list:
        rows = await self.db.fetchall(
            "SELECT * FROM pet_species WHERE enabled = 1 ORDER BY sort_order ASC"
        )
        return [PetSpecies.from_row(r) for r in rows]

    async def get_species(self, species_id: str):
        row = await self.db.fetchone(
            "SELECT * FROM pet_species WHERE species_id = ?", (species_id,)
        )
        return PetSpecies.from_row(row) if row else None

    async def get_pet(self, pet_id: int):
        row = await self.db.fetchone(
            f"{self._PET_SELECT} WHERE p.pet_id = ? AND p.released_at IS NULL",
            (pet_id,),
        )
        return Pet.from_row(row) if row else None

    async def get_pets(self, user_id: int) -> list:
        rows = await self.db.fetchall(
            f"{self._PET_SELECT} WHERE p.user_id = ? AND p.released_at IS NULL "
            "ORDER BY p.level DESC, p.pet_id ASC",
            (user_id,),
        )
        return [Pet.from_row(r) for r in rows]

    async def count_pets(self, user_id: int) -> int:
        return await self.db.fetchval(
            "SELECT COUNT(*) FROM pets WHERE user_id = ? AND released_at IS NULL",
            (user_id,), default=0,
        )

    async def get_active_pet(self, user_id: int):
        """The pet in players.pet_id -- the column reserved back in Phase 1."""
        pet_id = await self.db.fetchval(
            "SELECT pet_id FROM players WHERE user_id = ?", (user_id,)
        )
        if not pet_id:
            return None
        pet = await self.get_pet(pet_id)
        if pet is None or pet.user_id != user_id:
            # Released or reassigned out from under us -- clear the pointer.
            await self.db.execute(
                "UPDATE players SET pet_id = NULL WHERE user_id = ?", (user_id,)
            )
            return None
        return pet

    async def set_active_pet(self, user_id: int, pet_id: int | None) -> None:
        await self.db.execute(
            "UPDATE players SET pet_id = ?, updated_at = ? WHERE user_id = ?",
            (pet_id, _now(), user_id),
        )

    async def hatch_pet(self, user_id: int, rng) -> Pet | None:
        """Roll a species by hatch_weight and create the instance."""
        rows = await self.db.fetchall(
            "SELECT * FROM pet_species WHERE enabled = 1 AND hatch_weight > 0"
        )
        if not rows:
            return None

        total = sum(r["hatch_weight"] for r in rows)
        target = rng.uniform(0, total)
        running = 0.0
        chosen = rows[-1]
        for r in rows:
            running += r["hatch_weight"]
            if target <= running:
                chosen = r
                break

        now = _now()
        pet_id = await self.db.execute(
            "INSERT INTO pets (user_id, species_id, level, xp, hatched_at) "
            "VALUES (?, ?, 1, 0, ?)",
            (user_id, chosen["species_id"], now),
        )
        return await self.get_pet(pet_id)

    async def rename_pet(self, pet_id: int, name: str | None) -> None:
        await self.db.execute("UPDATE pets SET name = ? WHERE pet_id = ?", (name, pet_id))

    async def release_pet(self, pet_id: int, user_id: int) -> None:
        """Soft delete, and clear the active pointer if this was the active pet."""
        async with self.db.transaction() as conn:
            await conn.execute(
                "UPDATE pets SET released_at = ? WHERE pet_id = ?", (_now(), pet_id)
            )
            await conn.execute(
                "UPDATE players SET pet_id = NULL WHERE user_id = ? AND pet_id = ?",
                (user_id, pet_id),
            )

    async def grant_pet_xp(self, user_id: int, player_xp: int, share: float,
                           xp_for_level, max_level: int):
        """Feed the ACTIVE pet a share of XP the player just earned.

        Only the active pet gains anything -- that is what makes choosing one an
        actual investment rather than a menu. Returns (pet, levelled, new_level)
        or (None, False, 0) when there is no active pet.
        """
        pet = await self.get_active_pet(user_id)
        if pet is None:
            return None, False, 0

        amount = int(player_xp * share)
        if amount <= 0 or pet.level >= max_level:
            return pet, False, pet.level

        xp = pet.xp + amount
        level = pet.level
        levelled = False

        # Loop rather than a single step: a big dungeon payout can legitimately
        # carry a low-level pet through more than one level at once.
        while level < max_level and xp >= xp_for_level(level):
            xp -= xp_for_level(level)
            level += 1
            levelled = True

        if level >= max_level:
            xp = 0

        await self.db.execute(
            "UPDATE pets SET xp = ?, level = ? WHERE pet_id = ?", (xp, level, pet.pet_id)
        )
        pet.xp, pet.level = xp, level
        return pet, levelled, level

    async def top_duelists(self, limit: int) -> list:
        rows = await self.db.fetchall(
            "SELECT * FROM duel_stats WHERE wins + losses + draws > 0 "
            "ORDER BY rating DESC, wins DESC LIMIT ?",
            (limit,),
        )
        return [DuelStats.from_row(r) for r in rows]
