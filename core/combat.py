"""The dungeon engine: stat derivation and encounter resolution.

Deliberately imports nothing from discord.py. Everything here is a pure
function of its arguments, so the entire combat system can be simulated and
balance-tested offline without a bot token -- see scripts/simulate_dungeons.py.

The conceit: a dungeon run is a benchmark. You assemble a build, you run a
workload, and it either completes or your rig thermal-throttles and shuts down.

  POWER      raw compute      -> beats the encounter threshold
  THERMALS   cooling headroom -> HP pool AND damage reduction
  CLOCK      clock speed      -> crit chance
  BANDWIDTH  memory bandwidth -> $RAM rewards and drop odds
"""

import random
from dataclasses import dataclass, field

import config


# ============================================================
#  STATS
# ============================================================
@dataclass
class CombatStats:
    power: int = 0
    thermals: int = 0
    clock: int = 0
    bandwidth: int = 0
    hp: int = 0
    crit_chance: float = 0.0

    # provenance, for the !build breakdown
    from_class: dict = field(default_factory=dict)
    from_gear: dict = field(default_factory=dict)
    from_level: dict = field(default_factory=dict)
    from_pet: dict = field(default_factory=dict)
    prestige_multiplier: float = 1.0


def prestige_stat_multiplier(prestige: int) -> float:
    return 1.0 + config.PRESTIGE_STAT_BONUS * max(0, prestige)


def prestige_ram_multiplier(prestige: int) -> float:
    return 1.0 + config.PRESTIGE_RAM_BONUS * max(0, prestige)


def prestige_xp_multiplier(prestige: int) -> float:
    return 1.0 + config.PRESTIGE_XP_BONUS * max(0, prestige)


def prestige_title(prestige: int) -> str:
    if prestige <= 0:
        return ""
    if prestige < len(config.PRESTIGE_TITLES):
        return config.PRESTIGE_TITLES[prestige]
    return config.PRESTIGE_TITLE_MAX


def pet_contribution(pet) -> dict:
    """Stat block an active pet adds. Scales with the pet's own level.

    Deliberately small: a maxed legendary lands around 15% of a geared player's
    POWER, so a pet is a meaningful edge and never a replacement for gear.
    """
    zero = {"power": 0, "thermals": 0, "clock": 0, "bandwidth": 0}
    if pet is None or getattr(pet, "species", None) is None:
        return zero
    mult = 1 + (max(1, pet.level) - 1) * config.PET_STAT_GROWTH
    s = pet.species
    return {
        "power": int(s.base_power * mult),
        "thermals": int(s.base_thermals * mult),
        "clock": int(s.base_clock * mult),
        "bandwidth": int(s.base_bandwidth * mult),
    }


def pet_ram_multiplier(pet) -> float:
    """Active-pet $RAM bonus: +0.4% per pet level, so +10% at the level cap."""
    if pet is None:
        return 1.0
    return 1.0 + max(0, pet.level) * config.PET_RAM_BONUS_PER_LEVEL


def compute_stats(player, player_class, gear, pet=None) -> CombatStats:
    """Combine class base + equipped gear + level + active pet + prestige.

    `gear` is any iterable of objects exposing power/thermals/clock/bandwidth.
    `player_class` and `pet` may both be None.
    """
    cls_stats = {
        "power": getattr(player_class, "base_power", 0) if player_class else 0,
        "thermals": getattr(player_class, "base_thermals", 0) if player_class else 0,
        "clock": getattr(player_class, "base_clock", 0) if player_class else 0,
        "bandwidth": getattr(player_class, "base_bandwidth", 0) if player_class else 0,
    }

    gear_stats = {"power": 0, "thermals": 0, "clock": 0, "bandwidth": 0}
    for item in gear:
        gear_stats["power"] += item.power
        gear_stats["thermals"] += item.thermals
        gear_stats["clock"] += item.clock
        gear_stats["bandwidth"] += item.bandwidth

    level_stats = {
        "power": player.level * config.LEVEL_POWER_PER_LEVEL,
        "thermals": player.level * config.LEVEL_THERMALS_PER_LEVEL,
        "clock": 0,
        "bandwidth": 0,
    }

    pet_stats = pet_contribution(pet)
    mult = prestige_stat_multiplier(player.prestige)

    def total(key):
        return int(
            (cls_stats[key] + gear_stats[key] + level_stats[key] + pet_stats[key]) * mult
        )

    power, thermals = total("power"), total("thermals")
    clock, bandwidth = total("clock"), total("bandwidth")

    return CombatStats(
        power=power,
        thermals=thermals,
        clock=clock,
        bandwidth=bandwidth,
        hp=config.BASE_HP + thermals,
        crit_chance=min(config.CRIT_CAP, clock / config.CRIT_DIVISOR),
        from_class=cls_stats,
        from_gear=gear_stats,
        from_level=level_stats,
        from_pet=pet_stats,
        prestige_multiplier=mult,
    )


# ============================================================
#  ENCOUNTERS
# ============================================================
@dataclass
class Encounter:
    name: str
    roll: int
    score: int
    threshold: int
    crit: bool
    result: str          # 'win' | 'partial' | 'fail'
    damage: int
    hp_after: int


@dataclass
class RunResult:
    outcome: str         # 'flawless' | 'cleared' | 'partial' | 'wiped'
    encounters: list
    wins: int
    partials: int
    fails: int
    total: int
    hp_remaining: int
    hp_max: int
    ram_earned: int
    xp_earned: int
    item_dropped: str | None
    cooldown_seconds: int


def _encounter_names(dungeon_id: str, count: int, rng) -> list:
    pool = list(config.ENCOUNTER_NAMES.get(dungeon_id, config.DEFAULT_ENCOUNTER_NAMES))
    rng.shuffle(pool)
    while len(pool) < count:                      # top up if the pool is short
        pool.append(rng.choice(config.DEFAULT_ENCOUNTER_NAMES))
    return pool[:count]


def resolve_encounter(stats: CombatStats, threshold: int, name: str, hp: int, rng) -> Encounter:
    roll = rng.randint(1, 100)
    crit = rng.random() * 100 < stats.crit_chance
    score = stats.power + roll
    if crit:
        score += int(stats.power * config.CRIT_BONUS)

    if score >= threshold:
        result, damage = "win", 0
    elif score >= threshold * config.PARTIAL_THRESHOLD:
        result, damage = "partial", 0
    else:
        result = "fail"
        # Damage is anchored to the DUNGEON TIER, with only a minor term for how
        # badly you missed. Two earlier versions were wrong in opposite ways
        # (see scripts/simulate_dungeons.py, which caught both):
        #   * dividing damage by THERMALS made survivability quadratic and drove
        #     the wipe rate to a flat 0% everywhere;
        #   * using the raw miss margin punished low POWER twice -- you failed
        #     more often AND took bigger hits -- so the tank wiped MORE than the
        #     glass cannon, which is backwards.
        # Anchoring to the tier keeps "how often you fail" (POWER) independent of
        # "how many fails you survive" (THERMALS -> HP).
        partial_cut = threshold * config.PARTIAL_THRESHOLD
        damage = max(
            config.MIN_DAMAGE,
            int(
                threshold * config.DAMAGE_BASE_RATIO
                + (partial_cut - score) * config.DAMAGE_MISS_RATIO
            ),
        )

    return Encounter(
        name=name, roll=roll, score=score, threshold=threshold, crit=crit,
        result=result, damage=damage, hp_after=hp - damage,
    )


def pick_drop(loot_table, rng):
    """Weighted pick from [(item_code, weight), ...]. None if the table is empty."""
    entries = [(code, w) for code, w in loot_table if w > 0]
    if not entries:
        return None
    total = sum(w for _, w in entries)
    target = rng.uniform(0, total)
    running = 0.0
    for code, weight in entries:
        running += weight
        if target <= running:
            return code
    return entries[-1][0]


def duel_score(stats: CombatStats, rng) -> tuple:
    """One player's roll for one duel round. Returns (score, crit, roll).

    Same shape as a PvE encounter roll -- POWER + d100, crit adds half your
    POWER -- so duels and dungeons stay one system rather than two. BANDWIDTH
    contributes a small amount so a legendary RAM stick is not literally worthless
    in PvP, but at ~1/10 the weight of POWER it never decides a fight.
    """
    roll = rng.randint(1, 100)
    crit = rng.random() * 100 < stats.crit_chance
    score = stats.power + roll + int(stats.bandwidth / config.DUEL_BANDWIDTH_DIVISOR)
    if crit:
        score += int(stats.power * config.CRIT_BONUS)
    return score, crit, roll


@dataclass
class DuelRound:
    index: int
    a_score: int
    b_score: int
    a_crit: bool
    b_crit: bool
    winner: str          # 'a' | 'b' | 'tie'
    damage: int
    a_hp: int
    b_hp: int


@dataclass
class DuelResult:
    winner: str          # 'a' | 'b' | 'draw'
    rounds: list
    a_hp: int
    b_hp: int
    a_hp_max: int
    b_hp_max: int
    a_rounds_won: int
    b_rounds_won: int
    decided_by: str      # 'knockout' | 'rounds' | 'health' | 'draw'


def resolve_duel(stats_a: CombatStats, stats_b: CombatStats, rng=random) -> DuelResult:
    """Opposed-roll PvP. Both players roll each round; only the winner deals damage.

    Ends on a knockout, otherwise on rounds won after DUEL_MAX_ROUNDS, otherwise
    on remaining HP percentage, otherwise a draw.
    """
    a_hp, b_hp = stats_a.hp, stats_b.hp
    rounds = []
    a_won = b_won = 0
    decided_by = "rounds"

    for i in range(1, config.DUEL_MAX_ROUNDS + 1):
        a_score, a_crit, _ = duel_score(stats_a, rng)
        b_score, b_crit, _ = duel_score(stats_b, rng)

        if a_score == b_score:
            # BANDWIDTH breaks the tie -- faster memory resolves contention first.
            if stats_a.bandwidth > stats_b.bandwidth:
                winner = "a"
            elif stats_b.bandwidth > stats_a.bandwidth:
                winner = "b"
            else:
                winner = "tie"
        else:
            winner = "a" if a_score > b_score else "b"

        damage = 0
        if winner != "tie":
            attacker = stats_a if winner == "a" else stats_b
            margin = abs(a_score - b_score)
            damage = max(
                config.MIN_DAMAGE,
                int(
                    attacker.power * config.DUEL_DAMAGE_POWER_RATIO
                    + margin * config.DUEL_DAMAGE_MARGIN_RATIO
                ),
            )
            if winner == "a":
                b_hp -= damage
                a_won += 1
            else:
                a_hp -= damage
                b_won += 1

        rounds.append(DuelRound(
            index=i, a_score=a_score, b_score=b_score, a_crit=a_crit, b_crit=b_crit,
            winner=winner, damage=damage, a_hp=max(0, a_hp), b_hp=max(0, b_hp),
        ))

        if a_hp <= 0 or b_hp <= 0:
            decided_by = "knockout"
            break

    if decided_by == "knockout":
        result = "b" if a_hp <= 0 else "a"
        if a_hp <= 0 and b_hp <= 0:          # both dropped in the same round
            result = "a" if stats_a.bandwidth >= stats_b.bandwidth else "b"
    else:
        # Remaining HP PERCENTAGE decides a duel that goes the distance, ahead of
        # rounds won. Ranking on rounds first made THERMALS decorative: you won by
        # taking rounds, never by outlasting, so the tank class lost to everyone.
        # Percentage (not absolute) is what rewards a deeper HP pool.
        a_pct = a_hp / stats_a.hp if stats_a.hp else 0
        b_pct = b_hp / stats_b.hp if stats_b.hp else 0
        if abs(a_pct - b_pct) > 1e-9:
            result, decided_by = ("a" if a_pct > b_pct else "b"), "health"
        elif a_won != b_won:
            result, decided_by = ("a" if a_won > b_won else "b"), "rounds"
        else:
            result, decided_by = "draw", "draw"

    return DuelResult(
        winner=result, rounds=rounds,
        a_hp=max(0, a_hp), b_hp=max(0, b_hp),
        a_hp_max=stats_a.hp, b_hp_max=stats_b.hp,
        a_rounds_won=a_won, b_rounds_won=b_won,
        decided_by=decided_by,
    )


def elo_change(rating_a: int, rating_b: int, score_a: float, k: int = None) -> int:
    """Standard Elo delta for player A. score_a is 1 win / 0.5 draw / 0 loss.

    This is what makes alt-farming pointless: beating someone 400 points below
    you is worth about 1 rating point.
    """
    k = config.ELO_K if k is None else k
    expected = 1 / (1 + 10 ** ((rating_b - rating_a) / 400))
    return round(k * (score_a - expected))


def resolve_run(stats, dungeon, player_class, loot_table, prestige=0, rng=random) -> RunResult:
    """Run a full dungeon and return everything needed to render and persist it."""
    hp = stats.hp
    encounters = []
    names = _encounter_names(dungeon.dungeon_id, dungeon.encounters, rng)

    for name in names:
        enc = resolve_encounter(stats, dungeon.difficulty, name, hp, rng)
        encounters.append(enc)
        hp = enc.hp_after
        if hp <= 0:
            break                                  # thermal shutdown, run over

    wins = sum(1 for e in encounters if e.result == "win")
    partials = sum(1 for e in encounters if e.result == "partial")
    fails = sum(1 for e in encounters if e.result == "fail")
    total = dungeon.encounters

    if hp <= 0:
        outcome = "wiped"
    elif fails == 0 and partials == 0:
        outcome = "flawless"
    elif fails == 0:
        outcome = "cleared"
    else:
        outcome = "partial"

    # Partial credit is worth half an encounter.
    ratio = (wins + 0.5 * partials) / total if total else 0.0

    ram = rng.randint(dungeon.ram_reward_min, dungeon.ram_reward_max) * ratio
    ram *= 1 + stats.bandwidth / config.BANDWIDTH_RAM_DIVISOR
    ram *= getattr(player_class, "ram_multiplier", 1.0) if player_class else 1.0
    ram *= prestige_ram_multiplier(prestige)
    if outcome == "flawless":
        ram *= config.FLAWLESS_RAM_MULTIPLIER

    xp = dungeon.xp_reward * ratio
    xp *= getattr(player_class, "xp_multiplier", 1.0) if player_class else 1.0
    xp *= prestige_xp_multiplier(prestige)

    # Wiping forfeits the drop, but never costs anything you already had.
    if outcome == "wiped":
        drop_chance = 0.0
    else:
        drop_chance = dungeon.drop_chance + stats.bandwidth / config.BANDWIDTH_DROP_DIVISOR
        if outcome == "flawless":
            drop_chance += config.FLAWLESS_DROP_BONUS
        drop_chance = min(config.DROP_CHANCE_CAP, drop_chance)

    dropped = pick_drop(loot_table, rng) if rng.random() < drop_chance else None

    cooldown = int(
        dungeon.cooldown_seconds
        * (getattr(player_class, "cooldown_modifier", 1.0) if player_class else 1.0)
    )

    return RunResult(
        outcome=outcome,
        encounters=encounters,
        wins=wins,
        partials=partials,
        fails=fails,
        total=total,
        hp_remaining=max(0, hp),
        hp_max=stats.hp,
        ram_earned=int(ram),
        xp_earned=int(xp),
        item_dropped=dropped,
        cooldown_seconds=cooldown,
    )
