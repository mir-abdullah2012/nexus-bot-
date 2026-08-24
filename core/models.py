"""Typed views over database rows.

Cogs work with these instead of raw sqlite3.Row objects, so a column rename in a
later phase is a compiler-ish error in one place rather than a KeyError at 2am.
"""

from dataclasses import dataclass, field


@dataclass
class Player:
    """The central player record. Every future system hangs off user_id."""

    user_id: int
    balance: int = 0
    xp: int = 0
    level: int = 1
    last_daily: int = 0

    # Reserved for later phases. Populated by the schema, unused by Phase 1.
    class_id: str | None = None
    clan_id: int | None = None
    pet_id: int | None = None
    battle_pass_tier: int = 0
    battle_pass_xp: int = 0
    prestige: int = 0

    created_at: int = 0
    updated_at: int = 0

    @classmethod
    def from_row(cls, row):
        return cls(
            user_id=row["user_id"],
            balance=row["balance"],
            xp=row["xp"],
            level=row["level"],
            last_daily=row["last_daily"],
            class_id=row["class_id"],
            clan_id=row["clan_id"],
            pet_id=row["pet_id"],
            battle_pass_tier=row["battle_pass_tier"],
            battle_pass_xp=row["battle_pass_xp"],
            prestige=row["prestige"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass
class GuildConfig:
    """Per-DISCORD-server settings. Not to be confused with players.clan_id."""

    guild_id: int
    welcome_channel: int | None = None
    log_channel: int | None = None
    ai_enabled: bool = True
    prefix: str = "!"
    self_roles: list = field(default_factory=list)
    extra_banned: list = field(default_factory=list)

    @classmethod
    def from_row(cls, row, self_roles=None, extra_banned=None):
        return cls(
            guild_id=row["guild_id"],
            welcome_channel=row["welcome_channel"],
            log_channel=row["log_channel"],
            ai_enabled=bool(row["ai_enabled"]),
            prefix=row["prefix"],
            self_roles=list(self_roles or []),
            extra_banned=list(extra_banned or []),
        )


@dataclass
class Warning:
    id: int
    guild_id: int
    user_id: int
    reason: str
    created_at: int
    moderator_id: int | None = None
    active: bool = True

    @classmethod
    def from_row(cls, row):
        return cls(
            id=row["id"],
            guild_id=row["guild_id"],
            user_id=row["user_id"],
            reason=row["reason"],
            created_at=row["created_at"],
            moderator_id=row["moderator_id"],
            active=bool(row["active"]),
        )


@dataclass
class Reminder:
    id: int
    user_id: int
    channel_id: int
    remind_at: int
    text: str
    guild_id: int | None = None
    created_at: int = 0
    fired: bool = False

    @classmethod
    def from_row(cls, row):
        return cls(
            id=row["id"],
            user_id=row["user_id"],
            channel_id=row["channel_id"],
            remind_at=row["remind_at"],
            text=row["text"],
            guild_id=row["guild_id"],
            created_at=row["created_at"],
            fired=bool(row["fired"]),
        )


@dataclass
class ShopItem:
    code: str
    display_name: str
    category: str
    price: int
    role_name: str | None = None
    enabled: bool = True
    sort_order: int = 0

    @classmethod
    def from_row(cls, row):
        return cls(
            code=row["code"],
            display_name=row["display_name"],
            category=row["category"],
            price=row["price"],
            role_name=row["role_name"],
            enabled=bool(row["enabled"]),
            sort_order=row["sort_order"],
        )


# ============================================================
#  PHASE 2 -- RPG CORE
# ============================================================
@dataclass
class PlayerClass:
    class_id: str
    name: str
    description: str
    emoji: str
    base_power: int = 0
    base_thermals: int = 0
    base_clock: int = 0
    base_bandwidth: int = 0
    ram_multiplier: float = 1.0
    xp_multiplier: float = 1.0
    cooldown_modifier: float = 1.0
    unlock_level: int = 1
    sort_order: int = 0

    @classmethod
    def from_row(cls, row):
        return cls(
            class_id=row["class_id"],
            name=row["name"],
            description=row["description"],
            emoji=row["emoji"],
            base_power=row["base_power"],
            base_thermals=row["base_thermals"],
            base_clock=row["base_clock"],
            base_bandwidth=row["base_bandwidth"],
            ram_multiplier=row["ram_multiplier"],
            xp_multiplier=row["xp_multiplier"],
            cooldown_modifier=row["cooldown_modifier"],
            unlock_level=row["unlock_level"],
            sort_order=row["sort_order"],
        )


@dataclass
class GearItem:
    """A shop_items row joined with its gear_stats row."""

    item_code: str
    display_name: str
    slot: str
    power: int = 0
    thermals: int = 0
    clock: int = 0
    bandwidth: int = 0
    rarity: str = "common"
    salvage_value: int = 0
    price: int = 0

    @classmethod
    def from_row(cls, row):
        return cls(
            item_code=row["item_code"],
            display_name=row["display_name"],
            slot=row["slot"],
            power=row["power"],
            thermals=row["thermals"],
            clock=row["clock"],
            bandwidth=row["bandwidth"],
            rarity=row["rarity"],
            salvage_value=row["salvage_value"],
            price=row["price"] if "price" in row.keys() else 0,
        )

    def stat_line(self) -> str:
        parts = []
        if self.power:
            parts.append(f"PWR {self.power:+d}")
        if self.thermals:
            parts.append(f"THRM {self.thermals:+d}")
        if self.clock:
            parts.append(f"CLK {self.clock:+d}")
        if self.bandwidth:
            parts.append(f"BW {self.bandwidth:+d}")
        return " · ".join(parts) if parts else "no combat stats"


# ============================================================
#  PHASE 3 -- PVP + CLANS
# ============================================================
@dataclass
class Clan:
    """A player clan. Called a 'guild' in commands, 'clan' everywhere in code --
    discord.py already owns the word 'guild' for Discord servers."""

    clan_id: int
    name: str
    tag: str
    emoji: str
    description: str
    leader_id: int
    discord_guild_id: int | None = None
    is_open: bool = True
    max_members: int = 20
    created_at: int = 0
    disbanded_at: int | None = None
    member_count: int = 0

    @classmethod
    def from_row(cls, row):
        keys = row.keys()
        return cls(
            clan_id=row["clan_id"],
            name=row["name"],
            tag=row["tag"],
            emoji=row["emoji"],
            description=row["description"],
            leader_id=row["leader_id"],
            discord_guild_id=row["discord_guild_id"],
            is_open=bool(row["is_open"]),
            max_members=row["max_members"],
            created_at=row["created_at"],
            disbanded_at=row["disbanded_at"],
            member_count=row["member_count"] if "member_count" in keys else 0,
        )

    def label(self) -> str:
        return f"{self.emoji} [{self.tag}] {self.name}"


@dataclass
class ClanMember:
    user_id: int
    clan_id: int
    role: str = "member"
    joined_at: int = 0
    contribution: int = 0

    @classmethod
    def from_row(cls, row):
        return cls(
            user_id=row["user_id"],
            clan_id=row["clan_id"],
            role=row["role"],
            joined_at=row["joined_at"],
            contribution=row["contribution"],
        )

    @property
    def is_leader(self) -> bool:
        return self.role == "leader"

    @property
    def can_kick(self) -> bool:
        return self.role in ("leader", "officer")


@dataclass
class DuelStats:
    user_id: int
    rating: int = 1000
    wins: int = 0
    losses: int = 0
    draws: int = 0
    streak: int = 0
    best_streak: int = 0
    last_duel_at: int = 0

    @classmethod
    def from_row(cls, row):
        return cls(
            user_id=row["user_id"],
            rating=row["rating"],
            wins=row["wins"],
            losses=row["losses"],
            draws=row["draws"],
            streak=row["streak"],
            best_streak=row["best_streak"],
            last_duel_at=row["last_duel_at"],
        )

    @property
    def total(self) -> int:
        return self.wins + self.losses + self.draws

    @property
    def winrate(self) -> float:
        return (self.wins / self.total * 100) if self.total else 0.0


@dataclass
class Dungeon:
    dungeon_id: str
    name: str
    description: str
    emoji: str
    min_level: int
    difficulty: int
    encounters: int
    cooldown_seconds: int
    ram_reward_min: int
    ram_reward_max: int
    xp_reward: int
    drop_chance: float
    sort_order: int = 0

    @classmethod
    def from_row(cls, row):
        return cls(
            dungeon_id=row["dungeon_id"],
            name=row["name"],
            description=row["description"],
            emoji=row["emoji"],
            min_level=row["min_level"],
            difficulty=row["difficulty"],
            encounters=row["encounters"],
            cooldown_seconds=row["cooldown_seconds"],
            ram_reward_min=row["ram_reward_min"],
            ram_reward_max=row["ram_reward_max"],
            xp_reward=row["xp_reward"],
            drop_chance=row["drop_chance"],
            sort_order=row["sort_order"],
        )
