"""Balance-test PvP duels offline. No bot token, no second player needed.

    python scripts/simulate_duels.py
    python scripts/simulate_duels.py --runs 50000

Drives core.combat.resolve_duel() against the real seeded classes and gear, so
what it measures is what players will experience.
"""

import argparse
import random
import sqlite3
import statistics
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import config                                                  # noqa: E402
from core import combat, migrations                            # noqa: E402
from core.models import GearItem, Player, PlayerClass          # noqa: E402


def load_seed_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    migrations.apply_sync(conn, verbose=False)
    return conn


def gear_for_budget(conn, budget):
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
    ap.add_argument("--seed", type=int, default=4242)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    conn = load_seed_db()
    classes = [PlayerClass.from_row(r) for r in
               conn.execute("SELECT * FROM classes ORDER BY sort_order")]
    by_id = {k.class_id: k for k in classes}

    print(f"\n{args.runs:,} duels per matchup, seed={args.seed}")

    # ---- 1. mirror matches: is any class dominant at equal level and gear? ----
    print("\n" + "=" * 74)
    print("CLASS vs CLASS  (both lvl 15, both ~10k gear)")
    print("=" * 74)
    gear = gear_for_budget(conn, 10_000)
    stats = {
        k.class_id: combat.compute_stats(Player(user_id=1, level=15), k, gear)
        for k in classes
    }

    print(f"  {'':<19}" + "".join(f"{k.name[:9]:>11}" for k in classes))
    for a in classes:
        row = f"  {a.name:<19}"
        for b in classes:
            if a.class_id == b.class_id:
                row += f"{'—':>11}"
                continue
            wins = 0
            for _ in range(args.runs):
                res = combat.resolve_duel(stats[a.class_id], stats[b.class_id], rng)
                wins += 1 if res.winner == "a" else 0
            row += f"{100 * wins / args.runs:>10.1f}%"
        print(row)
    print("\n  (row beats column this often)")

    print(f"\n  {'class':<19}{'POWER':>7}{'HP':>6}{'crit':>7}{'BW':>6}")
    print("  " + "-" * 45)
    for k in classes:
        s = stats[k.class_id]
        print(f"  {k.name:<19}{s.power:>7}{s.hp:>6}{s.crit_chance:>6.0f}%{s.bandwidth:>6}")

    # ---- 2. does gear/level advantage decide fights? ----
    print("\n" + "=" * 74)
    print("POWER GAP  (Overclocker mirror, varying level + gear)")
    print("=" * 74)
    oc = by_id["overclocker"]
    base = combat.compute_stats(Player(user_id=1, level=15), oc, gear_for_budget(conn, 10_000))
    print(f"  {'opponent':<34}{'POWER':>7}{'gap':>7}{'A wins':>9}{'avg rounds':>12}")
    print("  " + "-" * 69)
    for label, lvl, budget in [
        ("identical build", 15, 10_000),
        ("lvl 12, 6k gear", 12, 6_000),
        ("lvl 8,  3k gear", 8, 3_000),
        ("lvl 5,  no gear", 5, 0),
        ("lvl 25, 24k gear (stronger)", 25, 24_000),
    ]:
        opp = combat.compute_stats(Player(user_id=2, level=lvl), oc, gear_for_budget(conn, budget))
        wins, rounds = 0, []
        for _ in range(args.runs):
            res = combat.resolve_duel(base, opp, rng)
            wins += 1 if res.winner == "a" else 0
            rounds.append(len(res.rounds))
        print(f"  {label:<34}{opp.power:>7}{base.power - opp.power:>+7}"
              f"{100 * wins / args.runs:>8.1f}%{statistics.mean(rounds):>12.2f}")

    # ---- 3. how are duels decided? ----
    print("\n" + "=" * 74)
    print("DECISION MODE  (identical builds -- are duels conclusive?)")
    print("=" * 74)
    modes, draws = {}, 0
    for _ in range(args.runs):
        res = combat.resolve_duel(base, base, rng)
        modes[res.decided_by] = modes.get(res.decided_by, 0) + 1
        draws += 1 if res.winner == "draw" else 0
    for mode, n in sorted(modes.items(), key=lambda x: -x[1]):
        print(f"  {mode:<14}{100 * n / args.runs:>7.1f}%")
    print(f"  {'draws':<14}{100 * draws / args.runs:>7.1f}%")

    # ---- 4. Elo: does farming a weak alt pay? ----
    print("\n" + "=" * 74)
    print("ELO -- rating gained by BEATING an opponent at each rating gap")
    print("=" * 74)
    print(f"  {'your rating':<14}{'their rating':<15}{'you win':>10}{'you lose':>11}")
    print("  " + "-" * 50)
    for mine, theirs in [(1000, 1000), (1200, 1000), (1400, 1000),
                         (1000, 1400), (1600, 1000)]:
        win = combat.elo_change(mine, theirs, 1.0)
        loss = combat.elo_change(mine, theirs, 0.0)
        print(f"  {mine:<14}{theirs:<15}{win:>+10}{loss:>+11}")

    # ---- 5. the live player's actual build ----
    print("\n" + "=" * 74)
    print("YOUR LIVE BUILD vs a fresh player")
    print("=" * 74)
    live_gear = []
    for code in ("5090", "9950X3D", "AIO360", "DDR5-8000"):
        row = conn.execute(
            "SELECT g.*, s.display_name, s.price FROM gear_stats g "
            "JOIN shop_items s ON s.code = g.item_code WHERE g.item_code = ?",
            (code,)).fetchone()
        live_gear.append(GearItem.from_row(row))
    live = combat.compute_stats(Player(user_id=1, level=5), by_id["overclocker"], live_gear)
    print(f"  you:   POWER {live.power}  HP {live.hp}  crit {live.crit_chance:.0f}%  "
          f"BW {live.bandwidth}")
    for label, lvl, budget in [("fresh lvl 3, no gear", 3, 0),
                               ("lvl 10, 8k gear", 10, 8_000)]:
        opp = combat.compute_stats(Player(user_id=2, level=lvl),
                                   by_id["overclocker"], gear_for_budget(conn, budget))
        wins = sum(1 for _ in range(args.runs)
                   if combat.resolve_duel(live, opp, rng).winner == "a")
        print(f"  vs {label:<24} POWER {opp.power:<4} -> you win "
              f"{100 * wins / args.runs:.1f}%")
    print()


if __name__ == "__main__":
    main()
