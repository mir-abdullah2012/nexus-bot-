"""Measure what pets actually do to dungeon and duel outcomes.

    python scripts/simulate_pets.py

The claim being tested: a maxed legendary pet is worth roughly 15% of a geared
player's POWER -- a real edge, never a replacement for gear.
"""

import argparse
import random
import sqlite3
import statistics
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import config                                                       # noqa: E402
from core import combat, migrations                                 # noqa: E402
from core.models import Dungeon, GearItem, Pet, PetSpecies, Player, PlayerClass  # noqa: E402


def seed_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    migrations.apply_sync(conn, verbose=False)
    return conn


def gear_for_budget(conn, budget):
    out = []
    per_slot = budget / 4 if budget else 0
    for slot in ("gpu", "cpu", "cooler", "ram"):
        row = conn.execute(
            "SELECT g.*, s.display_name, s.price FROM gear_stats g "
            "JOIN shop_items s ON s.code = g.item_code "
            "WHERE g.slot = ? AND s.price <= ? ORDER BY s.price DESC LIMIT 1",
            (slot, per_slot)).fetchone()
        if row:
            out.append(GearItem.from_row(row))
    return out


def make_pet(species: PetSpecies, level: int) -> Pet:
    return Pet(pet_id=1, user_id=1, species_id=species.species_id,
               level=level, xp=0, hatched_at=0, species=species)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=20_000)
    ap.add_argument("--seed", type=int, default=555)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    conn = seed_db()
    species = [PetSpecies.from_row(r) for r in
               conn.execute("SELECT * FROM pet_species ORDER BY sort_order")]
    oc = PlayerClass.from_row(
        conn.execute("SELECT * FROM classes WHERE class_id='overclocker'").fetchone())

    # ---- 1. hatch odds ----
    print("\n" + "=" * 72)
    print("HATCH ODDS")
    print("=" * 72)
    total = sum(s.hatch_weight for s in species)
    for s in species:
        print(f"  {s.emoji} {s.name:<22}{s.rarity:<11}"
              f"{100 * s.hatch_weight / total:>6.2f}%")
    print(f"  {'':<24}{'':<11}{'-' * 7}\n  {'':<24}{'total':<11}"
          f"{100 * sum(s.hatch_weight for s in species) / total:>6.2f}%")

    # ---- 2. stat contribution at level 1 vs cap ----
    print("\n" + "=" * 72)
    print("STAT CONTRIBUTION  (level 1 -> level 25)")
    print("=" * 72)
    print(f"  {'species':<24}{'lvl 1 P/T/C/B':>18}{'lvl 25 P/T/C/B':>20}{'total':>8}")
    print("  " + "-" * 70)
    for s in species:
        lo = combat.pet_contribution(make_pet(s, 1))
        hi = combat.pet_contribution(make_pet(s, config.PET_MAX_LEVEL))
        lo_s = f"{lo['power']}/{lo['thermals']}/{lo['clock']}/{lo['bandwidth']}"
        hi_s = f"{hi['power']}/{hi['thermals']}/{hi['clock']}/{hi['bandwidth']}"
        print(f"  {s.name:<24}{lo_s:>18}{hi_s:>20}{sum(hi.values()):>8}")

    # ---- 3. impact on a geared player's sheet ----
    print("\n" + "=" * 72)
    print("IMPACT ON A GEARED BUILD  (Overclocker lvl 15, ~10k gear)")
    print("=" * 72)
    gear = gear_for_budget(conn, 10_000)
    player = Player(user_id=1, level=15)
    base = combat.compute_stats(player, oc, gear, None)
    print(f"  {'pet':<26}{'POWER':>7}{'HP':>6}{'crit':>7}{'BW':>6}{'POWER +%':>11}")
    print("  " + "-" * 64)
    print(f"  {'(none)':<26}{base.power:>7}{base.hp:>6}"
          f"{base.crit_chance:>6.0f}%{base.bandwidth:>6}{'—':>11}")
    for s in species:
        st = combat.compute_stats(player, oc, gear,
                                  make_pet(s, config.PET_MAX_LEVEL))
        pct = 100 * (st.power - base.power) / base.power if base.power else 0
        print(f"  {s.name + ' @25':<26}{st.power:>7}{st.hp:>6}"
              f"{st.crit_chance:>6.0f}%{st.bandwidth:>6}{pct:>10.1f}%")

    # ---- 4. does it actually change dungeon outcomes? ----
    print("\n" + "=" * 72)
    print("DUNGEON EFFECT  (Crypto Mine Collapse, same build)")
    print("=" * 72)
    dungeon = Dungeon.from_row(
        conn.execute("SELECT * FROM dungeons WHERE dungeon_id='cryptomine'").fetchone())
    loot = [(r["item_code"], r["weight"]) for r in conn.execute(
        "SELECT item_code, weight FROM dungeon_loot WHERE dungeon_id='cryptomine'")]

    print(f"  {'pet':<26}{'wipe%':>8}{'flawless%':>12}{'avg $RAM':>11}")
    print("  " + "-" * 58)
    for label, pet in [("(none)", None)] + [
        (f"{s.name} @25", make_pet(s, config.PET_MAX_LEVEL))
        for s in species if s.rarity in ("common", "legendary")
    ]:
        st = combat.compute_stats(player, oc, gear, pet)
        mult = combat.pet_ram_multiplier(pet)
        outs, rams = [], []
        for _ in range(args.runs):
            r = combat.resolve_run(st, dungeon, oc, loot, 0, rng)
            outs.append(r.outcome)
            rams.append(int(r.ram_earned * mult))
        print(f"  {label:<26}{100 * outs.count('wiped') / len(outs):>7.1f}%"
              f"{100 * outs.count('flawless') / len(outs):>11.1f}%"
              f"{statistics.mean(rams):>11.0f}")

    # ---- 5. duel effect: pet vs no pet, otherwise identical ----
    print("\n" + "=" * 72)
    print("DUEL EFFECT  (identical builds, one side has a pet)")
    print("=" * 72)
    print(f"  {'pet':<26}{'win% vs petless':>18}")
    print("  " + "-" * 46)
    for s in species:
        with_pet = combat.compute_stats(player, oc, gear,
                                        make_pet(s, config.PET_MAX_LEVEL))
        wins = sum(1 for _ in range(args.runs)
                   if combat.resolve_duel(with_pet, base, rng).winner == "a")
        print(f"  {s.name + ' @25':<26}{100 * wins / args.runs:>17.1f}%")

    # ---- 6. how long to max a pet? ----
    print("\n" + "=" * 72)
    print("TIME TO MAX A PET")
    print("=" * 72)
    pet_xp_total = sum(config.pet_xp_for_level(l)
                       for l in range(1, config.PET_MAX_LEVEL))
    player_xp_needed = pet_xp_total / config.PET_XP_SHARE
    msgs = player_xp_needed / config.XP_PER_MESSAGE
    bsod_runs = player_xp_needed / 350
    print(f"  pet xp to reach level {config.PET_MAX_LEVEL}: {pet_xp_total:,}")
    print(f"  player xp needed (at {int(config.PET_XP_SHARE * 100)}% share): "
          f"{player_xp_needed:,.0f}")
    print(f"  ≈ {msgs:,.0f} messages, or {bsod_runs:,.0f} Blue Screen Abyss clears")
    print()


if __name__ == "__main__":
    main()
