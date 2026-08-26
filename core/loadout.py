"""One place that assembles a player's full combat loadout.

Before Phase 4, four separate call sites (dungeon x2, duel, build) each repeated
the same player -> class -> gear -> compute_stats block. Adding pets would have
made that five copies of the same five lines, and the first time one of them
drifted, duels and dungeons would silently disagree about someone's stats.

Everything that needs a stat sheet goes through build_loadout().
"""

from dataclasses import dataclass

import config
from core import combat


@dataclass
class Loadout:
    player: object
    player_class: object
    gear: dict           # slot -> GearItem
    pet: object
    stats: object        # combat.CombatStats

    @property
    def ram_multiplier(self) -> float:
        """Combined $RAM bonus from prestige and the active pet."""
        return (
            combat.prestige_ram_multiplier(self.player.prestige)
            * combat.pet_ram_multiplier(self.pet)
        )


async def build_loadout(repo, user_id: int) -> Loadout:
    player = await repo.get_player(user_id)
    player_class = (
        await repo.get_class(player.class_id) if player.class_id else None
    )
    gear = await repo.get_equipped_map(user_id)
    pet = await repo.get_active_pet(user_id)
    stats = combat.compute_stats(player, player_class, gear.values(), pet)
    return Loadout(player=player, player_class=player_class, gear=gear,
                   pet=pet, stats=stats)


async def grant_pet_xp(repo, user_id: int, player_xp: int):
    """Feed the active pet its share of XP the player just earned."""
    return await repo.grant_pet_xp(
        user_id, player_xp, config.PET_XP_SHARE,
        config.pet_xp_for_level, config.PET_MAX_LEVEL,
    )
