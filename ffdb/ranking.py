"""Derive a top-N player list from actual production, then backfill careers.

Three phases, each resumable because every response is archived on disk:

  1. pool     - fetch all 32 rosters, keep QB/RB/WR/TE           (~33 requests)
  2. rank     - load ONE season for each candidate, rank by      (~1 per player)
                fantasy points, store the result
  3. backfill - fetch the full career of the ranked top N        (~1 per season)

Ranking on realised production means the list is a fact about what happened,
not a projection. The trade-off is that it is backward-looking: a 2026 rookie
has no 2025 NFL production and cannot appear, and a breakout is only reflected
after the fact.
"""

from __future__ import annotations

import logging
import sqlite3

from . import db, pipeline, rosters, scoring
from .espn import ESPNClient

log = logging.getLogger(__name__)

DEFAULT_SCORING = "ppr"
REGULAR_SEASON = 2


def load_candidate_season(
    conn: sqlite3.Connection,
    client: ESPNClient,
    season: int,
    positions=rosters.FANTASY_POSITIONS,
    force: bool = False,
) -> dict:
    """Phase 1+2a: build the candidate pool and load one season for each."""
    db.upsert_teams(conn, rosters.fetch_teams(client, force=force))
    pool = rosters.candidate_pool(client, positions=positions, force=force)

    loaded = 0
    empty: list[str] = []
    failed: list[tuple[str, str]] = []
    for i, profile in enumerate(pool, 1):
        name = profile.get("display_name") or profile["athlete_id"]
        try:
            summary = pipeline.build_player(
                conn, client, profile["athlete_id"], seasons=[season],
                force=force, profile=profile,
            )
        except Exception as exc:  # noqa: BLE001 - one bad player must not stop the sweep
            log.error("[%d/%d] %s failed: %s", i, len(pool), name, exc)
            failed.append((name, str(exc)))
            continue

        if summary["games"]:
            loaded += 1
        else:
            empty.append(name)
        if i % 25 == 0:
            log.info("[%d/%d] candidates processed", i, len(pool))

    log.info(
        "season %s loaded: %d players with games, %d with none, %d failed",
        season, loaded, len(empty), len(failed),
    )
    return {"pool": len(pool), "loaded": loaded, "empty": empty, "failed": failed}


def compute_rankings(
    conn: sqlite3.Connection,
    season: int,
    top: int = 200,
    scoring_format: str = DEFAULT_SCORING,
) -> list[dict]:
    """Phase 2b: rank the loaded season by total fantasy points.

    Regular season only - postseason games are not available to every player
    and would reward players on good teams for reasons unrelated to the player.
    """
    if scoring_format not in scoring.FORMATS:
        raise ValueError(f"unknown scoring format {scoring_format!r}; try {list(scoring.FORMATS)}")
    column = f"fp_{scoring_format}"

    rows = conn.execute(
        f"""
        SELECT pg.athlete_id,
               a.position_abbr,
               COUNT(*)                  AS games,
               SUM(pg.{column})          AS points_total,
               AVG(pg.{column})          AS points_per_game
        FROM player_games pg
        JOIN athletes a ON a.athlete_id = pg.athlete_id
        WHERE pg.season = ? AND pg.season_type = ? AND pg.is_all_star = 0
        GROUP BY pg.athlete_id
        ORDER BY points_total DESC
        LIMIT ?
        """,
        (season, REGULAR_SEASON, top),
    ).fetchall()

    per_position: dict[str, int] = {}
    ranked = []
    for i, row in enumerate(rows, 1):
        position = row["position_abbr"] or "?"
        per_position[position] = per_position.get(position, 0) + 1
        ranked.append(
            {
                "season": season,
                "scoring": scoring_format,
                "athlete_id": row["athlete_id"],
                "rank": i,
                "position_rank": per_position[position],
                "position_abbr": position,
                "games": row["games"],
                "points_total": round(row["points_total"] or 0, 2),
                "points_per_game": round(row["points_per_game"] or 0, 2),
            }
        )

    db.replace_rankings(conn, season, scoring_format, ranked)
    log.info("ranked top %d for %s (%s)", len(ranked), season, scoring_format)
    return ranked


def ranked_athlete_ids(
    conn: sqlite3.Connection, season: int, top: int = 200, scoring_format: str = DEFAULT_SCORING
) -> list[str]:
    return [
        row["athlete_id"]
        for row in conn.execute(
            "SELECT athlete_id FROM rankings WHERE season = ? AND scoring = ? "
            "ORDER BY rank LIMIT ?",
            (season, scoring_format, top),
        )
    ]


def backfill_careers(
    conn: sqlite3.Connection,
    client: ESPNClient,
    athlete_ids: list[str],
    force: bool = False,
) -> dict:
    """Phase 3: fetch every season for the given athletes.

    The season already loaded during ranking is served from the archive, so
    this only pays for the seasons that are genuinely new.
    """
    total_games = 0
    failed: list[tuple[str, str]] = []
    for i, athlete_id in enumerate(athlete_ids, 1):
        row = conn.execute(
            "SELECT display_name, position_abbr, team_id, team_abbr, jersey, height_inches, "
            "weight_lbs, birth_date, birth_city, birth_state, birth_country, experience_years, "
            "active, status, first_name, last_name, position_name, athlete_id "
            "FROM athletes WHERE athlete_id = ?",
            (athlete_id,),
        ).fetchone()
        profile = dict(row) if row else None
        try:
            summary = pipeline.build_player(
                conn, client, athlete_id, force=force, profile=profile
            )
            total_games += summary["games"]
            log.info(
                "[%d/%d] %s: %d games across %d seasons",
                i, len(athlete_ids), summary["name"], summary["games"], len(summary["seasons"]),
            )
        except Exception as exc:  # noqa: BLE001
            log.error("[%d/%d] %s failed: %s", i, len(athlete_ids), athlete_id, exc)
            failed.append((athlete_id, str(exc)))

    return {"players": len(athlete_ids), "games": total_games, "failed": failed}


def build_top_n(
    conn: sqlite3.Connection,
    client: ESPNClient,
    season: int,
    top: int = 200,
    scoring_format: str = DEFAULT_SCORING,
    force: bool = False,
) -> dict:
    """All three phases end to end."""
    pool = load_candidate_season(conn, client, season, force=force)
    ranked = compute_rankings(conn, season, top=top, scoring_format=scoring_format)
    backfill = backfill_careers(
        conn, client, [r["athlete_id"] for r in ranked], force=force
    )
    db.rebuild_views(conn)
    return {"pool": pool, "ranked": len(ranked), "backfill": backfill}
