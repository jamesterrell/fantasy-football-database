"""Team defense game logs - one stat line per team per game.

Two endpoints are involved. The team schedule lists a season's events with the
matchup context (week, opponent, final score); the per-competitor statistics
endpoint returns one team's complete stat line for one of those events, split
into categories:

    splits.categories[] -> {name: "defensive", stats: [{name, value, ...}]}

Only the defensive categories are materialised into columns. The rest of the
payload still matters, because a defense's *allowed* numbers are just the
opposing offense's own numbers - so both competitors are fetched for every
event and each one's offensive line supplies the other's `*_allowed` columns.

ESPN does publish `defensive.pointsAllowed` and `defensive.yardsAllowed`, but
they are zero-filled before roughly 2015 and disagree with the opponent's own
total in some later games, so they are kept as raw stat columns and the
authoritative `points_allowed` / `yards_allowed` are derived here instead.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from . import config
from .espn import ESPNClient, ESPNError

log = logging.getLogger(__name__)

# Categories materialised into their own columns. Their stat keys do not
# overlap, so they merge into one flat namespace the way a game log does.
DEFENSE_CATEGORIES = ("defensive", "defensiveInterceptions")

# A defense's "allowed" line is the opposing offense's own line. Maps
# (category, stat) on the opponent's stat sheet -> column on this defense row.
ALLOWED_FROM_OPPONENT: dict[tuple[str, str], str] = {
    ("passing", "totalYards"): "yards_allowed",
    ("passing", "netPassingYards"): "pass_yards_allowed",
    ("passing", "passingAttempts"): "pass_attempts_allowed",
    ("passing", "completions"): "completions_allowed",
    ("rushing", "rushingYards"): "rush_yards_allowed",
    ("rushing", "rushingAttempts"): "rush_attempts_allowed",
    ("scoring", "passingTouchdowns"): "pass_tds_allowed",
    ("scoring", "rushingTouchdowns"): "rush_tds_allowed",
    ("scoring", "totalTouchdowns"): "tds_allowed",
    ("miscellaneous", "totalPlays"): "plays_allowed",
    ("miscellaneous", "firstDowns"): "first_downs_allowed",
    ("miscellaneous", "thirdDownAttempts"): "third_down_att_allowed",
    ("miscellaneous", "thirdDownConvs"): "third_down_conv_allowed",
    ("miscellaneous", "redzoneAttempts"): "redzone_att_allowed",
    ("miscellaneous", "redzoneTouchdowns"): "redzone_tds_allowed",
    ("miscellaneous", "possessionTimeSeconds"): "possession_seconds_allowed",
    # The opponent giving the ball away is this defense taking it away.
    ("miscellaneous", "totalGiveaways"): "turnovers_forced",
}

# Takeaway components live outside the defensive categories.
TAKEAWAYS_FROM_OWN: dict[tuple[str, str], str] = {
    ("returning", "fumbleRecoveries"): "fumbles_recovered",
    ("miscellaneous", "totalTakeaways"): "takeaways",
}

# AFC/NFC conference squads, used by the Pro Bowl - same rule game logs apply.
_ALL_STAR_TEAM_IDS = {"31", "32"}


def _to_number(value: Any) -> float | None:
    """Coerce a core-API stat value to a float, or None when absent."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if text in ("", "-", "--"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


# ------------------------------------------------------------------ schedule


def fetch_schedule(
    client: ESPNClient,
    team_id: str,
    season: int,
    season_type: int = config.SEASON_TYPE_REGULAR,
    force: bool = False,
) -> dict:
    """One team's schedule for a season and season type."""
    return client.get_json(
        config.TEAM_SCHEDULE_URL.format(team_id=team_id),
        params={"season": season, "seasontype": season_type},
        cache_key=f"team_schedule/{team_id}/{season}_{season_type}",
        force=force,
    )


def parse_schedule(payload: dict) -> list[dict]:
    """Matchups from a schedule payload, one per completed event.

    Events without a final score have not been played yet and carry no stats,
    so they are dropped rather than stored as empty rows.
    """
    matchups = []
    for event in payload.get("events") or []:
        event_id = str(event.get("id") or "")
        competitions = event.get("competitions") or []
        if not event_id or not competitions:
            continue

        competition = competitions[0]
        competitors = competition.get("competitors") or []
        if len(competitors) != 2:
            continue

        sides = []
        for competitor in competitors:
            team = competitor.get("team") or {}
            team_id = str(competitor.get("id") or team.get("id") or "")
            if not team_id:
                break
            sides.append(
                {
                    "team_id": team_id,
                    "team_abbr": team.get("abbreviation"),
                    "home_away": competitor.get("homeAway"),
                    "score": _to_number((competitor.get("score") or {}).get("value")),
                }
            )
        if len(sides) != 2 or any(s["score"] is None for s in sides):
            continue  # not played yet, or an unusable record

        season_type = event.get("seasonType") or {}
        week = event.get("week") or {}
        home = next((s for s in sides if s["home_away"] == "home"), sides[0])
        away = next((s for s in sides if s is not home), sides[1])
        is_all_star = int(bool({s["team_id"] for s in sides} & _ALL_STAR_TEAM_IDS))

        matchups.append(
            {
                "event_id": event_id,
                "season": (event.get("season") or {}).get("year"),
                "season_type": season_type.get("type"),
                "season_type_name": season_type.get("name"),
                "week": week.get("number"),
                "game_date": event.get("date"),
                "is_all_star": is_all_star,
                "home": home,
                "away": away,
            }
        )
    return matchups


def game_row(matchup: dict) -> dict:
    """A `games` row, so team defense rows have an event to reference."""
    return {
        "event_id": matchup["event_id"],
        "season": matchup["season"],
        "season_type": matchup["season_type"],
        "season_type_name": matchup["season_type_name"],
        "week": matchup["week"],
        "game_date": matchup["game_date"],
        "home_team_id": matchup["home"]["team_id"],
        "away_team_id": matchup["away"]["team_id"],
        "home_score": matchup["home"]["score"],
        "away_score": matchup["away"]["score"],
        "score": f"{matchup['away']['score']:.0f}-{matchup['home']['score']:.0f}",
        "is_all_star": matchup["is_all_star"],
    }


# ------------------------------------------------------------- team box score


def fetch_competitor_stats(
    client: ESPNClient, event_id: str, team_id: str, force: bool = False
) -> dict | None:
    """One team's stat line for one event. None when ESPN has no box score."""
    try:
        return client.get_json(
            config.COMPETITOR_STATS_URL.format(event_id=event_id, team_id=team_id),
            cache_key=f"team_game_stats/{event_id}/{team_id}",
            force=force,
        )
    except ESPNError as exc:
        log.warning("no box score for event %s team %s: %s", event_id, team_id, exc)
        return None


def parse_competitor_stats(payload: dict | None) -> dict[str, dict[str, float | None]]:
    """Flatten a statistics payload into {category: {stat_name: value}}."""
    if not payload:
        return {}
    categories: dict[str, dict[str, float | None]] = {}
    for category in (payload.get("splits") or {}).get("categories") or []:
        name = category.get("name")
        if not name:
            continue
        categories[name] = {
            stat["name"]: _to_number(stat.get("value"))
            for stat in category.get("stats") or []
            if stat.get("name")
        }
    return categories


def _lookup(categories: dict[str, dict[str, float | None]], key: tuple[str, str]) -> float | None:
    return (categories.get(key[0]) or {}).get(key[1])


def build_defense_row(
    matchup: dict,
    side: dict,
    opponent: dict,
    own_stats: dict[str, dict[str, float | None]],
    opponent_stats: dict[str, dict[str, float | None]],
) -> dict:
    """One team's defensive line for one game.

    `_stats` holds the defensive stat keys destined for their own columns; the
    `*_allowed` columns come from the opposing offense, and `points_allowed`
    from the final score, which is the one number always present.
    """
    defense_stats: dict[str, float | None] = {}
    for category in DEFENSE_CATEGORIES:
        defense_stats.update(own_stats.get(category) or {})

    row = {
        "team_id": side["team_id"],
        "event_id": matchup["event_id"],
        "season": matchup["season"],
        "season_type": matchup["season_type"],
        "week": matchup["week"],
        "game_date": (matchup["game_date"] or "")[:10] or None,
        "team_abbr": side["team_abbr"],
        "opponent_id": opponent["team_id"],
        "opponent_abbr": opponent["team_abbr"],
        "home_away": side["home_away"],
        "result": _result(side["score"], opponent["score"]),
        "team_score": side["score"],
        "opponent_score": opponent["score"],
        "is_all_star": matchup["is_all_star"],
        "points_allowed": opponent["score"],
        "raw_stats": json.dumps(own_stats),
        "_stats": defense_stats,
    }

    for key, column in ALLOWED_FROM_OPPONENT.items():
        row[column] = _lookup(opponent_stats, key)
    for key, column in TAKEAWAYS_FROM_OWN.items():
        row[column] = _lookup(own_stats, key)
    return row


def _result(team_score: float | None, opponent_score: float | None) -> str | None:
    if team_score is None or opponent_score is None:
        return None
    if team_score > opponent_score:
        return "W"
    if team_score < opponent_score:
        return "L"
    return "T"


def build_event_rows(
    client: ESPNClient, matchup: dict, force: bool = False
) -> list[dict]:
    """Both defensive rows for one event. Empty when ESPN has no box score."""
    sides = [matchup["home"], matchup["away"]]
    stats_by_team = {
        side["team_id"]: parse_competitor_stats(
            fetch_competitor_stats(client, matchup["event_id"], side["team_id"], force=force)
        )
        for side in sides
    }
    if not any(stats_by_team.values()):
        return []

    return [
        build_defense_row(
            matchup,
            side,
            other,
            stats_by_team[side["team_id"]],
            stats_by_team[other["team_id"]],
        )
        for side, other in ((sides[0], sides[1]), (sides[1], sides[0]))
    ]
