"""Orchestration: name in, populated database out."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable

from . import athletes, config, db, gamelog, rosters, scoring, teamdefense
from .espn import ESPNClient

log = logging.getLogger(__name__)


def build_player(
    conn: sqlite3.Connection,
    client: ESPNClient,
    name_or_id: str,
    seasons: Iterable[int] | None = None,
    force: bool = False,
    profile: dict | None = None,
) -> dict:
    """Load one player's full career of game logs.

    `force=True` bypasses the raw JSON archive and re-fetches from ESPN, which
    is what you want for the in-progress season. Pass `profile` when the caller
    already has the athlete's details (a roster load does) to skip two requests.
    """
    if profile is None:
        athlete_id = athletes.resolve_athlete_id(client, name_or_id, force=force)
        profile = athletes.fetch_profile(client, athlete_id, force=force)
    else:
        athlete_id = profile["athlete_id"]
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


def build_team_defense(
    conn: sqlite3.Connection,
    client: ESPNClient,
    season: int,
    season_types: Iterable[int] = (config.SEASON_TYPE_REGULAR,),
    teams: Iterable[str] | None = None,
    force: bool = False,
) -> dict:
    """Load every team's defensive game log for one season.

    Each event is fetched once even though two teams' schedules list it, and
    both competitors' stat lines are read together - a defense's allowed
    numbers are the opposing offense's own numbers.
    """
    all_teams = rosters.fetch_teams(client, force=force)
    db.upsert_teams(conn, all_teams)

    wanted = {t.upper() for t in teams} if teams else None
    selected = [
        t
        for t in all_teams
        if wanted is None
        or (t["abbreviation"] or "").upper() in wanted
        or t["team_id"] in wanted
    ]
    if wanted and not selected:
        raise ValueError(f"no NFL team matched {sorted(wanted)}")

    # event_id -> matchup, deduped across the schedules that mention it.
    matchups: dict[str, dict] = {}
    for team in selected:
        for season_type in season_types:
            payload = teamdefense.fetch_schedule(
                client, team["team_id"], season, season_type, force=force
            )
            for matchup in teamdefense.parse_schedule(payload):
                matchups.setdefault(matchup["event_id"], matchup)

    log.info("%s: %d events across %d teams", season, len(matchups), len(selected))

    db.upsert_games(conn, [teamdefense.game_row(m) for m in matchups.values()])

    rows: list[dict] = []
    skipped: list[str] = []
    for matchup in matchups.values():
        event_rows = teamdefense.build_event_rows(client, matchup, force=force)
        if not event_rows:
            skipped.append(matchup["event_id"])
            continue
        rows.extend(event_rows)

    loaded = db.upsert_team_defense_games(conn, rows)
    db.rebuild_views(conn)

    if skipped:
        log.warning("%d events had no box score: %s", len(skipped), ", ".join(skipped[:10]))

    return {
        "season": season,
        "teams": len(selected),
        "events": len(matchups),
        "rows": loaded,
        "skipped": skipped,
    }
