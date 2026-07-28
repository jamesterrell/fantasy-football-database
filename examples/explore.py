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
    print("\n=== 2. One player's ten most recent games")
    print(q(conn, """
        SELECT game_date, week, opponent_abbr, home_away, result, receivingYards, receivingTouchdowns,
               rushingAttempts, rushingYards, rushingTouchdowns, receptions, fp_ppr
        FROM v_player_games
        WHERE display_name = :player
        ORDER BY game_date DESC
        LIMIT 10
    """, player="Jonathan Taylor").to_string(index=False))

    # ------------------------------------------------------------------ 3
    # WHERE filters rows, ORDER BY sorts, LIMIT caps the count.
    print("\n=== 3. Biggest single games in the database (regular season)")
    print(q(conn, """
        SELECT display_name, position_abbr, season, week, opponent_abbr,
               rushingYards, receivingYards, receptions, fp_ppr
        FROM v_player_games
        WHERE season_type = 2
        ORDER BY fp_ppr DESC
        LIMIT 10
    """).to_string(index=False))

    # ------------------------------------------------------------------ 4
    # GROUP BY collapses rows into one row per group; the aggregate functions
    # (SUM/AVG/COUNT/MAX) describe each group.
    print("\n=== 4. One player's season totals (this is what GROUP BY is for)")
    print(q(conn, """
        SELECT season,
               COUNT(*)                        AS games,
               SUM(rushingYards)               AS rush_yds,
               SUM(rushingTouchdowns)          AS rush_td,
               SUM(receptions)                 AS rec,
               ROUND(SUM(fp_ppr), 1)           AS ppr_total,
               ROUND(AVG(fp_ppr), 1)           AS ppr_per_game
        FROM v_player_games
        WHERE display_name = :player AND season_type = 2
        GROUP BY season
        ORDER BY season
    """, player="Jonathan Taylor").to_string(index=False))

    # ------------------------------------------------------------------ 5
    # HAVING filters *after* grouping - use it on aggregates, WHERE on raw rows.
    print("\n=== 5. Most consistent RBs: best average PPR, min 10 games in 2025")
    print(q(conn, """
        SELECT display_name,
               COUNT(*)              AS games,
               ROUND(AVG(fp_ppr), 1) AS avg_ppr,
               ROUND(MAX(fp_ppr), 1) AS best_game
        FROM v_player_games
        WHERE season = 2025 AND season_type = 2 AND position_abbr = 'RB'
        GROUP BY athlete_id
        HAVING games >= 10
        ORDER BY avg_ppr DESC
        LIMIT 10
    """).to_string(index=False))

    # ------------------------------------------------------------------ 6
    # The derived ranking, with position rank (RB1, WR1, ...) alongside.
    print("\n=== 6. Top 15 of the 2025 PPR ranking")
    print(q(conn, """
        SELECT rank, display_name, position_rank, team_abbr, games,
               points_total, points_per_game
        FROM v_rankings
        WHERE season = :season AND scoring = 'ppr'
        ORDER BY rank
        LIMIT 15
    """, season=2025).to_string(index=False))

    # ------------------------------------------------------------------ 7
    # Once it's a DataFrame, use pandas for anything SQL makes awkward -
    # rolling windows, shifts, and other modelling features. groupby() keeps
    # each player's history separate so one player's form can't leak into
    # the next player's rows.
    print("\n=== 7. Hand off to pandas: per-player rolling form and a target")
    df = q(conn, """
        SELECT athlete_id, display_name, game_date, season, week, fp_ppr
        FROM v_player_games
        WHERE season_type = 2
        ORDER BY athlete_id, game_date
    """)
    grouped = df.groupby("athlete_id")["fp_ppr"]
    df["fp_ppr_roll3"] = grouped.transform(lambda s: s.rolling(3).mean()).round(1)
    df["fp_ppr_next"] = grouped.shift(-1)   # a plausible model target
    print(df[df.display_name == "Jonathan Taylor"].tail(6).to_string(index=False))
    print(f"\n({len(df):,} player-games across {df.athlete_id.nunique()} players)")

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
