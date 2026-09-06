"""Snap share per player-season, from nflverse.

Box-score volume - carries, targets, attempts - measures what a player *did*.
Snap share measures how much he was on the field to do it, which is the closer
thing to role. A back on 30% of snaps who happened to score twice looks like a
starter in a box score and does not look like one here.

Source is nflverse's `snap_counts`, itself scraped from Pro Football Reference,
available 2012 onward - comfortably covering the 2016-2025 panel. Keyed on
`pfr_player_id`, so it needs the players crosswalk to reach an ESPN athlete id;
that lands 99.7% of offensive snap rows.

Grain: one row per (season, athlete_id), regular season only. `offense_pct` is
averaged over the games he actually appeared in, so it answers "when he was
active, how much did he play" rather than diluting a starter's share with the
weeks he was injured - the availability model already owns that question and
double-counting it here would make the two halves fight.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable

import pandas as pd

from . import config, db
from .preseason import _as_id, _fetch

log = logging.getLogger(__name__)


def _crosswalk_pfr(force: bool = False) -> pd.DataFrame:
    """pfr_id -> espn_id."""
    players = _fetch("players/players.parquet", force=force)
    out = players.loc[
        players.pfr_id.notna() & players.espn_id.notna(), ["pfr_id", "espn_id"]
    ].copy()
    out["espn_id"] = _as_id(out.espn_id)
    return out.drop_duplicates("pfr_id")


def season_snaps(season: int, crosswalk: pd.DataFrame, force: bool = False) -> pd.DataFrame:
    """One row per player for one regular season."""
    raw = _fetch(f"snap_counts/snap_counts_{season}.parquet", force=force)
    if raw.empty:
        return pd.DataFrame()

    reg = raw[(raw.game_type == "REG") & (raw.offense_snaps > 0)].copy()
    if reg.empty:
        return pd.DataFrame()

    reg = reg.merge(crosswalk, left_on="pfr_player_id", right_on="pfr_id", how="left")
    reg = reg[reg.espn_id.notna()]

    grouped = reg.groupby("espn_id").agg(
        games_with_snaps=("offense_snaps", "size"),
        offense_snaps=("offense_snaps", "sum"),
        # Mean over appearances, not over the season - see the module docstring.
        offense_pct=("offense_pct", "mean"),
        team=("team", "last"),
        position=("position", "last"),
    ).reset_index().rename(columns={"espn_id": "athlete_id"})
    grouped["season"] = season
    return grouped


COLUMNS = ("season", "athlete_id", "team", "position", "games_with_snaps",
           "offense_snaps", "offense_pct", "updated_at")


def replace_season(conn: sqlite3.Connection, season: int, frame: pd.DataFrame) -> int:
    frame = frame.copy()
    frame["updated_at"] = db.now_iso()
    for c in COLUMNS:
        if c not in frame.columns:
            frame[c] = None
    frame = frame[list(COLUMNS)].where(pd.notna(frame), None)
    conn.execute("DELETE FROM player_snaps WHERE season = ?", (season,))
    conn.executemany(
        f"INSERT INTO player_snaps ({', '.join(COLUMNS)}) "
        f"VALUES ({', '.join('?' * len(COLUMNS))})",
        frame.itertuples(index=False, name=None),
    )
    conn.commit()
    return len(frame)


def build(conn: sqlite3.Connection, seasons: Iterable[int], force: bool = False) -> dict:
    crosswalk = _crosswalk_pfr(force=force)
    per_season, total = {}, 0
    for season in seasons:
        try:
            frame = season_snaps(season, crosswalk, force=force)
        except Exception as exc:                                  # no file yet
            log.warning("%s: %s", season, exc)
            continue
        if frame.empty:
            log.warning("%s: no snap data published", season)
            continue
        n = replace_season(conn, season, frame)
        per_season[season] = {"rows": n, "mean_pct": float(frame.offense_pct.mean())}
        total += n
    return {"seasons": per_season, "rows": total}
