"""Command line entry point:  python -m ffdb <command>"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import athletes as athletes_mod
from . import attendance, config, db, pipeline, ranking, scoring, seasonpool
from .espn import ESPNClient


def _client(args: argparse.Namespace) -> ESPNClient:
    return ESPNClient(use_cache=not args.no_cache)


def cmd_add(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    db.init_db(conn)
    summaries, failures = pipeline.build_players(
        conn, _client(args), args.players, seasons=args.season, force=args.force
    )
    for summary in summaries:
        seasons = ", ".join(f"{s}:{n}" for s, n in sorted(summary["seasons"].items()))
        print(
            f"{summary['name']} ({summary['position'] or '?'}, id {summary['athlete_id']}): "
            f"{summary['games']} games  [{seasons}]"
        )
    for name, error in failures:
        print(f"FAILED {name}: {error}", file=sys.stderr)
    conn.close()
    return 1 if failures else 0


def cmd_players(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    db.init_db(conn)
    rows = conn.execute(
        """
        SELECT a.athlete_id, a.display_name, a.position_abbr, a.team_abbr,
               COUNT(pg.event_id) AS games,
               MIN(pg.season) AS first_season, MAX(pg.season) AS last_season
        FROM athletes a
        LEFT JOIN player_games pg ON pg.athlete_id = a.athlete_id
        GROUP BY a.athlete_id
        ORDER BY a.display_name
        """
    ).fetchall()
    if not rows:
        print("No players loaded yet. Try:  python -m ffdb add \"Jonathan Taylor\"")
        return 0
    print(f"{'ID':>8}  {'NAME':<26}{'POS':<5}{'TEAM':<6}{'GMS':>4}  SEASONS")
    for r in rows:
        span = f"{r['first_season']}-{r['last_season']}" if r["games"] else "-"
        print(
            f"{r['athlete_id']:>8}  {r['display_name'] or '?':<26}"
            f"{r['position_abbr'] or '':<5}{r['team_abbr'] or '':<6}{r['games']:>4}  {span}"
        )
    conn.close()
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    db.init_db(conn)
    key = athletes_mod.normalize_name(args.player)
    match = None
    for row in conn.execute("SELECT athlete_id, display_name FROM athletes"):
        if row["athlete_id"] == args.player or athletes_mod.normalize_name(
            row["display_name"] or ""
        ) == key:
            match = row
            break
    if match is None:
        print(f"{args.player!r} is not in the database yet.", file=sys.stderr)
        return 1

    print(f"{match['display_name']} ({match['athlete_id']})\n")
    header = f"{'SEASON':<8}{'TYPE':<6}{'GMS':>4}{'PPR':>9}{'PPR/G':>8}{'HALF':>9}{'STD':>9}"
    print(header)
    print("-" * len(header))
    for r in conn.execute(
        """
        SELECT season, season_type, games, fp_ppr_total, fp_ppr_per_game,
               fp_half_ppr_total, fp_standard_total
        FROM v_player_seasons WHERE athlete_id = ? ORDER BY season, season_type
        """,
        (match["athlete_id"],),
    ):
        label = {2: "reg", 3: "post", 1: "pre"}.get(r["season_type"], "?")
        print(
            f"{r['season']:<8}{label:<6}{r['games']:>4}{r['fp_ppr_total']:>9}"
            f"{r['fp_ppr_per_game']:>8}{r['fp_half_ppr_total']:>9}{r['fp_standard_total']:>9}"
        )
    conn.close()
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    import pandas as pd

    conn = db.connect(args.db)
    db.init_db(conn)
    frame = pd.read_sql_query(f"SELECT * FROM {args.table}", conn)
    out = Path(args.out) if args.out else config.EXPORT_DIR / f"{args.table}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(out, index=False)
    print(f"wrote {len(frame)} rows x {len(frame.columns)} cols -> {out}")
    conn.close()
    return 0


def cmd_build_top(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    db.init_db(conn)
    result = ranking.build_top_n(
        conn, _client(args), season=args.season, top=args.top,
        scoring_format=args.scoring, force=args.force,
    )
    pool = result["pool"]
    print(
        f"\ncandidate pool {pool['pool']} | {pool['loaded']} with {args.season} games | "
        f"{len(pool['empty'])} without | {len(pool['failed'])} failed"
    )
    print(
        f"ranked top {result['ranked']} by {args.scoring}; "
        f"backfilled {result['backfill']['games']} career games "
        f"for {result['backfill']['players']} players"
    )
    conn.close()
    return 0


def _season_range(values: list[str]) -> list[int]:
    """Accept `2022`, `2020-2024`, or several of either."""
    seasons: set[int] = set()
    for value in values:
        if "-" in value:
            start, _, end = value.partition("-")
            seasons.update(range(int(start), int(end) + 1))
        else:
            seasons.add(int(value))
    return sorted(seasons)


def cmd_build_season(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    db.init_db(conn)
    result = seasonpool.build_seasons(
        conn, _client(args), seasons=_season_range(args.season),
        force=args.force, resync=args.resync,
    )
    print(
        f"\n{result['discovered']} players appeared in {result['seasons']}; "
        f"{result['kept']} at fantasy positions"
    )
    print(
        f"pulled {result['pulled']} players ({result['games']} games); "
        f"{result['already_synced']} already complete"
    )
    if result["unresolved"]:
        print(
            f"{len(result['unresolved'])} players had no resolvable position "
            "and were left out:", file=sys.stderr,
        )
        for athlete_id, name in result["unresolved"]:
            print(f"  {name} ({athlete_id})", file=sys.stderr)
    if result["failed"]:
        print(f"{len(result['failed'])} failed:", file=sys.stderr)
        for athlete_id, error in result["failed"]:
            print(f"  {athlete_id}: {error}", file=sys.stderr)

    rows = conn.execute(
        "SELECT season, COUNT(DISTINCT athlete_id) AS players, COUNT(*) AS games "
        "FROM player_games WHERE season_type = 2 AND is_all_star = 0 "
        "AND season BETWEEN ? AND ? GROUP BY season ORDER BY season",
        (min(result["seasons"]), max(result["seasons"])),
    ).fetchall()
    if rows:
        print(f"\n{'SEASON':<8}{'PLAYERS':>9}{'GAMES':>8}")
        print("-" * 25)
        for r in rows:
            print(f"{r['season']:<8}{r['players']:>9}{r['games']:>8}")
    conn.close()
    return 1 if result["failed"] else 0


def cmd_attendance(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    db.init_db(conn)
    result = attendance.fill_seasons(
        conn, _client(args), seasons=_season_range(args.season),
        dry_run=args.dry_run, force=args.force,
    )

    verb = "would fill" if args.dry_run else "filled"
    print(
        f"\nchecked {result['athlete_seasons']} athlete-seasons across {result['seasons']}"
        + (f" ({result['eventlog_missing']} had no event log)" if result["eventlog_missing"] else "")
    )
    header = f"{'SEASON':<8}{'PLAYED, UNLOGGED':>18}{'INACTIVE':>10}{'FILLED':>8}{'NOT ZERO':>10}"
    print("\n" + header)
    print("-" * len(header))
    for season, counts in sorted(result["per_season"].items()):
        print(
            f"{season:<8}{counts['fillable']:>18}{counts['inactive']:>10}"
            f"{counts['filled']:>8}{counts['scoring']:>10}"
        )
    print(
        f"\n{result['fillable']} games played but unlogged, {result['inactive']} rostered but "
        f"inactive (left absent), {result['unknown_event']} events not in `games`"
    )
    print(f"{verb} {result['filled']} rows")
    if result["no_stat_line"]:
        print(f"{result['no_stat_line']} filled with zeros - ESPN served no stat line for them")
    if result["not_actually_scoreless"]:
        print(
            f"{result['not_actually_scoreless']} were not scoreless after all and were stored "
            "with their real numbers",
            file=sys.stderr,
        )

    if args.skip_games_played:
        conn.close()
        return 0

    gp = attendance.fill_from_games_played(
        conn, _client(args), seasons=_season_range(args.season),
        dry_run=args.dry_run, force=args.force,
    )
    print(
        f"\nseason totals read for {gp['athletes']} athletes"
        + (f" ({gp['no_stats']} unavailable)" if gp["no_stats"] else "")
        + f"; games-played recorded for {gp['seasons_recorded']} athlete-seasons"
    )
    header = f"{'SEASON':<8}{'ROWS SHORT OF GP':>18}{'FORCED':>8}{'AMBIGUOUS':>11}"
    print("\n" + header)
    print("-" * len(header))
    for season, counts in sorted(gp["per_season"].items()):
        print(
            f"{season:<8}{counts['missing']:>18}{counts['forced']:>8}{counts['ambiguous']:>11}"
        )
    print(
        f"\n{gp['missing_rows']} rows short of ESPN's games-played across "
        f"{gp['short_seasons']} athlete-seasons"
    )
    print(
        f"{verb} {gp['filled']} of them - the ones with only one possible assignment; "
        f"{gp['ambiguous']} could be any of several weeks and were left absent"
    )
    if gp["impossible"]:
        print(
            f"{gp['impossible']} had fewer candidate games than games played, which should "
            "not happen - check them",
            file=sys.stderr,
        )
    print(
        "the ambiguous ones are still counted: v_player_seasons.games_played holds ESPN's "
        "figure, so fp_ppr_per_game_played is right even where the rows are not there"
    )
    conn.close()
    return 0


def cmd_rankings(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    db.init_db(conn)
    rows = conn.execute(
        "SELECT rank, display_name, position_rank, team_abbr, games, points_total, "
        "points_per_game FROM v_rankings WHERE season = ? AND scoring = ? "
        "ORDER BY rank LIMIT ?",
        (args.season, args.scoring, args.top),
    ).fetchall()
    if not rows:
        print(
            f"No {args.scoring} rankings for {args.season}. Run:  "
            f"python -m ffdb build-top --season {args.season}",
            file=sys.stderr,
        )
        return 1
    header = f"{'#':>4}  {'PLAYER':<26}{'POS':<6}{'TEAM':<6}{'GMS':>4}{'PTS':>9}{'PTS/G':>8}"
    print(header)
    print("-" * len(header))
    for r in rows:
        print(
            f"{r['rank']:>4}  {r['display_name'] or '?':<26}{r['position_rank'] or '':<6}"
            f"{r['team_abbr'] or '':<6}{r['games']:>4}{r['points_total']:>9}"
            f"{r['points_per_game']:>8}"
        )
    conn.close()
    return 0


def cmd_schedule(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    db.init_db(conn)
    season_types = [config.SEASON_TYPE_REGULAR]
    if args.postseason:
        season_types.append(config.SEASON_TYPE_POST)

    result = pipeline.build_schedule(
        conn, _client(args), season=args.season,
        season_types=season_types, teams=args.team, force=args.force,
    )
    print(
        f"{result['season']}: {result['events']} games across {result['teams']} teams "
        f"({result['unplayed']} not played yet)"
    )

    rows = conn.execute(
        """
        SELECT week, COUNT(*) AS games,
               SUM(score IS NULL) AS upcoming,
               MIN(substr(game_date, 1, 10)) AS first_kickoff
        FROM games
        WHERE season = ? AND season_type = ? AND is_all_star = 0
        GROUP BY week
        ORDER BY week
        """,
        (args.season, config.SEASON_TYPE_REGULAR),
    ).fetchall()
    if rows:
        header = f"{'WEEK':>5}{'GMS':>5}{'UPCOMING':>10}  FIRST KICKOFF"
        print("\n" + header)
        print("-" * len(header))
        for r in rows:
            print(
                f"{r['week']:>5}{r['games']:>5}{r['upcoming']:>10}  {r['first_kickoff'] or '?'}"
            )
    conn.close()
    return 0


def cmd_defense(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    db.init_db(conn)
    season_types = [config.SEASON_TYPE_REGULAR]
    if args.postseason:
        season_types.append(config.SEASON_TYPE_POST)

    result = pipeline.build_team_defense(
        conn, _client(args), season=args.season,
        season_types=season_types, teams=args.team, force=args.force,
    )
    print(
        f"{result['season']}: {result['rows']} team-game rows from "
        f"{result['events']} events across {result['teams']} teams"
        + (f" ({len(result['skipped'])} without a box score)" if result["skipped"] else "")
    )

    rows = conn.execute(
        """
        SELECT team_abbr, COUNT(*) AS games,
               ROUND(AVG(points_allowed), 1) AS pa,
               ROUND(AVG(yards_allowed), 1)  AS ya,
               SUM(sacks)            AS sacks,
               SUM(turnovers_forced) AS tos
        FROM team_defense_games
        WHERE season = ? AND season_type = ? AND is_all_star = 0
        GROUP BY team_id
        ORDER BY pa
        """,
        (args.season, config.SEASON_TYPE_REGULAR),
    ).fetchall()
    if rows:
        header = f"{'TEAM':<6}{'GMS':>4}{'PA/G':>8}{'YDS/G':>8}{'SACKS':>7}{'TO':>5}"
        print("\n" + header)
        print("-" * len(header))
        for r in rows:
            print(
                f"{r['team_abbr'] or '?':<6}{r['games']:>4}{r['pa']:>8}"
                f"{r['ya']:>8}{r['sacks'] or 0:>7}{r['tos'] or 0:>5}"
            )
    conn.close()
    return 0


def cmd_index(args: argparse.Namespace) -> int:
    client = _client(args)
    index = athletes_mod.fetch_athlete_index(client, force=args.force)
    active = sum(1 for i in index if i.get("active"))
    print(f"{len(index)} athletes in ESPN's index ({active} active)")
    if args.search:
        matches = athletes_mod.find_athletes(index, args.search)
        if not matches:
            print(f"no match for {args.search!r}")
            return 1
        for m in matches:
            years = (m.get("experience") or {}).get("years", "?")
            state = "active" if m.get("active") else "inactive"
            print(f"  {m['id']:>8}  {m.get('displayName', '?'):<28}{state:<10}exp={years}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ffdb", description=__doc__)
    parser.add_argument("--db", default=str(config.DB_PATH), help="SQLite file to use")
    parser.add_argument("--no-cache", action="store_true", help="ignore the raw JSON archive")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="load a player's full career of game logs")
    p_add.add_argument("players", nargs="+", help='player name(s) or ESPN id(s)')
    p_add.add_argument("--season", type=int, action="append", help="limit to season(s)")
    p_add.add_argument("--force", action="store_true", help="re-fetch instead of using the archive")
    p_add.set_defaults(func=cmd_add)

    p_players = sub.add_parser("players", help="list players already in the database")
    p_players.set_defaults(func=cmd_players)

    p_show = sub.add_parser("show", help="season-by-season fantasy summary for one player")
    p_show.add_argument("player")
    p_show.set_defaults(func=cmd_show)

    p_export = sub.add_parser("export", help="dump a table or view to CSV")
    p_export.add_argument("--table", default="v_player_games")
    p_export.add_argument("--out")
    p_export.set_defaults(func=cmd_export)

    p_top = sub.add_parser(
        "build-top", help="derive the top N players from a season, then load their careers"
    )
    p_top.add_argument("--season", type=int, required=True, help="season to rank on")
    p_top.add_argument("--top", type=int, default=200)
    p_top.add_argument("--scoring", default=ranking.DEFAULT_SCORING, choices=sorted(scoring.FORMATS))
    p_top.add_argument("--force", action="store_true")
    p_top.set_defaults(func=cmd_build_top)

    p_season = sub.add_parser(
        "build-season",
        help="load every fantasy-position player who appeared in a season (no top-N filter)",
    )
    p_season.add_argument(
        "--season", action="append", required=True,
        help="season or range, e.g. --season 2020-2024 (repeatable)",
    )
    p_season.add_argument(
        "--resync", action="store_true",
        help="re-pull athlete-seasons already recorded in sync_log",
    )
    p_season.add_argument("--force", action="store_true", help="ignore the raw JSON archive")
    p_season.set_defaults(func=cmd_build_season)

    p_att = sub.add_parser(
        "attendance",
        help="recover games a player was active for that ESPN's game log omits",
    )
    p_att.add_argument(
        "--season", action="append", required=True,
        help="season or range, e.g. --season 2016-2025 (repeatable)",
    )
    p_att.add_argument(
        "--dry-run", action="store_true",
        help="classify the gaps and report, without fetching stat lines or writing",
    )
    p_att.add_argument(
        "--skip-games-played", action="store_true",
        help="event-log pass only; skip the season games-played comparison",
    )
    p_att.add_argument("--force", action="store_true", help="ignore the raw JSON archive")
    p_att.set_defaults(func=cmd_attendance)

    p_rank = sub.add_parser("rankings", help="show a stored ranking")
    p_rank.add_argument("--season", type=int, required=True)
    p_rank.add_argument("--top", type=int, default=50)
    p_rank.add_argument("--scoring", default=ranking.DEFAULT_SCORING, choices=sorted(scoring.FORMATS))
    p_rank.set_defaults(func=cmd_rankings)

    p_sched = sub.add_parser(
        "schedule", help="load a season's schedule into games, upcoming games included"
    )
    p_sched.add_argument("--season", type=int, required=True)
    p_sched.add_argument(
        "--team", action="append", help="limit to team abbreviation(s), e.g. --team KC"
    )
    p_sched.add_argument("--postseason", action="store_true", help="include playoff games")
    p_sched.add_argument(
        "--force", action="store_true", help="re-fetch instead of using the archive"
    )
    p_sched.set_defaults(func=cmd_schedule)

    p_def = sub.add_parser("defense", help="load team defense game logs for a season")
    p_def.add_argument("--season", type=int, required=True)
    p_def.add_argument(
        "--team", action="append", help="limit to team abbreviation(s), e.g. --team KC"
    )
    p_def.add_argument("--postseason", action="store_true", help="include playoff games")
    p_def.add_argument("--force", action="store_true", help="re-fetch instead of using the archive")
    p_def.set_defaults(func=cmd_defense)

    p_index = sub.add_parser("index", help="refresh/search ESPN's athlete index")
    p_index.add_argument("--search", help="look up ids for a name")
    p_index.add_argument("--force", action="store_true")
    p_index.set_defaults(func=cmd_index)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    config.ensure_dirs()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
