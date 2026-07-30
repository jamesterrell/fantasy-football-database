"""SQLite storage layer.

The stat columns on `player_games` are not fixed: ESPN publishes a different
stat vocabulary per position and extends it over time, so the table grows a
column the first time a new stat key is seen. Every game also keeps the
untouched name -> value mapping in `raw_stats`, so nothing is ever lost to a
parsing decision made today.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Iterable
from typing import Any

from . import config

log = logging.getLogger(__name__)

# Fixed columns on player_games. A stat key colliding with one of these gets a
# "stat_" prefix rather than clobbering it.
PLAYER_GAME_FIXED_COLUMNS = (
    "athlete_id",
    "event_id",
    "season",
    "season_type",
    "week",
    "game_date",
    "team_id",
    "team_abbr",
    "opponent_id",
    "opponent_abbr",
    "home_away",
    "result",
    "team_score",
    "opponent_score",
    "is_all_star",
    "fp_standard",
    "fp_half_ppr",
    "fp_ppr",
    "raw_stats",
    "loaded_at",
)

# Fixed columns on team_defense_games. Everything ending in `_allowed` is the
# opposing offense's own number, carried over so a defense row reads on its own.
TEAM_DEFENSE_FIXED_COLUMNS = (
    "team_id",
    "event_id",
    "season",
    "season_type",
    "week",
    "game_date",
    "team_abbr",
    "opponent_id",
    "opponent_abbr",
    "home_away",
    "result",
    "team_score",
    "opponent_score",
    "is_all_star",
    "points_allowed",
    "yards_allowed",
    "pass_yards_allowed",
    "rush_yards_allowed",
    "pass_attempts_allowed",
    "completions_allowed",
    "rush_attempts_allowed",
    "pass_tds_allowed",
    "rush_tds_allowed",
    "tds_allowed",
    "plays_allowed",
    "first_downs_allowed",
    "third_down_att_allowed",
    "third_down_conv_allowed",
    "redzone_att_allowed",
    "redzone_tds_allowed",
    "possession_seconds_allowed",
    "turnovers_forced",
    "takeaways",
    "fumbles_recovered",
    "raw_stats",
    "loaded_at",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS athletes (
    athlete_id       TEXT PRIMARY KEY,
    display_name     TEXT,
    first_name       TEXT,
    last_name        TEXT,
    position_abbr    TEXT,
    position_name    TEXT,
    team_id          TEXT,
    team_abbr        TEXT,
    jersey           TEXT,
    height_inches    INTEGER,
    weight_lbs       INTEGER,
    birth_date       TEXT,
    birth_city       TEXT,
    birth_state      TEXT,
    birth_country    TEXT,
    experience_years INTEGER,
    active           INTEGER,
    status           TEXT,
    updated_at       TEXT
);

CREATE TABLE IF NOT EXISTS games (
    event_id         TEXT PRIMARY KEY,
    season           INTEGER,
    season_type      INTEGER,
    season_type_name TEXT,
    week             INTEGER,
    game_date        TEXT,
    home_team_id     TEXT,
    away_team_id     TEXT,
    home_score       REAL,
    away_score       REAL,
    score            TEXT,
    is_all_star      INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS player_games (
    athlete_id     TEXT NOT NULL REFERENCES athletes(athlete_id),
    event_id       TEXT NOT NULL REFERENCES games(event_id),
    season         INTEGER,
    season_type    INTEGER,
    week           INTEGER,
    game_date      TEXT,
    team_id        TEXT,
    team_abbr      TEXT,
    opponent_id    TEXT,
    opponent_abbr  TEXT,
    home_away      TEXT,
    result         TEXT,
    team_score     REAL,
    opponent_score REAL,
    is_all_star    INTEGER DEFAULT 0,
    fp_standard    REAL,
    fp_half_ppr    REAL,
    fp_ppr         REAL,
    raw_stats      TEXT,
    loaded_at      TEXT,
    PRIMARY KEY (athlete_id, event_id)
);

-- One row per team per game: what that defense did, and what it gave up.
CREATE TABLE IF NOT EXISTS team_defense_games (
    team_id                    TEXT NOT NULL REFERENCES teams(team_id),
    event_id                   TEXT NOT NULL REFERENCES games(event_id),
    season                     INTEGER,
    season_type                INTEGER,
    week                       INTEGER,
    game_date                  TEXT,
    team_abbr                  TEXT,
    opponent_id                TEXT,
    opponent_abbr              TEXT,
    home_away                  TEXT,
    result                     TEXT,
    team_score                 REAL,
    opponent_score             REAL,
    is_all_star                INTEGER DEFAULT 0,
    points_allowed             REAL,
    yards_allowed              REAL,
    pass_yards_allowed         REAL,
    rush_yards_allowed         REAL,
    pass_attempts_allowed      REAL,
    completions_allowed        REAL,
    rush_attempts_allowed      REAL,
    pass_tds_allowed           REAL,
    rush_tds_allowed           REAL,
    tds_allowed                REAL,
    plays_allowed              REAL,
    first_downs_allowed        REAL,
    third_down_att_allowed     REAL,
    third_down_conv_allowed    REAL,
    redzone_att_allowed        REAL,
    redzone_tds_allowed        REAL,
    possession_seconds_allowed REAL,
    turnovers_forced           REAL,
    takeaways                  REAL,
    fumbles_recovered          REAL,
    raw_stats                  TEXT,
    loaded_at                  TEXT,
    PRIMARY KEY (team_id, event_id)
);

CREATE TABLE IF NOT EXISTS teams (
    team_id      TEXT PRIMARY KEY,
    abbreviation TEXT,
    display_name TEXT,
    location     TEXT,
    name         TEXT
);

-- Derived rankings: who the top N were by actual production in a season.
CREATE TABLE IF NOT EXISTS rankings (
    season          INTEGER NOT NULL,
    scoring         TEXT    NOT NULL,
    athlete_id      TEXT    NOT NULL REFERENCES athletes(athlete_id),
    rank            INTEGER NOT NULL,
    position_rank   INTEGER,
    position_abbr   TEXT,
    games           INTEGER,
    points_total    REAL,
    points_per_game REAL,
    computed_at     TEXT,
    PRIMARY KEY (season, scoring, athlete_id)
);

CREATE TABLE IF NOT EXISTS sync_log (
    athlete_id TEXT NOT NULL,
    season     INTEGER NOT NULL,
    games      INTEGER,
    synced_at  TEXT,
    PRIMARY KEY (athlete_id, season)
);

CREATE INDEX IF NOT EXISTS ix_rankings_season ON rankings(season, scoring, rank);
CREATE INDEX IF NOT EXISTS ix_player_games_season ON player_games(season, season_type);
CREATE INDEX IF NOT EXISTS ix_player_games_athlete ON player_games(athlete_id, game_date);
CREATE INDEX IF NOT EXISTS ix_games_season ON games(season, week);
CREATE INDEX IF NOT EXISTS ix_team_defense_event ON team_defense_games(event_id);
CREATE INDEX IF NOT EXISTS ix_team_defense_season ON team_defense_games(season, week);
CREATE INDEX IF NOT EXISTS ix_team_defense_opponent ON team_defense_games(opponent_id, season);
"""

# Kept apart from SCHEMA because _migrate_stat_catalog rebuilds this table on
# databases created before it was keyed by table as well as stat name.
STAT_CATALOG_DDL = """
CREATE TABLE IF NOT EXISTS stat_catalog (
    table_name  TEXT NOT NULL DEFAULT 'player_games',
    stat_name   TEXT NOT NULL,
    column_name TEXT NOT NULL,
    first_seen  TEXT,
    PRIMARY KEY (table_name, stat_name)
);
"""

SCHEMA += STAT_CATALOG_DDL


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path | str = config.DB_PATH) -> sqlite3.Connection:
    config.ensure_dirs()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


# Fixed columns added to the schema after the first release, so existing
# databases pick them up without being rebuilt.
MIGRATIONS: dict[str, dict[str, str]] = {
    "games": {"is_all_star": "INTEGER DEFAULT 0"},
    "player_games": {"is_all_star": "INTEGER DEFAULT 0"},
}


def _migrate_stat_catalog(conn: sqlite3.Connection) -> None:
    """Re-key stat_catalog by (table_name, stat_name).

    It was keyed by stat_name alone while player_games was the only table
    growing columns. Team defense reuses names that mean something different
    there - `sacks` is taken on a QB game log and given up on a defense - so
    the table has to be rebuilt on databases predating it.
    """
    columns = existing_columns(conn, "stat_catalog")
    if not columns or "table_name" in columns:
        return
    conn.execute("ALTER TABLE stat_catalog RENAME TO stat_catalog_old")
    conn.executescript(STAT_CATALOG_DDL)
    conn.execute(
        "INSERT INTO stat_catalog (table_name, stat_name, column_name, first_seen) "
        "SELECT 'player_games', stat_name, column_name, first_seen FROM stat_catalog_old"
    )
    conn.execute("DROP TABLE stat_catalog_old")
    log.info("migrated stat_catalog: re-keyed by (table_name, stat_name)")


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    for table, columns in MIGRATIONS.items():
        present = existing_columns(conn, table)
        for column, decl in columns.items():
            if column not in present:
                conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {decl}')
                log.info("migrated %s: added %s", table, column)
    _migrate_stat_catalog(conn)
    ensure_stat_columns(conn, TEAM_DEFENSE_BASELINE_STATS, "team_defense_games")
    conn.commit()


# ------------------------------------------------------------------ columns

# Tables whose stat columns are created on demand, with the fixed columns a
# stat key must not collide with.
DYNAMIC_STAT_TABLES = {
    "player_games": PLAYER_GAME_FIXED_COLUMNS,
    "team_defense_games": TEAM_DEFENSE_FIXED_COLUMNS,
}

# ESPN defensive stat keys created up front rather than on first sight.
# SQLite accepts a view naming a column that does not exist and only fails when
# the view is queried, so anything a view references has to exist from the
# start - and these are the keys a defense row is worth reading for anyway.
TEAM_DEFENSE_BASELINE_STATS = (
    "sacks",
    "sackYards",
    "interceptions",
    "interceptionYards",
    "interceptionTouchdowns",
    "defensiveTouchdowns",
    "safeties",
    "totalTackles",
    "soloTackles",
    "assistTackles",
    "tacklesForLoss",
    "passesDefended",
    "QBHits",
)


def column_for_stat(stat_name: str, table: str = "player_games") -> str:
    return f"stat_{stat_name}" if stat_name in DYNAMIC_STAT_TABLES[table] else stat_name


def existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f'PRAGMA table_info("{table}")')}


def ensure_stat_columns(
    conn: sqlite3.Connection, stat_names: Iterable[str], table: str = "player_games"
) -> dict[str, str]:
    """Add a REAL column for each unseen stat key. Returns stat -> column mapping."""
    present = existing_columns(conn, table)
    mapping: dict[str, str] = {}
    for stat_name in stat_names:
        column = column_for_stat(stat_name, table)
        mapping[stat_name] = column
        if column not in present:
            conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" REAL')
            present.add(column)
            log.info("added stat column %s.%s", table, column)
        conn.execute(
            "INSERT INTO stat_catalog (table_name, stat_name, column_name, first_seen) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(table_name, stat_name) DO NOTHING",
            (table, stat_name, column, now_iso()),
        )
    conn.commit()
    return mapping


# ------------------------------------------------------------------- upserts


def _upsert(conn: sqlite3.Connection, table: str, row: dict[str, Any], keys: tuple[str, ...]) -> None:
    columns = list(row)
    placeholders = ", ".join("?" for _ in columns)
    quoted = ", ".join(f'"{c}"' for c in columns)
    updates = ", ".join(f'"{c}" = excluded."{c}"' for c in columns if c not in keys)
    conflict = ", ".join(f'"{k}"' for k in keys)
    sql = f'INSERT INTO "{table}" ({quoted}) VALUES ({placeholders}) ON CONFLICT({conflict}) DO '
    sql += f"UPDATE SET {updates}" if updates else "NOTHING"
    conn.execute(sql, [row[c] for c in columns])


def upsert_athlete(conn: sqlite3.Connection, profile: dict) -> None:
    row = dict(profile)
    row["updated_at"] = now_iso()
    _upsert(conn, "athletes", row, ("athlete_id",))
    conn.commit()


def upsert_games(conn: sqlite3.Connection, games: Iterable[dict]) -> int:
    count = 0
    for game in games:
        _upsert(conn, "games", dict(game), ("event_id",))
        count += 1
    conn.commit()
    return count


def _upsert_stat_rows(
    conn: sqlite3.Connection, table: str, rows: Iterable[dict], keys: tuple[str, ...]
) -> int:
    """Insert rows whose `_stats` mapping is materialised into its own columns."""
    rows = list(rows)
    if not rows:
        return 0

    stat_names = sorted({name for row in rows for name in row.get("_stats", {})})
    mapping = ensure_stat_columns(conn, stat_names, table)

    count = 0
    for row in rows:
        record = {k: v for k, v in row.items() if not k.startswith("_")}
        for stat_name, value in row.get("_stats", {}).items():
            record[mapping[stat_name]] = value
        record["loaded_at"] = now_iso()
        _upsert(conn, table, record, keys)
        count += 1
    conn.commit()
    return count


def upsert_player_games(conn: sqlite3.Connection, rows: Iterable[dict]) -> int:
    """Insert player-game rows, materialising stat keys into their own columns."""
    return _upsert_stat_rows(conn, "player_games", rows, ("athlete_id", "event_id"))


def upsert_team_defense_games(conn: sqlite3.Connection, rows: Iterable[dict]) -> int:
    """Insert team-defense rows, materialising stat keys into their own columns."""
    return _upsert_stat_rows(conn, "team_defense_games", rows, ("team_id", "event_id"))


def upsert_teams(conn: sqlite3.Connection, teams: Iterable[dict]) -> int:
    count = 0
    for team in teams:
        _upsert(conn, "teams", dict(team), ("team_id",))
        count += 1
    conn.commit()
    return count


def replace_rankings(conn: sqlite3.Connection, season: int, scoring: str, rows: Iterable[dict]) -> int:
    """Swap in a fresh ranking for one season/format."""
    conn.execute("DELETE FROM rankings WHERE season = ? AND scoring = ?", (season, scoring))
    count = 0
    for row in rows:
        record = dict(row)
        record["computed_at"] = now_iso()
        _upsert(conn, "rankings", record, ("season", "scoring", "athlete_id"))
        count += 1
    conn.commit()
    return count


def record_sync(conn: sqlite3.Connection, athlete_id: str, season: int, games: int) -> None:
    _upsert(
        conn,
        "sync_log",
        {"athlete_id": athlete_id, "season": season, "games": games, "synced_at": now_iso()},
        ("athlete_id", "season"),
    )
    conn.commit()


# --------------------------------------------------------------------- views

# Recreated after every load: SQLite freezes `SELECT *` at view-creation time,
# so a view built before a new stat column exists would never show it.
VIEWS = {
    "v_player_games": """
        CREATE VIEW v_player_games AS
        SELECT a.display_name, a.position_abbr, pg.*
        FROM player_games pg
        JOIN athletes a ON a.athlete_id = pg.athlete_id
        WHERE pg.is_all_star = 0
    """,
    "v_player_seasons": """
        CREATE VIEW v_player_seasons AS
        SELECT
            a.display_name,
            a.position_abbr,
            pg.athlete_id,
            pg.season,
            pg.season_type,
            COUNT(*)                          AS games,
            ROUND(SUM(pg.fp_ppr), 2)          AS fp_ppr_total,
            ROUND(AVG(pg.fp_ppr), 2)          AS fp_ppr_per_game,
            ROUND(SUM(pg.fp_half_ppr), 2)     AS fp_half_ppr_total,
            ROUND(SUM(pg.fp_standard), 2)     AS fp_standard_total
        FROM player_games pg
        JOIN athletes a ON a.athlete_id = pg.athlete_id
        WHERE pg.is_all_star = 0
        GROUP BY pg.athlete_id, pg.season, pg.season_type
    """,
    "v_team_defense": """
        CREATE VIEW v_team_defense AS
        SELECT t.display_name AS team_name, td.*
        FROM team_defense_games td
        LEFT JOIN teams t ON t.team_id = td.team_id
        WHERE td.is_all_star = 0
    """,
    # One row per player-game with the defense they faced attached, which is
    # the join the game logs are collected for.
    "v_player_games_vs_defense": """
        CREATE VIEW v_player_games_vs_defense AS
        SELECT
            pg.*,
            td.points_allowed             AS def_points_allowed,
            td.yards_allowed              AS def_yards_allowed,
            td.pass_yards_allowed         AS def_pass_yards_allowed,
            td.rush_yards_allowed         AS def_rush_yards_allowed,
            td.pass_tds_allowed           AS def_pass_tds_allowed,
            td.rush_tds_allowed           AS def_rush_tds_allowed,
            td.turnovers_forced           AS def_turnovers_forced,
            td.takeaways                  AS def_takeaways,
            td.sacks                      AS def_sacks
        FROM v_player_games pg
        LEFT JOIN team_defense_games td
               ON td.event_id = pg.event_id
              AND td.team_id  = pg.opponent_id
    """,
    # Every regular-season game twice, once from each team's point of view, so a
    # team's schedule (including 2026's, still unplayed) reads as one row per
    # week with the opponent alongside. `score` is NULL until the game is played.
    #
    # Filtered on season_type rather than season_type_name: ESPN's own label is
    # bare ("Regular Season") on schedule-loaded rows but year-prefixed
    # ("2019 Regular Season") on older game-log rows, so matching the text drops
    # the earlier seasons. The id is 2 in both cases.
    "v_team_schedule": """
        CREATE VIEW v_team_schedule AS
        WITH both_sides AS (
            SELECT event_id, season_type_name, season, week,
                   home_team_id AS team, away_team_id AS opponent, score
            FROM games
            WHERE season_type = 2
            UNION ALL
            SELECT event_id, season_type_name, season, week,
                   away_team_id AS team, home_team_id AS opponent, score
            FROM games
            WHERE season_type = 2
        )
        SELECT * FROM both_sides
        ORDER BY season, week, team, opponent
    """,
    "v_rankings": """
        CREATE VIEW v_rankings AS
        SELECT r.season, r.scoring, r.rank,
               a.display_name,
               r.position_abbr,
               r.position_abbr || r.position_rank AS position_rank,
               a.team_abbr,
               r.games, r.points_total, r.points_per_game
        FROM rankings r
        JOIN athletes a ON a.athlete_id = r.athlete_id
    """,
}


def rebuild_views(conn: sqlite3.Connection) -> None:
    for name, ddl in VIEWS.items():
        conn.execute(f"DROP VIEW IF EXISTS {name}")
        conn.execute(ddl)
    conn.commit()
