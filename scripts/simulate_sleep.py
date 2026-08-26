"""Model the pet sleep system offline.

    python scripts/simulate_sleep.py

Three questions:
  1. Does the chance curve feel right across furniture setups?
  2. How long does maxing the +12 lifetime bonus actually take?
  3. Does a maxed bonus break duel balance, the way pet POWER did in Phase 4?
"""

import argparse
import random
import sqlite3
import statistics
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import config                                                        # noqa: E402
from core import combat, migrations                                  # noqa: E402
from core.models import GearItem, HomeItem, Pet, PetSpecies, Player, PlayerClass  # noqa: E402


def seed_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    migrations.apply_sync(conn, verbose=False)
    return conn


def home_from(conn, codes):
    out = {}
    for code in codes:
        row = conn.execute(
            "SELECT h.*, s.display_name, s.price FROM home_items h "
            "JOIN shop_items s ON s.code = h.item_code WHERE h.item_code = ?",
            (code,)).fetchone()
        item = HomeItem.from_row(row)
        out[item.slot] = item
    return out


def gear_for_budget(conn, budget):
    out = []
    per = budget / 4 if budget else 0
    for slot in ("gpu", "cpu", "cooler", "ram"):
        row = conn.execute(
            "SELECT g.*, s.display_name, s.price FROM gear_stats g "
            "JOIN shop_items s ON s.code = g.item_code "
            "WHERE g.slot = ? AND s.price <= ? ORDER BY s.price DESC LIMIT 1",
            (slot, per)).fetchone()
        if row:
            out.append(GearItem.from_row(row))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=20_000)
    ap.add_argument("--seed", type=int, default=606)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    conn = seed_db()

    setups = [
        ("no house at all", []),
        ("Drive Bay + basic", ["DRIVEBAY", "FOAM", "SILICA"]),
        ("Mini-ITX + good", ["MINIITX", "THERMALPAD", "PASTE"]),
        ("Full Tower + premium", ["FULLTOWER", "LIQUIDBED", "NITRO"]),
    ]

    # ---- 1. the chance curve ----
    print("\n" + "=" * 72)
    print("SLEEP CHANCE")
    print("=" * 72)
    print(f"  {'setup':<26}{'1st':>7}{'10th':>8}{'30th':>8}{'100th':>8}")
    print("  " + "-" * 58)
    for label, codes in setups:
        home = home_from(conn, codes)
        cells = "".join(
            f"{combat.sleep_chance(home, n) * 100:>7.0f}%" for n in (0, 10, 30, 100)
        )
        print(f"  {label:<26}{cells}")
    print(f"\n  floor {config.SLEEP_BASE_CHANCE * 100:.0f}% · "
          f"ceiling {config.SLEEP_MAX_CHANCE * 100:.0f}% · "
          f"rest cap +{config.SLEEP_REST_CAP * 100:.0f}% at "
          f"{int(config.SLEEP_REST_CAP / config.SLEEP_REST_PER_SLEEP)} sleeps")

    # ---- 2. time to cap ----
    print("\n" + "=" * 72)
    print(f"NIGHTS TO REACH THE +{config.SLEEP_BONUS_CAP} CAP "
          f"({config.SLEEP_COOLDOWN // 3600}h cooldown)")
    print("=" * 72)
    print(f"  {'setup':<26}{'median':>9}{'p10':>7}{'p90':>7}{'≈ days':>9}")
    print("  " + "-" * 58)
    for label, codes in setups:
        home = home_from(conn, codes)
        nights = []
        for _ in range(2000):
            bonus, n = 0, 0
            while bonus < config.SLEEP_BONUS_CAP and n < 100_000:
                if rng.random() < combat.sleep_chance(home, n):
                    bonus += 1
                n += 1
            nights.append(n)
        nights.sort()
        med = statistics.median(nights)
        print(f"  {label:<26}{med:>9.0f}{nights[len(nights)//10]:>7}"
              f"{nights[9*len(nights)//10]:>7}"
              f"{med * config.SLEEP_COOLDOWN / 86400:>9.0f}")

    # ---- 3. does a maxed bonus break duels? ----
    print("\n" + "=" * 72)
    print("DUEL IMPACT OF A MAXED SLEEP BONUS")
    print("=" * 72)
    oc = PlayerClass.from_row(
        conn.execute("SELECT * FROM classes WHERE class_id='overclocker'").fetchone())
    dragon = PetSpecies.from_row(
        conn.execute("SELECT * FROM pet_species WHERE species_id='thermal_dragon'"
                     ).fetchone())
    gear = gear_for_budget(conn, 10_000)
    player = Player(user_id=1, level=15)

    def pet_with(bonus_t, bonus_c, bonus_b):
        return Pet(pet_id=1, user_id=1, species_id=dragon.species_id,
                   level=config.PET_MAX_LEVEL, species=dragon,
                   bonus_thermals=bonus_t, bonus_clock=bonus_c,
                   bonus_bandwidth=bonus_b)

    baseline = combat.compute_stats(player, oc, gear, None)
    variants = [
        ("no pet", None),
        ("maxed pet, no sleep", pet_with(0, 0, 0)),
        ("maxed pet, +12 all THRM", pet_with(12, 0, 0)),
        ("maxed pet, +12 all CLK", pet_with(0, 12, 0)),
        ("maxed pet, +12 spread", pet_with(4, 4, 4)),
    ]
    print(f"  {'build':<28}{'POWER':>7}{'HP':>6}{'crit':>7}{'BW':>6}"
          f"{'win% vs no pet':>17}")
    print("  " + "-" * 71)
    for label, pet in variants:
        st = combat.compute_stats(player, oc, gear, pet)
        wins = sum(1 for _ in range(args.runs)
                   if combat.resolve_duel(st, baseline, rng).winner == "a")
        print(f"  {label:<28}{st.power:>7}{st.hp:>6}{st.crit_chance:>6.0f}%"
              f"{st.bandwidth:>6}{100 * wins / args.runs:>16.1f}%")
    print("\n  POWER is identical across every row -- sleep cannot grant it.")

    # ---- 4. which stat does each species trend toward? ----
    print("\n" + "=" * 72)
    print("BONUS DISTRIBUTION BY SPECIES (1,000 successful sleeps each)")
    print("=" * 72)
    print(f"  {'species':<24}{'THRM':>8}{'CLK':>8}{'BW':>8}")
    print("  " + "-" * 48)
    for row in conn.execute("SELECT * FROM pet_species ORDER BY sort_order"):
        sp = PetSpecies.from_row(row)
        tally = {s: 0 for s in config.SLEEP_BONUS_STATS}
        for _ in range(1000):
            tally[combat.pick_sleep_stat(sp, rng)] += 1
        print(f"  {sp.name:<24}{tally['thermals'] / 10:>7.0f}%"
              f"{tally['clock'] / 10:>7.0f}%{tally['bandwidth'] / 10:>7.0f}%")
    print()


if __name__ == "__main__":
    main()
