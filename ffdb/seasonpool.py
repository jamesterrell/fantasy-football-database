"""Enumerate the players who actually appeared in a season, from box scores.

`rosters.candidate_pool` only sees *currently* rostered players, so using it to
backfill an earlier season quietly reproduces today's league in the past: a
player who produced in 2021 and was out of the NFL by 2025 never appears. What
survives is the players good enough to still be around, and a model trained on
that sample learns the survivorship rather than the football.

A box score cannot be biased that way. Every player who recorded a stat in a
game is in that game's box score permanently, so the union over a season's box
scores is exactly the set of players who played it - retired, cut and injured
included. One request per event, and the season's events are already in `games`
from the schedule load.

Scoring depends on it too: a player can only score fantasy points by passing,
rushing or receiving, and each of those puts them in the matching box score
category. Enumerating those three categories therefore cannot miss anyone who
scored, which is the guarantee the roster pool could not make.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable

from . import athletes as athletes_mod
from . import config, db, pipeline, rosters
from .espn import ESPNClient

log = logging.getLogger(__name__)

# Box score categories that can produce fantasy points. Defensive, kicking and
# return categories are skipped: a player whose only appearance is there is not
# in scope for the QB/RB/WR/TE scoring rules, and including them would mean
# fetching a profile for every lineman in the league to find that out.
SCORING_CATEGORIES = ("passing", "rushing", "receiving")


def fetch_boxscore(client: ESPNClient, event_id: str, force: bool = False) -> dict:
    """Raw event summary, which carries the box score."""
    return client.get_json(
        config.SUMMARY_URL,
        params={"event": event_id},
        cache_key=f"boxscore/{event_id}",
        force=force,
    )


def athletes_from_boxscore(
    payload: dict, categories: Iterable[str] = SCORING_CATEGORIES
) -> dict[str, str]:
    """athlete_id -> display name for everyone in the given categories."""
    wanted = {c.lower() for c in categories}
    found: dict[str, str] = {}
    for team in payload.get("boxscore", {}).get("players") or []:
        for category in team.get("statistics") or []:
            if (category.get("name") or "").lower() not in wanted:
                continue
            for entry in category.get("athletes") or []:
                athlete = entry.get("athlete") or {}
                athlete_id = str(athlete.get("id") or "")
                if athlete_id:
                    found.setdefault(athlete_id, athlete.get("displayName") or athlete_id)
    return found


def season_event_ids(
    conn: sqlite3.Connection, season: int, season_type: int = config.SEASON_TYPE_REGULAR
) -> list[str]:
    """Played events for a season, from the schedule already in `games`.

    Unplayed games are excluded - there is no box score to read - so this is
    safe to call against a season in progress.
    """
    return [
        row["event_id"]
        for row in conn.execute(
            "SELECT event_id FROM games WHERE season = ? AND season_type = ? "
            "AND is_all_star = 0 AND score IS NOT NULL ORDER BY week, event_id",
            (season, season_type),
        )
    ]


def discover_participants(
    conn: sqlite3.Connection,
    client: ESPNClient,
    seasons: Iterable[int],
    force: bool = False,
) -> dict[str, dict]:
    """Sweep every season's box scores. Returns athlete_id -> {name, seasons}."""
    participants: dict[str, dict] = {}
    for season in sorted(seasons):
        event_ids = season_event_ids(conn, season)
        if not event_ids:
            log.warning(
                "%s: no played events in `games` - load the schedule first "
                "(python -m ffdb schedule --season %s)",
                season, season,
            )
            continue

        before = len(participants)
        missing = 0
        for i, event_id in enumerate(event_ids, 1):
            try:
                payload = fetch_boxscore(client, event_id, force=force)
            except Exception as exc:  # noqa: BLE001 - one bad event must not stop the sweep
                log.error("%s event %s: %s", season, event_id, exc)
                missing += 1
                continue

            found = athletes_from_boxscore(payload)
            if not found:
                log.warning("%s event %s: box score has no offensive players", season, event_id)
                missing += 1
            for athlete_id, name in found.items():
                record = participants.setdefault(athlete_id, {"name": name, "seasons": set()})
                record["seasons"].add(season)
            if i % 50 == 0:
                log.info("%s: %d/%d events scanned", season, i, len(event_ids))

        season_total = sum(1 for r in participants.values() if season in r["seasons"])
        log.info(
            "%s: %d events -> %d players (%d new)%s",
            season, len(event_ids), season_total, len(participants) - before,
            f", {missing} events unreadable" if missing else "",
        )
    return participants


def _profile_for(
    conn: sqlite3.Connection, client: ESPNClient, athlete_id: str, force: bool = False
) -> dict | None:
    """Stored profile if we already know the player's position, else fetch it.

    Position is the only field the filter needs, and a player loaded by an
    earlier run already has it - which spares two requests each for the ~900
    players the 2025 sync brought in.
    """
    if not force:
        row = conn.execute(
            "SELECT * FROM athletes WHERE athlete_id = ? AND position_abbr IS NOT NULL",
            (athlete_id,),
        ).fetchone()
        if row:
            profile = {k: row[k] for k in row.keys() if k != "updated_at"}
            return profile
    try:
        return athletes_mod.fetch_profile(client, athlete_id, force=force)
    except Exception as exc:  # noqa: BLE001
        log.error("profile lookup failed for %s: %s", athlete_id, exc)
        return None


def _synced_seasons(conn: sqlite3.Connection) -> dict[str, set[int]]:
    """athlete_id -> seasons already pulled, so a rerun costs nothing."""
    synced: dict[str, set[int]] = {}
    for row in conn.execute("SELECT athlete_id, season FROM sync_log"):
        synced.setdefault(row["athlete_id"], set()).add(row["season"])
    return synced


def build_seasons(
    conn: sqlite3.Connection,
    client: ESPNClient,
    seasons: Iterable[int],
    positions: Iterable[str] = rosters.FANTASY_POSITIONS,
    force: bool = False,
    resync: bool = False,
) -> dict:
    """Load every fantasy-relevant player who appeared in the given seasons.

    Interrupting this is safe: `sync_log` records each athlete-season as it
    lands, and a rerun skips what is already there unless `resync` is set.
    """
    season_list = sorted(seasons)
    wanted_positions = {p.upper() for p in positions}

    log.info("scanning box scores for %s", ", ".join(str(s) for s in season_list))
    participants = discover_participants(conn, client, season_list, force=force)
    log.info("%d distinct players appeared across %s", len(participants), season_list)

    log.info("resolving positions")
    keep: dict[str, dict] = {}
    skipped_positions: dict[str, int] = {}
    unresolved: list[tuple[str, str]] = []
    for i, (athlete_id, record) in enumerate(participants.items(), 1):
        profile = _profile_for(conn, client, athlete_id, force=force)
        if profile is None:
            # Position unknown means we cannot say whether the player is in
            # scope. Surfaced rather than dropped quietly - a player silently
            # missing from a season is the whole problem this command exists
            # to fix.
            unresolved.append((athlete_id, record["name"]))
            continue
        position = (profile.get("position_abbr") or "?").upper()
        if position not in wanted_positions:
            skipped_positions[position] = skipped_positions.get(position, 0) + 1
            continue
        keep[athlete_id] = {**record, "profile": profile}
        if i % 100 == 0:
            log.info("[%d/%d] profiles resolved, %d kept", i, len(participants), len(keep))

    log.info(
        "%d players at %s; skipped %s",
        len(keep), ", ".join(sorted(wanted_positions)),
        ", ".join(f"{n} {p}" for p, n in sorted(skipped_positions.items(), key=lambda kv: -kv[1]))
        or "none",
    )
    if unresolved:
        log.warning(
            "%d players have no resolvable position and were left out: %s",
            len(unresolved), ", ".join(f"{n} ({a})" for a, n in unresolved[:10]),
        )

    synced = {} if resync else _synced_seasons(conn)
    plan = {
        athlete_id: sorted(record["seasons"] - synced.get(athlete_id, set()))
        for athlete_id, record in keep.items()
    }
    todo = {a: s for a, s in plan.items() if s}
    already = len(plan) - len(todo)
    log.info(
        "%d players need %d athlete-seasons pulled (%d players already complete)",
        len(todo), sum(len(s) for s in todo.values()), already,
    )

    loaded_games = 0
    failed: list[tuple[str, str]] = []
    for i, (athlete_id, athlete_seasons) in enumerate(todo.items(), 1):
        record = keep[athlete_id]
        try:
            summary = pipeline.build_player(
                conn, client, athlete_id, seasons=athlete_seasons,
                force=force, profile=record["profile"], rebuild_views=False,
            )
            loaded_games += summary["games"]
            log.info(
                "[%d/%d] %s: %d games across %s",
                i, len(todo), summary["name"], summary["games"], athlete_seasons,
            )
        except Exception as exc:  # noqa: BLE001 - a long backfill must survive one bad player
            log.error("[%d/%d] %s (%s) failed: %s", i, len(todo), record["name"], athlete_id, exc)
            failed.append((athlete_id, str(exc)))

    db.rebuild_views(conn)
    return {
        "seasons": season_list,
        "discovered": len(participants),
        "kept": len(keep),
        "skipped_positions": skipped_positions,
        "unresolved": unresolved,
        "already_synced": already,
        "pulled": len(todo),
        "games": loaded_games,
        "failed": failed,
    }
