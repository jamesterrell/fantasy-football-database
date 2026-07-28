"""Orchestration: name in, populated database out."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable

from . import athletes, db, gamelog, scoring
from .espn import ESPNClient

log = logging.getLogger(__name__)


def build_player(
    conn: sqlite3.Connection,
    client: ESPNClient,
    name_or_id: str,
    seasons: Iterable[int] | None = None,
    force: bool = False,
) -> dict:
    """Load one player's full career of game logs.

    `force=True` bypasses the raw JSON archive and re-fetches from ESPN, which
    is what you want for the in-progress season.
    """
    athlete_id = athletes.resolve_athlete_id(client, name_or_id, force=force)
    profile = athletes.fetch_profile(client, athlete_id, force=force)
    db.upsert_athlete(conn, profile)

    name = profile.get("display_name") or athlete_id
    season_list = sorted(seasons) if seasons else gamelog.discover_seasons(client, athlete_id, force)
    if not season_list:
        log.warning("no seasons found for %s (%s)", name, athlete_id)

    log.info(
        "%s (%s, %s) - seasons %s",
        name,
        athlete_id,
        profile.get("position_abbr") or "?",
        season_list or "none",
    )

    total_games = 0
    per_season: dict[int, int] = {}
    for season in season_list:
        payload = gamelog.fetch_gamelog(client, athlete_id, season=season, force=force)
        games, player_games = gamelog.parse_gamelog(athlete_id, season, payload)
        if not player_games:
            log.info("  %s: no games", season)
            db.record_sync(conn, athlete_id, season, 0)
            per_season[season] = 0
            continue

        for row in player_games:
            row.update(scoring.all_formats(row["_stats"]))

        db.upsert_games(conn, games)
        n = db.upsert_player_games(conn, player_games)
        db.record_sync(conn, athlete_id, season, n)
        per_season[season] = n
        total_games += n
        log.info("  %s: %d games", season, n)

    if season_list and not total_games:
        # ESPN lists the seasons but serves no game log for some athletes
        # (retired players in particular). Nothing to parse around - the data
        # is simply absent upstream, and it should not pass silently.
        log.warning(
            "%s (%s): ESPN lists seasons %s but returned no game logs for any of them",
            name,
            athlete_id,
            season_list,
        )

    db.rebuild_views(conn)
    return {
        "athlete_id": athlete_id,
        "name": name,
        "position": profile.get("position_abbr"),
        "seasons": per_season,
        "games": total_games,
    }


def build_players(
    conn: sqlite3.Connection,
    client: ESPNClient,
    names: Iterable[str],
    seasons: Iterable[int] | None = None,
    force: bool = False,
) -> tuple[list[dict], list[tuple[str, str]]]:
    """Load several players. Returns (summaries, failures) - one bad name will
    not abort a long run."""
    summaries: list[dict] = []
    failures: list[tuple[str, str]] = []
    for name in names:
        try:
            summaries.append(build_player(conn, client, name, seasons=seasons, force=force))
        except Exception as exc:  # noqa: BLE001 - a batch load should survive one bad player
            log.error("failed to load %s: %s", name, exc)
            failures.append((name, str(exc)))
    return summaries, failures
