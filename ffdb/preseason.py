"""Preseason roster and depth-chart state, from nflverse.

Why this is not an ESPN loader
------------------------------
Everything else in this project comes from ESPN. This does not, because ESPN
does not have it. The `athletes` table carries `status` and `team_abbr`, but it
is a live snapshot that gets overwritten on every refresh, so there is no way to
ask what a player's status was in August of 2019. ESPN's depth-chart endpoint
looks like it fixes that - it takes a season in the URL - but it ignores the
parameter and serves the current chart for every year requested. Asking for
Kansas City in 2016 returns Patrick Mahomes as the starter, three years before
he took the job. Verified before writing this module; do not re-try that route.

nflverse publishes the real archive as parquet files on GitHub releases: weekly
rosters and weekly depth charts from 2016 on, plus a player crosswalk carrying
`espn_id`, which is what makes any of it joinable to a database keyed on ESPN
ids. No client library is used - these are plain HTTP GETs of published files,
cached under `data/nflverse/` the same way the ESPN archive is cached.

What "preseason" means here
---------------------------
nflverse's earliest roster snapshot is regular-season week 1; the source has no
preseason game type. Week 1 falls a few days after most fantasy drafts, so this
is very slightly ahead of true draft-day knowledge:

  * For the cases that motivated the table - unsigned, released, retired - the
    fact is settled by July and the few days do not matter.
  * `INA` is declared on game day and is genuinely after the fact. Prefer
    `on_roster` and `status` over `active` wherever that distinction could
    change a conclusion.

`snapshot_week` records the week each row actually came from, so this stays
checkable rather than assumed.

Grain
-----
One row per (season, athlete_id). A player in the projection universe who
appears on no roster at all gets an explicit row with status 'NONE'. That
matters: a LEFT JOIN returning nothing then means "no data for this player",
which is a different statement from "he was not on a roster", and a model that
conflates the two will read missingness as unemployment.
"""

from __future__ import annotations

import io
import logging
import sqlite3
from collections.abc import Iterable

import pandas as pd
import requests

from . import config, db

log = logging.getLogger(__name__)

# nflverse status codes. ACT is the only one meaning "will suit up"; the rest
# are on the books in some form (DEV = practice squad, RES = injured reserve,
# EXE = exempt) or off them entirely. Stored raw so a consumer can draw its own
# line rather than inheriting the one below.
STATUS_DESC = {
    "ACT": "active roster",
    "DEV": "practice squad",
    "RES": "injured reserve",
    "INA": "inactive (game-day)",
    "CUT": "released",
    "RET": "retired",
    "EXE": "exempt list",
    "PUP": "physically unable to perform",
    "SUS": "suspended",
    "UFA": "unrestricted free agent",
    "RFA": "restricted free agent",
    "NWT": "not with team",
    "RSN": "reserve, did not report",
    "RSR": "reserve, retired",
    "TRC": "trade pending",
    "TRD": "traded",
    "NONE": "not on any roster",
}

# Not under contract to a team at the snapshot. The line is drawn where the
# data draws it: measured over 2017-2025, the share of these players who ever
# record a regular-season stat line that year runs RET 0.0%, UFA 0.6%, NWT 2.5%,
# RSN 3.4%, CUT 3.5% - against 26.3% for ACT. The injured and the buried stay on
# the roster side (RES 7.1%, PUP 7.8%, DEV 12.9%) because they are employed and
# can be activated; `status` is kept raw so a consumer wanting a finer split
# than this flag does not have to accept ours.
OFF_ROSTER = {"CUT", "RET", "NONE", "UFA", "NWT", "RSN", "RSR"}

FANTASY_POSITIONS = ("QB", "RB", "WR", "TE")


# --------------------------------------------------------------- fetching


def _fetch(asset: str, force: bool = False) -> pd.DataFrame:
    """GET one nflverse release asset, caching the parquet under data/nflverse."""
    config.NFLVERSE_DIR.mkdir(parents=True, exist_ok=True)
    local = config.NFLVERSE_DIR / asset.replace("/", "_")
    if local.exists() and not force:
        return pd.read_parquet(local)

    url = f"{config.NFLVERSE_RELEASE_URL}/{asset}"
    log.info("fetching %s", url)
    response = requests.get(
        url, timeout=300, headers={"User-Agent": config.NFLVERSE_USER_AGENT}
    )
    response.raise_for_status()
    frame = pd.read_parquet(io.BytesIO(response.content))
    frame.to_parquet(local)
    return frame


def _crosswalk(force: bool = False) -> pd.DataFrame:
    """gsis_id -> espn_id.

    The weekly roster files carry `espn_id` inline for about 70% of rows; this
    fills the rest. Ids are strings on both sides throughout - pandas will read
    an id column as float if any value is missing, and "4242335.0" joins to
    nothing while raising no error.
    """
    players = _fetch("players/players.parquet", force=force)
    out = players.loc[
        players.espn_id.notna() & players.gsis_id.notna(), ["gsis_id", "espn_id"]
    ].copy()
    out["espn_id"] = _as_id(out.espn_id)
    return out.drop_duplicates("gsis_id")


def _as_id(series: pd.Series) -> pd.Series:
    """Normalise an id column to a bare string, dropping any float artefact."""
    return series.astype("string").str.replace(r"\.0$", "", regex=True)


def _first_regular_week(frame: pd.DataFrame) -> pd.DataFrame:
    reg = frame[frame.game_type == "REG"] if "game_type" in frame.columns else frame
    return reg if reg.empty else reg[reg.week == reg.week.min()]


# --------------------------------------------------------------- assembling


def _rosters(season: int, crosswalk: pd.DataFrame, force: bool = False) -> pd.DataFrame:
    """Week-1 roster snapshot for one season, keyed by ESPN athlete_id."""
    raw = _first_regular_week(_fetch(f"weekly_rosters/roster_weekly_{season}.parquet", force))
    if raw.empty:
        return pd.DataFrame()

    frame = raw.copy()
    frame["snapshot_week"] = frame.week.astype(int)
    frame["espn_id"] = _as_id(frame.espn_id) if "espn_id" in frame.columns else pd.NA
    frame = frame.merge(
        crosswalk.rename(columns={"espn_id": "_espn_from_crosswalk"}), on="gsis_id", how="left"
    )
    frame["athlete_id"] = frame.espn_id.fillna(frame._espn_from_crosswalk)
    frame = frame[frame.athlete_id.notna()].copy()

    # A player can appear twice in one week if he changed teams inside it. Keep
    # the active row, which is the one describing where he actually is.
    frame["_off"] = (frame.status != "ACT").astype(int)
    frame = frame.sort_values(["athlete_id", "_off"]).drop_duplicates("athlete_id")

    frame["season"] = season
    return frame[
        ["season", "athlete_id", "gsis_id", "full_name", "position", "team",
         "status", "snapshot_week"]
    ]


DEPTH_COLUMNS = ["season", "gsis_id", "depth_rank", "depth_position", "depth_snapshot"]


def _depth_weekly(raw: pd.DataFrame) -> pd.DataFrame:
    """2016-2024 format: one row per player per formation per week."""
    frame = _first_regular_week(raw)
    if frame.empty:
        return pd.DataFrame(columns=DEPTH_COLUMNS)
    frame = frame.copy()
    frame["depth_rank"] = pd.to_numeric(frame.depth_team, errors="coerce")
    frame["depth_snapshot"] = "week " + frame.week.astype(int).astype(str)
    return frame.rename(columns={"depth_position": "depth_position"})


def _depth_snapshots(raw: pd.DataFrame, kickoff: pd.Timestamp | None) -> pd.DataFrame:
    """2025+ format: timestamped snapshots rather than weeks.

    nflverse changed the source part-way through the panel. The new file has no
    `week` at all - it is a running series of scrapes keyed by `dt`, every day
    or two, and it keeps running long after the season it is filed under ends:
    the 2025 file carries snapshots into March 2026.

    So neither end of the range is the right pick. The earliest is far too
    early - for a season still ahead of us it lands in March, before free
    agency, before the draft, before camp, which for 2026 meant a depth chart
    five months stale. The latest is worse: for a finished season it is months
    *after* the outcome being predicted, which is leakage outright.

    The rule is the last snapshot strictly before that season's first kickoff -
    the most recent thing anyone could have known on draft day, and nothing
    they could not. Kickoff dates come from the `games` table, which carries
    the schedule for a season not yet played.
    """
    frame = raw.copy()
    frame["dt"] = pd.to_datetime(frame.dt, utc=True)
    if kickoff is not None:
        before = frame[frame.dt < kickoff]
        # A season whose file begins after kickoff has no preseason reading at
        # all; fall back to the earliest rather than silently taking an
        # in-season one.
        frame = before if not before.empty else frame[frame.dt == frame.dt.min()]
    pick = frame.dt.max() if kickoff is not None else frame.dt.min()
    frame = frame[frame.dt == pick].copy()
    frame["depth_rank"] = pd.to_numeric(frame.pos_rank, errors="coerce")
    frame["depth_position"] = frame.pos_abb
    frame["depth_snapshot"] = pick.date().isoformat()
    return frame


def season_kickoff(conn: sqlite3.Connection, season: int) -> pd.Timestamp | None:
    """First regular-season kickoff, or None if the schedule is not loaded."""
    row = conn.execute(
        "SELECT MIN(game_date) FROM games WHERE season = ? AND season_type = ?",
        (season, config.SEASON_TYPE_REGULAR),
    ).fetchone()
    if not row or not row[0]:
        return None
    return pd.to_datetime(row[0], utc=True, format="ISO8601")


def _depth(season: int, force: bool = False,
           kickoff: pd.Timestamp | None = None) -> pd.DataFrame:
    """Best listed depth-chart rank per player at the start of a season.

    A player appears once per formation, so the same man is listed at several
    ranks; the minimum is his best role. `depth_position` comes from that same
    row because the slot a player is ranked at can differ from his listed
    position - a receiver ranked in the slot, a back ranked at fullback.

    Two source formats exist and are dispatched on rather than assumed; see
    `_depth_snapshots` for the 2025 change.
    """
    empty = pd.DataFrame(columns=DEPTH_COLUMNS)
    try:
        raw = _fetch(f"depth_charts/depth_charts_{season}.parquet", force)
    except requests.HTTPError:
        log.warning("no depth chart published for %s", season)
        return empty
    if raw.empty or "gsis_id" not in raw.columns:
        return empty

    if "week" in raw.columns:
        frame = _depth_weekly(raw)
    elif "dt" in raw.columns:
        frame = _depth_snapshots(raw, kickoff)
    else:
        log.warning("%s depth chart in an unrecognised format, skipped", season)
        return empty

    frame = frame[frame.gsis_id.notna()].dropna(subset=["depth_rank"])
    if frame.empty:
        return empty
    best = frame.sort_values("depth_rank").drop_duplicates("gsis_id").copy()
    best["season"] = season
    return best[DEPTH_COLUMNS]


# How many seasons back a player can have last appeared and still be someone a
# projection will be asked about. Mirrors MAX_MISSED_SEASONS = 1 in the model's
# `bayes.data.add_missed_seasons`, which puts a player who sat out one full
# season back on the board.
#
# Getting this wrong is silent and one-directional. A window of a single season
# left players who missed the previous year *and* are unsigned falling through
# both sides - absent from the roster file because no team employs them, and
# absent from the candidate list because they did not play - so they got no row
# at all. A missing row reads as "unknown", which the model treats as
# rostered-until-proven-otherwise, and the 2026 board duly projected Brandon
# Aiyuk, Joe Mixon and Diontae Johnson at 25-33 points while nobody employed
# any of them.
CANDIDATE_LOOKBACK = 2


def _candidates(conn: sqlite3.Connection, season: int) -> pd.DataFrame:
    """Fantasy players recently active enough to be projected for `season`.

    These are the rows a projection will be asked to produce, so these are the
    rows that need an answer to "was he on a roster". Anyone here who is absent
    from the snapshot is genuinely unrostered rather than merely unmatched,
    which is what makes the 'NONE' rows below safe to assert.
    """
    marks = ",".join("?" * len(FANTASY_POSITIONS))
    return pd.read_sql_query(
        f"""
        SELECT DISTINCT pg.athlete_id,
               a.display_name  AS full_name,
               a.position_abbr AS position
        FROM player_games pg
        JOIN athletes a ON a.athlete_id = pg.athlete_id
        WHERE pg.season BETWEEN ? AND ?
          AND pg.season_type = ?
          AND pg.is_all_star = 0
          AND a.position_abbr IN ({marks})
        """,
        conn,
        params=(season - CANDIDATE_LOOKBACK, season - 1,
                config.SEASON_TYPE_REGULAR, *FANTASY_POSITIONS),
    )


def build_season(
    conn: sqlite3.Connection, season: int, crosswalk: pd.DataFrame, force: bool = False
) -> pd.DataFrame:
    """Assemble one season's preseason rows, rostered and unrostered alike."""
    roster = _rosters(season, crosswalk, force=force)
    if roster.empty:
        return roster

    merged = roster.merge(
        _depth(season, force=force, kickoff=season_kickoff(conn, season)),
        on=["season", "gsis_id"], how="left",
    )

    candidates = _candidates(conn, season)
    if not candidates.empty:
        candidates["athlete_id"] = _as_id(candidates.athlete_id)
        missing = candidates[~candidates.athlete_id.isin(merged.athlete_id)].copy()
        if not missing.empty:
            missing["season"] = season
            missing["status"] = "NONE"
            missing["snapshot_week"] = int(merged.snapshot_week.iloc[0])
            merged = pd.concat([merged, missing], ignore_index=True)

    merged["on_roster"] = (~merged.status.isin(OFF_ROSTER)).astype(int)
    merged["active"] = (merged.status == "ACT").astype(int)
    merged["status_desc"] = merged.status.map(STATUS_DESC).fillna(merged.status)
    merged["source"] = "nflverse"
    return merged


def build(
    conn: sqlite3.Connection, seasons: Iterable[int], force: bool = False
) -> dict:
    """Build and store preseason rows for each season. Returns a summary."""
    crosswalk = _crosswalk(force=force)
    per_season, total = {}, 0
    for season in seasons:
        frame = build_season(conn, season, crosswalk, force=force)
        if frame.empty:
            log.warning("%s: no roster data published, skipped", season)
            continue
        written = replace_season(conn, season, frame)
        per_season[season] = {
            "rows": written,
            "rostered": int(frame.on_roster.sum()),
            "unrostered": int((frame.status == "NONE").sum()),
            "with_depth": int(frame.depth_rank.notna().sum()),
            "snapshot_week": int(frame.snapshot_week.iloc[0]),
        }
        total += written
        log.info("%s: %d rows", season, written)
    return {"seasons": per_season, "rows": total}


# ----------------------------------------------------------------- storage

COLUMNS = (
    "season", "athlete_id", "gsis_id", "full_name", "position", "team",
    "status", "status_desc", "on_roster", "active", "depth_rank",
    "depth_position", "snapshot_week", "depth_snapshot", "source", "updated_at",
)


def replace_season(conn: sqlite3.Connection, season: int, frame: pd.DataFrame) -> int:
    """Swap in one season's rows, the way replace_rankings does for a ranking."""
    frame = frame.copy()
    frame["updated_at"] = db.now_iso()
    for column in COLUMNS:
        if column not in frame.columns:
            frame[column] = None
    frame = frame[list(COLUMNS)].where(pd.notna(frame), None)

    conn.execute("DELETE FROM preseason_roster WHERE season = ?", (season,))
    conn.executemany(
        f"INSERT INTO preseason_roster ({', '.join(COLUMNS)}) "
        f"VALUES ({', '.join('?' * len(COLUMNS))})",
        frame.itertuples(index=False, name=None),
    )
    conn.commit()
    return len(frame)
