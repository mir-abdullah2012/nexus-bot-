"""Balance-test the dungeon engine offline. No bot token, no network.

    python scripts/simulate_dungeons.py            # 20k runs per matchup
    python scripts/simulate_dungeons.py --runs 100000

Loads the real schema and the real seed data, then drives core.combat directly,
so what it measures is exactly what players will experience.
"""

import argparse
import random
import sqlite3
import statistics
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core import combat, migrations                       # noqa: E402
from core.models import Dungeon, GearItem, Player, PlayerClass   # noqa: E402


def load_seed_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    migrations.apply_sync(conn, verbose=False)
    return conn


def best_gear_for_budget(conn, budget):
    """Greedy 'best affordable item per slot', to model a realistic build."""
    chosen = []
    per_slot = budget / 4 if budget else 0
    for slot in ("gpu", "cpu", "cooler", "ram"):
        row = conn.execute(
            "SELECT g.*, s.display_name, s.price FROM gear_stats g "
            "JOIN shop_items s ON s.code = g.item_code "
            "WHERE g.slot = ? AND s.price <= ? ORDER BY s.price DESC LIMIT 1",
            (slot, per_slot),
        ).fetchone()
        if row:
            chosen.append(GearItem.from_row(row))
    return chosen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=20_000)
    ap.add_argument("--seed", type=int, default=1234)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    conn = load_seed_db()

    classes = [PlayerClass.from_row(r) for r in
               conn.execute("SELECT * FROM classes ORDER BY sort_order")]
    dungeons = [Dungeon.from_row(r) for r in
                conn.execute("SELECT * FROM dungeons ORDER BY sort_order")]

    # Profiles are relative to each dungeon's own min_level, so every tier gets
    # measured from "just barely allowed in" up to "comfortably over-geared".
    # Absolute levels alone hid the danger zone entirely.
    def profiles_for(dungeon):
        base = dungeon.min_level
        return [
            (f"min lvl {base:<2} no gear", base, 0),
            (f"min lvl {base:<2} ~4k gear", base, 4_000),
            (f"+5  lvl {base + 5:<2} ~10k gear", base + 5, 10_000),
            (f"+12 lvl {base + 12:<2} ~24k gear", base + 12, 24_000),
        ]

    print(f"\n{args.runs:,} runs per matchup, seed={args.seed}\n")

    for dungeon in dungeons:
        print("=" * 78)
        print(f"{dungeon.emoji} {dungeon.name}  "
              f"(lvl {dungeon.min_level}+, difficulty {dungeon.difficulty}, "
              f"{dungeon.encounters} encounters)")
        print("=" * 78)
        loot = [(r["item_code"], r["weight"]) for r in conn.execute(
            "SELECT item_code, weight FROM dungeon_loot WHERE dungeon_id = ?",
            (dungeon.dungeon_id,))]

        header = (f"  {'profile':<24}{'class':<19}"
                  f"{'wipe%':>7}{'flawless%':>11}{'avg $RAM':>10}{'avg XP':>8}{'drop%':>7}")
        print(header)
        print("  " + "-" * (len(header) - 2))

        for label, level, budget in profiles_for(dungeon):
            gear = best_gear_for_budget(conn, budget)
            for klass in classes:
                player = Player(user_id=1, level=level, prestige=0)
                stats = combat.compute_stats(player, klass, gear)

                outcomes, rams, xps, drops = [], [], [], 0
                for _ in range(args.runs):
                    res = combat.resolve_run(stats, dungeon, klass, loot, 0, rng)
                    outcomes.append(res.outcome)
                    rams.append(res.ram_earned)
                    xps.append(res.xp_earned)
                    drops += 1 if res.item_dropped else 0

                n = len(outcomes)
                wipe = 100 * outcomes.count("wiped") / n
                flaw = 100 * outcomes.count("flawless") / n
                print(f"  {label:<24}{klass.name:<19}"
                      f"{wipe:>6.1f}%{flaw:>10.1f}%"
                      f"{statistics.mean(rams):>10.0f}{statistics.mean(xps):>8.0f}"
                      f"{100 * drops / n:>6.1f}%")
        print()

    # ---- prestige impact ----
    print("=" * 78)
    print("PRESTIGE SCALING  (Overclocker, lvl 25, ~20k gear, Crypto Mine Collapse)")
    print("=" * 78)
    dungeon = next(d for d in dungeons if d.dungeon_id == "cryptomine")
    loot = [(r["item_code"], r["weight"]) for r in conn.execute(
        "SELECT item_code, weight FROM dungeon_loot WHERE dungeon_id = ?",
        (dungeon.dungeon_id,))]
    klass = classes[0]
    gear = best_gear_for_budget(conn, 20_000)
    print(f"  {'prestige':<10}{'POWER':>7}{'HP':>6}{'crit':>7}{'wipe%':>8}{'avg $RAM':>11}")
    print("  " + "-" * 47)
    for p in (0, 1, 3, 5):
        player = Player(user_id=1, level=25, prestige=p)
        stats = combat.compute_stats(player, klass, gear)
        outcomes, rams = [], []
        for _ in range(args.runs):
            res = combat.resolve_run(stats, dungeon, klass, loot, p, rng)
            outcomes.append(res.outcome)
            rams.append(res.ram_earned)
        print(f"  {p:<10}{stats.power:>7}{stats.hp:>6}{stats.crit_chance:>6.0f}%"
              f"{100 * outcomes.count('wiped') / len(outcomes):>7.1f}%"
              f"{statistics.mean(rams):>11.0f}")
    print()


if __name__ == "__main__":
    main()
