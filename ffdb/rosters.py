"""Team rosters - the candidate pool for ranking.

ESPN's roster endpoint returns every player on a team together with position and
full biographical detail, so a candidate list costs 32 requests instead of one
profile lookup per player.

Note this yields *currently rostered* players. A player who produced in a past
season but is now unsigned will not appear - which happens to match what the
game log endpoint will serve anyway (see README, "Data quality notes").
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from . import config
from .espn import ESPNClient

log = logging.getLogger(__name__)

# Positions with fantasy scoring rules implemented in scoring.py. Kickers and
# team defenses need their own rules and are deliberately out of scope.
FANTASY_POSITIONS = ("QB", "RB", "WR", "TE")


def fetch_teams(client: ESPNClient, force: bool = False) -> list[dict]:
    """All 32 NFL teams."""
    payload = client.get_json(config.TEAMS_URL, cache_key="teams/index", force=force)
    teams = []
    for sport in payload.get("sports") or []:
        for league in sport.get("leagues") or []:
            for entry in league.get("teams") or []:
                team = entry.get("team") or {}
                if team.get("id"):
                    teams.append(
                        {
                            "team_id": str(team["id"]),
                            "abbreviation": team.get("abbreviation"),
                            "display_name": team.get("displayName"),
                            "location": team.get("location"),
                            "name": team.get("name"),
                        }
                    )
    return teams


def _profile_from_roster(athlete: dict, team: dict) -> dict:
    """Reshape a roster entry into an `athletes` row.

    Same shape as athletes.fetch_profile, built without the extra requests.
    """
    position = athlete.get("position") or {}
    status = athlete.get("status") or {}
    birth_place = athlete.get("birthPlace") or {}
    experience = athlete.get("experience") or {}

    def _int(value):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None

    return {
        "athlete_id": str(athlete["id"]),
        "display_name": athlete.get("displayName") or athlete.get("fullName"),
        "first_name": athlete.get("firstName"),
        "last_name": athlete.get("lastName"),
        "position_abbr": position.get("abbreviation"),
        "position_name": position.get("displayName"),
        "team_id": team["team_id"],
        "team_abbr": team.get("abbreviation"),
        "jersey": athlete.get("jersey"),
        "height_inches": _int(athlete.get("height")),
        "weight_lbs": _int(athlete.get("weight")),
        "birth_date": (athlete.get("dateOfBirth") or "")[:10] or None,
        "birth_city": birth_place.get("city"),
        "birth_state": birth_place.get("state"),
        "birth_country": birth_place.get("country"),
        "experience_years": experience.get("years"),
        "active": 1 if (status.get("type") or "active") == "active" else 0,
        "status": status.get("name"),
    }


def fetch_roster(client: ESPNClient, team: dict, force: bool = False) -> list[dict]:
    """Every athlete on one team's roster, as `athletes` rows."""
    payload = client.get_json(
        config.ROSTER_URL.format(team_id=team["team_id"]),
        cache_key=f"roster/{team['team_id']}",
        force=force,
    )
    players = []
    for group in payload.get("athletes") or []:
        for athlete in group.get("items") or []:
            if athlete.get("id"):
                players.append(_profile_from_roster(athlete, team))
    return players


def candidate_pool(
    client: ESPNClient,
    positions: Iterable[str] = FANTASY_POSITIONS,
    force: bool = False,
) -> list[dict]:
    """Every rostered player at the given positions, league-wide."""
    wanted = {p.upper() for p in positions}
    teams = fetch_teams(client, force=force)
    log.info("fetching rosters for %d teams", len(teams))

    pool: dict[str, dict] = {}
    for team in teams:
        roster = fetch_roster(client, team, force=force)
        kept = [p for p in roster if (p.get("position_abbr") or "").upper() in wanted]
        for player in kept:
            pool[player["athlete_id"]] = player
        log.debug("%s: %d of %d players kept", team["abbreviation"], len(kept), len(roster))

    log.info("candidate pool: %d players at %s", len(pool), ", ".join(sorted(wanted)))
    return list(pool.values())
