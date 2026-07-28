"""Fantasy point calculation from ESPN stat keys.

Points are derived at load time and stored alongside the raw stats so the
database is directly usable for modelling without re-deriving a target.
Only keys present in a given game log contribute, so the same map works for
every position.
"""

from __future__ import annotations

# Scoring shared by the three common formats; the reception value is the only
# difference between them.
BASE_SCORING: dict[str, float] = {
    "passingYards": 0.04,
    "passingTouchdowns": 4.0,
    "rushingYards": 0.1,
    "rushingTouchdowns": 6.0,
    "receivingYards": 0.1,
    "receivingTouchdowns": 6.0,
    "fumblesLost": -2.0,
    # Return / miscellaneous touchdowns, when ESPN exposes them.
    "kickReturnTouchdowns": 6.0,
    "puntReturnTouchdowns": 6.0,
    "interceptionTouchdowns": 6.0,
    "fumblesTouchdowns": 6.0,
    # Two-point conversions.
    "twoPointRushConvs": 2.0,
    "twoPointRecConvs": 2.0,
    "twoPointPassConvs": 2.0,
}

# Interceptions thrown cost the passer 2 points. The same key means interceptions
# *caught* on a defender's log, so it is only applied to players who threw a pass.
PASSING_INTERCEPTION_PENALTY = -2.0

FORMATS: dict[str, float] = {
    "standard": 0.0,
    "half_ppr": 0.5,
    "ppr": 1.0,
}


def _threw_a_pass(stats: dict[str, float | None]) -> bool:
    return any(stats.get(key) for key in ("passingAttempts", "completions", "passingYards"))


def compute_points(stats: dict[str, float | None], reception_value: float) -> float:
    """Fantasy points for one game. Missing or non-numeric stats count as zero."""
    total = 0.0
    for key, weight in BASE_SCORING.items():
        value = stats.get(key)
        if value:
            total += value * weight

    receptions = stats.get("receptions")
    if receptions:
        total += receptions * reception_value

    interceptions = stats.get("interceptions")
    if interceptions and _threw_a_pass(stats):
        total += interceptions * PASSING_INTERCEPTION_PENALTY

    return round(total, 2)


def all_formats(stats: dict[str, float | None]) -> dict[str, float]:
    """Fantasy points under every supported format, keyed as fp_<format>."""
    return {f"fp_{name}": compute_points(stats, value) for name, value in FORMATS.items()}
