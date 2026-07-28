"""Fetch and parse ESPN athlete game logs into tidy rows.

Payload shape (site.web.api .../athletes/{id}/gamelog?season=YYYY):

    names[]        stat keys, index-aligned with every event's stats[]
    labels[]       the column headers ESPN renders (not unique - "YDS" twice)
    events{}       event_id -> game metadata (date, week, opponent, score...)
    seasonTypes[]  one entry per regular season / postseason
        categories[].events[] -> {eventId, stats[]}
    filters[]      includes the full list of seasons this athlete has played

The `names` array is position-dependent (a QB's differs from a RB's) and shifts
as ESPN adds stats, so we never hardcode a column list - the database grows
columns to match whatever keys arrive.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from . import config
from .espn import ESPNClient

log = logging.getLogger(__name__)


def fetch_gamelog(
    client: ESPNClient, athlete_id: str, season: int | None = None, force: bool = False
) -> dict:
    """Raw game log payload. `season=None` returns the athlete's current season."""
    params = {"season": season} if season else None
    key = f"gamelog/{athlete_id}/{season or 'current'}"
    return client.get_json(
        config.GAMELOG_URL.format(athlete_id=athlete_id),
        params=params,
        cache_key=key,
        force=force,
    )


def seasons_from_payload(payload: dict) -> list[int]:
    """Seasons ESPN offers for this athlete, taken from the season filter."""
    for filt in payload.get("filters") or []:
        if filt.get("name") == "season":
            seasons = []
            for opt in filt.get("options") or []:
                try:
                    seasons.append(int(opt["value"]))
                except (KeyError, TypeError, ValueError):
                    continue
            return sorted(seasons)
    return []


def discover_seasons(client: ESPNClient, athlete_id: str, force: bool = False) -> list[int]:
    """Every season the athlete has an NFL game log for."""
    payload = fetch_gamelog(client, athlete_id, season=None, force=force)
    return seasons_from_payload(payload)


def _season_type_id(display_name: str) -> int:
    lowered = display_name.lower()
    if "postseason" in lowered or "playoff" in lowered:
        return config.SEASON_TYPE_POST
    if "preseason" in lowered:
        return config.SEASON_TYPE_PRE
    return config.SEASON_TYPE_REGULAR


# AFC/NFC conference squads, used by the Pro Bowl.
_ALL_STAR_TEAM_IDS = {"31", "32"}


def _is_all_star(team: dict, opponent: dict) -> int:
    """1 for exhibition (Pro Bowl) games, which ESPN files under Postseason.

    They carry a week number and real-looking stats but are not NFL games, so
    they would otherwise corrupt week-based and postseason features.
    """
    if team.get("isAllStar") or opponent.get("isAllStar"):
        return 1
    ids = {str(team.get("id") or ""), str(opponent.get("id") or "")}
    return 1 if ids & _ALL_STAR_TEAM_IDS else 0


def _to_number(value: Any) -> float | None:
    """Parse an ESPN stat cell. '-' means the stat did not apply to that game."""
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if text in ("", "-", "--"):
        return None
    try:
        return float(text)
    except ValueError:
        # Composite cells such as a kicker's "3/4" keep their raw form in
        # raw_stats; there is no single number to store here.
        return None


def parse_gamelog(athlete_id: str, season: int, payload: dict) -> tuple[list[dict], list[dict]]:
    """Return (game rows, player-game rows) for one athlete-season payload."""
    names: list[str] = payload.get("names") or []
    events_meta: dict[str, dict] = payload.get("events") or {}
    if not names or not events_meta:
        return [], []

    games: dict[str, dict] = {}
    player_games: list[dict] = []
    seen: set[str] = set()

    for season_type in payload.get("seasonTypes") or []:
        type_name = season_type.get("displayName") or ""
        type_id = _season_type_id(type_name)

        for category in season_type.get("categories") or []:
            # splitType categories are aggregates (by opponent, by month, ...);
            # only the per-event split holds one row per game.
            if category.get("type") != "event":
                continue

            for entry in category.get("events") or []:
                event_id = str(entry.get("eventId") or "")
                meta = events_meta.get(event_id)
                if not event_id or meta is None or event_id in seen:
                    continue
                seen.add(event_id)

                raw_stats = dict(zip(names, entry.get("stats") or []))
                team = meta.get("team") or {}
                opponent = meta.get("opponent") or {}
                home_id = str(meta.get("homeTeamId") or "") or None
                team_id = str(team.get("id") or "") or None
                is_home = (meta.get("atVs") == "vs") if meta.get("atVs") else (team_id == home_id)
                is_all_star = _is_all_star(team, opponent)

                home_score = _to_number(meta.get("homeTeamScore"))
                away_score = _to_number(meta.get("awayTeamScore"))
                team_score, opp_score = (
                    (home_score, away_score) if is_home else (away_score, home_score)
                )

                games[event_id] = {
                    "event_id": event_id,
                    "season": season,
                    "season_type": type_id,
                    "season_type_name": type_name,
                    "week": meta.get("week"),
                    "game_date": meta.get("gameDate"),
                    "home_team_id": home_id,
                    "away_team_id": str(meta.get("awayTeamId") or "") or None,
                    "home_score": home_score,
                    "away_score": away_score,
                    "score": meta.get("score"),
                    "is_all_star": is_all_star,
                }

                row = {
                    "athlete_id": str(athlete_id),
                    "event_id": event_id,
                    "season": season,
                    "season_type": type_id,
                    "week": meta.get("week"),
                    "game_date": (meta.get("gameDate") or "")[:10] or None,
                    "team_id": team_id,
                    "team_abbr": team.get("abbreviation"),
                    "opponent_id": str(opponent.get("id") or "") or None,
                    "opponent_abbr": opponent.get("abbreviation"),
                    "home_away": "home" if is_home else "away",
                    "result": meta.get("gameResult"),
                    "team_score": team_score,
                    "opponent_score": opp_score,
                    "is_all_star": is_all_star,
                    "raw_stats": json.dumps(raw_stats),
                    "_stats": {k: _to_number(v) for k, v in raw_stats.items()},
                }
                player_games.append(row)

    player_games.sort(key=lambda r: (r["season_type"], r["game_date"] or ""))
    return list(games.values()), player_games
