"""Worked examples for querying the database from Python.

Run the whole thing:      python examples/explore.py
Or copy any single query into your own script / notebook.

Everything here is a plain SQL string handed to pandas. If you can write the
SQL, `pd.read_sql_query(sql, conn)` gives you a DataFrame - that is the whole
interface.
"""

import sqlite3
from pathlib import Path

import pandas as pd

DB = Path(__file__).resolve().parent.parent / "data" / "fantasy_football.db"


def q(conn, sql, **params):
    """Run SQL, return a DataFrame. Params are passed safely, never f-strings."""
    return pd.read_sql_query(sql, conn, params=params or None)


def main():
    conn = sqlite3.connect(DB)

    # ------------------------------------------------------------------ 1
    print("\n=== 1. What tables exist?")
    print(q(conn, """
        SELECT type, name FROM sqlite_master
        WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%'
        ORDER BY type, name
    """).to_string(index=False))

    # ------------------------------------------------------------------ 2
    # v_player_games is the one to use for modelling: it has the player's name
    # and position joined on, and Pro Bowls already filtered out.
    print("\n=== 2. Ten most recent games")
    print(q(conn, """
        SELECT game_date, week, opponent_abbr, home_away, result, receivingYards, receivingTouchdowns,
               rushingAttempts, rushingYards, rushingTouchdowns, receptions, fp_ppr
        FROM v_player_games
        ORDER BY game_date DESC
        LIMIT 10
    """).to_string(index=False))

    # ------------------------------------------------------------------ 3
    # WHERE filters rows, ORDER BY sorts, LIMIT caps the count.
    print("\n=== 3. Best PPR games of his career (regular season only)")
    print(q(conn, """
        SELECT season, week, opponent_abbr, rushingYards, rushingTouchdowns,
               receptions, receivingYards, fp_ppr
        FROM v_player_games
        WHERE season_type = 2
        ORDER BY fp_ppr DESC
        LIMIT 5
    """).to_string(index=False))

    # ------------------------------------------------------------------ 4
    # GROUP BY collapses rows into one row per group; the aggregate functions
    # (SUM/AVG/COUNT/MAX) describe each group.
    print("\n=== 4. Per-season totals (this is what GROUP BY is for)")
    print(q(conn, """
        SELECT season,
               COUNT(*)                        AS games,
               SUM(rushingYards)               AS rush_yds,
               SUM(rushingTouchdowns)          AS rush_td,
               SUM(receptions)                 AS rec,
               ROUND(SUM(fp_ppr), 1)           AS ppr_total,
               ROUND(AVG(fp_ppr), 1)           AS ppr_per_game
        FROM v_player_games
        WHERE season_type = 2
        GROUP BY season
        ORDER BY season
    """).to_string(index=False))

    # ------------------------------------------------------------------ 5
    print("\n=== 5. Home vs away splits")
    print(q(conn, """
        SELECT home_away,
               COUNT(*)              AS games,
               ROUND(AVG(fp_ppr), 1) AS avg_ppr,
               ROUND(AVG(rushingYards), 1) AS avg_rush_yds
        FROM v_player_games
        WHERE season_type = 2
        GROUP BY home_away
    """).to_string(index=False))

    # ------------------------------------------------------------------ 6
    # Named parameters (:season) keep values out of the SQL string.
    print("\n=== 6. Parameterised query - one season")
    print(q(conn, """
        SELECT week, opponent_abbr, result, rushingYards, fp_ppr
        FROM v_player_games
        WHERE season = :season AND season_type = 2
        ORDER BY week
    """, season=2025).head().to_string(index=False))

    # ------------------------------------------------------------------ 7
    # Once it's a DataFrame, use pandas for anything SQL makes awkward -
    # rolling windows, shifts, and other modelling features.
    print("\n=== 7. Hand off to pandas: 3-game rolling average")
    df = q(conn, """
        SELECT game_date, season, week, opponent_abbr, fp_ppr
        FROM v_player_games
        WHERE season_type = 2
        ORDER BY game_date
    """)
    df["fp_ppr_roll3"] = df["fp_ppr"].rolling(3).mean().round(1)
    df["fp_ppr_next"] = df["fp_ppr"].shift(-1)   # a plausible model target
    print(df.tail(8).to_string(index=False))

    # ------------------------------------------------------------------ 8
    # Every stat ESPN sent is also kept verbatim as JSON per game, so you can
    # confirm nothing was lost or mangled in parsing.
    print("\n=== 8. The raw payload behind one row")
    print(q(conn, """
        SELECT game_date, raw_stats FROM player_games
        ORDER BY game_date DESC LIMIT 1
    """).iloc[0]["raw_stats"])

    conn.close()


if __name__ == "__main__":
    main()
