"""Recover games a player was active for that ESPN's game log leaves out.

A game log is not a list of games the player was available for - it is a list of
games ESPN has an offensive stat line for. A player who dressed, took snaps and
touched nothing produces no row at all, so the season simply skips that week.
That is not the same as being hurt, and in `player_games` alone the two are
indistinguishable: both are an absent row.

It matters because the absent rows are all zeros, so dropping them lifts every
per-game average - and unevenly. 2021 is short roughly 850 of them, which is the
whole reason its mean PPR reads a point above the seasons either side.

ESPN's core API settles it. Each athlete-season has an event log listing every
event the athlete was on a roster for, played or not:

    .../seasons/{season}/athletes/{id}/eventlog
        events.items[] -> {event {$ref}, teamId, played, statistics {$ref}}

Three cases fall out, and only the first can be filled:

    in the event log, played=true,  no game-log row -> played, recorded nothing
    in the event log, played=false, no game-log row -> inactive; must stay absent
    not in the event log at all                     -> not on the roster that week

That third case is the one a naive fill gets wrong. Most missing games are not
scoreless games: measured over 2020-2025, a player's team plays roughly 300
games they have no row for per 1,500 rows they do have, and around 99% of those
are genuine absences. Zero-filling every gap would invent tens of thousands of
games nobody played.

Nothing here is assumed to be zero either. Each fillable game's `statistics`
$ref is fetched and the real line stored, which comes back scoreless in almost
every case - and where it does not, the row is written with the numbers it
actually has and counted separately, rather than flattened to a zero that never
happened. Recovered rows carry `source = 'eventlog'` so they can be told apart
from ESPN's own at query time.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Iterable

from . import config, db, scoring
from .espn import ESPNClient, ESPNNotFound

log = logging.getLogger(__name__)

_EVENT_ID_RE = re.compile(r"/events/(\d+)")


# ------------------------------------------------------------------ fetching


def fetch_eventlog(
    client: ESPNClient, athlete_id: str, season: int, force: bool = False
) -> dict:
    """Every event the athlete was rostered for in one season, played or not.

    Pages are merged into a single document before caching, so the archive
    holds one file per athlete-season however ESPN chooses to paginate.
    """
    url = config.ATHLETE_EVENTLOG_URL.format(season=season, athlete_id=athlete_id)
    key = f"eventlog/{athlete_id}/{season}"
    if client.use_cache and not force:
        cached = client.read_cache(key)
        if cached is not None:
            return cached

    merged: dict | None = None
    items: list[dict] = []
    page = 1
    while True:
        payload = client.get_json(
            url,
            params={"page": page, "limit": config.EVENTLOG_PAGE_SIZE},
            cache_key=None,  # cached as one merged document below
            force=True,
        )
        events = payload.get("events") or {}
        items.extend(events.get("items") or [])
        if merged is None:
            merged = payload
        if page >= (events.get("pageCount") or 1):
            break
        page += 1

    merged = merged or {}
    merged.setdefault("events", {})["items"] = items
    if client.use_cache:
        client.write_cache(key, merged)
    return merged


def fetch_season_stats(client: ESPNClient, athlete_id: str, force: bool = False) -> dict:
    """Career season totals for one athlete. One request covers every season."""
    return client.get_json(
        config.ATHLETE_STATS_URL.format(athlete_id=athlete_id),
        cache_key=f"athlete_stats/{athlete_id}",
        force=force,
    )


def fetch_event_stats(
    client: ESPNClient, athlete_id: str, event_id: str, ref: str, force: bool = False
) -> dict | None:
    """One athlete's full stat line for one event, or None if ESPN has none."""
    try:
        return client.get_json(
            # The event log hands back http:// refs; ask for them over TLS.
            ref.replace("http://", "https://", 1),
            cache_key=f"event_stats/{athlete_id}/{event_id}",
            force=force,
        )
    except ESPNNotFound as exc:
        log.debug("no stat line for athlete %s in event %s: %s", athlete_id, event_id, exc)
        return None


# ------------------------------------------------------------------- parsing


def parse_eventlog(payload: dict) -> list[dict]:
    """event_id, team, played flag and stat-line ref for each rostered event."""
    entries: list[dict] = []
    seen: set[str] = set()
    for item in (payload.get("events") or {}).get("items") or []:
        match = _EVENT_ID_RE.search((item.get("event") or {}).get("$ref") or "")
        if not match or match.group(1) in seen:
            continue
        seen.add(match.group(1))
        entries.append(
            {
                "event_id": match.group(1),
                "team_id": str(item.get("teamId") or "") or None,
                "played": bool(item.get("played")),
                "statistics_ref": (item.get("statistics") or {}).get("$ref"),
            }
        )
    return entries


def games_played(payload: dict) -> dict[int, int]:
    """season -> games played, from the season totals payload.

    Every stat category repeats `gamesPlayed`, and a handful of athletes carry
    different figures in different categories - a player who both rushed and
    returned kicks can show the games in which each applied. The largest is the
    number of games they appeared in, which is what a denominator wants.
    """
    per_season: dict[int, int] = {}
    for category in payload.get("categories") or []:
        names = category.get("names") or []
        if "gamesPlayed" not in names:
            continue
        index = names.index("gamesPlayed")
        for entry in category.get("statistics") or []:
            season = (entry.get("season") or {}).get("year")
            stats = entry.get("stats") or []
            if season is None or index >= len(stats):
                continue
            try:
                value = int(str(stats[index]).replace(",", ""))
            except ValueError:
                continue
            per_season[season] = max(per_season.get(season, 0), value)
    return per_season


def parse_event_stats(payload: dict) -> dict[str, float | None]:
    """Flatten the categorised stat line to one name -> value mapping.

    ESPN groups the same key under several categories (`fumbles` appears under
    general, rushing and receiving); the first occurrence wins, matching how the
    game log presents a single column per key.
    """
    stats: dict[str, float | None] = {}
    for category in (payload.get("splits") or {}).get("categories") or []:
        for stat in category.get("stats") or []:
            name = stat.get("name")
            if name and name not in stats:
                value = stat.get("value")
                stats[name] = float(value) if isinstance(value, (int, float)) else None
    return stats


# ---------------------------------------------------------------- gap finding


def _game_index(conn: sqlite3.Connection, seasons: Iterable[int]) -> dict[str, sqlite3.Row]:
    """Played, non-exhibition games for the given seasons, keyed by event id."""
    season_list = sorted(set(seasons))
    marks = ", ".join("?" for _ in season_list)
    return {
        row["event_id"]: row
        for row in conn.execute(
            f"SELECT * FROM games WHERE season IN ({marks}) "
            "AND is_all_star = 0 AND score IS NOT NULL",
            season_list,
        )
    }


def _team_abbreviations(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["team_id"]: r["abbreviation"] for r in conn.execute("SELECT * FROM teams")}


def loaded_athlete_seasons(
    conn: sqlite3.Connection, seasons: Iterable[int]
) -> dict[tuple[str, int], set[str]]:
    """(athlete, season) -> the events already stored, for seasons with rows.

    Only athlete-seasons that already have at least one game-log row are
    returned. An athlete-season with none is not a player who was quietly
    scoreless all year - it is one of the athletes whose game log ESPN serves
    empty (see the README), and filling a whole season of zeros for them would
    manufacture a career out of an upstream outage.
    """
    season_list = sorted(set(seasons))
    marks = ", ".join("?" for _ in season_list)
    loaded: dict[tuple[str, int], set[str]] = {}
    for row in conn.execute(
        f"SELECT athlete_id, season, event_id FROM player_games "
        f"WHERE season IN ({marks}) AND is_all_star = 0",
        season_list,
    ):
        loaded.setdefault((row["athlete_id"], row["season"]), set()).add(row["event_id"])
    return loaded


def classify(
    entries: Iterable[dict], have: set[str], games: dict[str, sqlite3.Row]
) -> dict[str, list[dict]]:
    """Split one athlete-season's event log into what can and cannot be filled.

    `fillable` played it and has no row; `inactive` was rostered but did not
    play; `unknown_event` is in the event log but not in `games`, which is
    normally a preseason game and never something to store.
    """
    result: dict[str, list[dict]] = {"fillable": [], "inactive": [], "unknown_event": []}
    for entry in entries:
        if entry["event_id"] in have:
            continue
        if entry["event_id"] not in games:
            result["unknown_event"].append(entry)
        elif entry["played"]:
            result["fillable"].append(entry)
        else:
            result["inactive"].append(entry)
    return result


# ---------------------------------------------------------------- row building


def build_row(
    athlete_id: str,
    game: sqlite3.Row,
    team_id: str | None,
    stats: dict[str, float | None],
    keep_stats: Iterable[str],
    abbreviations: dict[str, str] | None = None,
) -> dict:
    """A `player_games` row for a game the log skipped.

    `keep_stats` limits which recovered keys reach their own column. The core
    API ships a much wider, partly derived vocabulary than the game log does
    (`ESPNRBRating`, `netYardsPerGame`, `teamGamesPlayed`), and letting all 80
    of them in would grow `player_games` columns that are populated on a
    fraction of a percent of rows. The complete mapping is kept in `raw_stats`
    either way, so nothing is discarded - only kept out of the schema.

    Raises ValueError if the event log's team did not play this game, rather
    than guess a side: every field below reads off which one it was.
    """
    home_id = game["home_team_id"]
    away_id = game["away_team_id"]
    if team_id not in (home_id, away_id):
        raise ValueError(
            f"team {team_id!r} did not play event {game['event_id']} ({away_id} at {home_id})"
        )

    is_home = team_id == home_id
    opponent_id = away_id if is_home else home_id
    team_score = game["home_score"] if is_home else game["away_score"]
    opponent_score = game["away_score"] if is_home else game["home_score"]
    result = None
    if team_score is not None and opponent_score is not None:
        result = "W" if team_score > opponent_score else "L" if team_score < opponent_score else "T"

    wanted = set(keep_stats)
    abbreviations = abbreviations or {}
    return {
        "athlete_id": str(athlete_id),
        "event_id": game["event_id"],
        "season": game["season"],
        "season_type": game["season_type"],
        "week": game["week"],
        "game_date": (game["game_date"] or "")[:10] or None,
        "team_id": team_id,
        "team_abbr": abbreviations.get(team_id),
        "opponent_id": opponent_id,
        "opponent_abbr": abbreviations.get(opponent_id),
        "home_away": "home" if is_home else "away",
        "result": result,
        "team_score": team_score,
        "opponent_score": opponent_score,
        "is_all_star": game["is_all_star"],
        "raw_stats": json.dumps(stats),
        "source": "eventlog",
        "_stats": {k: v for k, v in stats.items() if k in wanted},
    }


def known_stat_keys(conn: sqlite3.Connection) -> set[str]:
    """The stat vocabulary `player_games` already uses.

    Taken from `stat_catalog` rather than hardcoded so a recovered row can only
    ever populate columns the game logs themselves put there.
    """
    return {
        row["stat_name"]
        for row in conn.execute(
            "SELECT stat_name FROM stat_catalog WHERE table_name = 'player_games'"
        )
    }


# ------------------------------------------------- recovery from games played


def forced_assignment(missing: int, candidates: list[dict]) -> list[dict] | None:
    """The games to fill, when the shortfall can only be assigned one way.

    ESPN's season total says how many games a player appeared in, never which.
    That is enough on its own only when the number of games they were rostered
    for and have no row for is exactly the number missing - then every one of
    them must be a game they played, and no choice is being made. With more
    candidates than missing rows, picking any subset would be inventing which
    weeks a player was on the field, so None comes back and they stay absent.
    """
    if missing <= 0 or len(candidates) != missing:
        return None
    return candidates


def fill_from_games_played(
    conn: sqlite3.Connection,
    client: ESPNClient,
    seasons: Iterable[int],
    dry_run: bool = False,
    force: bool = False,
) -> dict:
    """Store ESPN's games-played count, and fill the games it pins down.

    Runs after the event-log pass, because it only has anything to say about
    games that pass left behind. One request per athlete covers a whole career.
    """
    season_list = sorted(set(seasons))
    games = {
        event_id: row
        for event_id, row in _game_index(conn, season_list).items()
        if row["season_type"] == config.SEASON_TYPE_REGULAR
    }
    abbr = _team_abbreviations(conn)

    # Regular season only on both sides: ESPN's games-played excludes playoffs.
    have: dict[str, dict[int, set[str]]] = {}
    marks = ", ".join("?" for _ in season_list)
    for row in conn.execute(
        f"SELECT athlete_id, season, event_id FROM player_games WHERE season IN ({marks}) "
        "AND season_type = 2 AND is_all_star = 0",
        season_list,
    ):
        have.setdefault(row["athlete_id"], {}).setdefault(row["season"], set()).add(row["event_id"])

    tally = dict.fromkeys(
        ("athletes", "no_stats", "seasons_recorded", "short_seasons",
         "missing_rows", "forced", "ambiguous", "impossible", "filled"), 0
    )
    per_season: dict[int, dict[str, int]] = {
        s: {"missing": 0, "forced": 0, "ambiguous": 0} for s in season_list
    }
    season_rows: list[dict] = []
    filled_rows: list[dict] = []

    log.info("reading season totals for %d athletes", len(have))
    for i, (athlete_id, by_season) in enumerate(sorted(have.items()), 1):
        try:
            payload = fetch_season_stats(client, athlete_id, force=force)
        except Exception as exc:  # noqa: BLE001 - one bad athlete must not stop the sweep
            log.error("season totals failed for %s: %s", athlete_id, exc)
            tally["no_stats"] += 1
            continue
        tally["athletes"] += 1
        counts = games_played(payload)

        for season, events in by_season.items():
            gp = counts.get(season)
            if gp is None:
                continue
            season_rows.append(
                {"athlete_id": athlete_id, "season": season, "games_played": gp}
            )
            tally["seasons_recorded"] += 1

            missing = gp - len(events)
            if missing <= 0:
                continue
            tally["short_seasons"] += 1
            tally["missing_rows"] += missing
            per_season[season]["missing"] += missing

            try:
                entries = parse_eventlog(fetch_eventlog(client, athlete_id, season, force=force))
            except Exception as exc:  # noqa: BLE001
                log.error("event log failed for %s %s: %s", athlete_id, season, exc)
                continue
            candidates = [
                e for e in entries if e["event_id"] in games and e["event_id"] not in events
            ]
            assigned = forced_assignment(missing, candidates)
            if assigned is None:
                bucket = "impossible" if len(candidates) < missing else "ambiguous"
                tally[bucket] += missing
                if bucket == "ambiguous":
                    per_season[season]["ambiguous"] += missing
                continue

            tally["forced"] += missing
            per_season[season]["forced"] += missing
            if dry_run:
                continue
            for entry in assigned:
                try:
                    row = build_row(
                        athlete_id, games[entry["event_id"]], entry["team_id"], {}, (), abbr
                    )
                except ValueError as exc:
                    log.error("skipping %s: %s", athlete_id, exc)
                    continue
                # No stat line exists for these - that is why the game log has no
                # row. The zero is the point: ESPN counts the appearance, and a
                # player who had scored would be in the box score and logged.
                row["source"] = "inferred"
                row.update({"fp_standard": 0.0, "fp_half_ppr": 0.0, "fp_ppr": 0.0})
                filled_rows.append(row)
                tally["filled"] += 1

        if len(season_rows) >= 500 and not dry_run:
            db.upsert_athlete_seasons(conn, season_rows)
            season_rows.clear()
        if filled_rows and len(filled_rows) >= 200 and not dry_run:
            db.upsert_player_games(conn, filled_rows)
            filled_rows.clear()
        if i % 250 == 0:
            log.info(
                "[%d/%d] athletes read; %d missing rows, %d of them forced",
                i, len(have), tally["missing_rows"], tally["forced"],
            )

    if not dry_run:
        if season_rows:
            db.upsert_athlete_seasons(conn, season_rows)
        if filled_rows:
            db.upsert_player_games(conn, filled_rows)
        db.rebuild_views(conn)

    return {**tally, "per_season": per_season, "seasons": season_list}


# ------------------------------------------------------------------ the sweep


def fill_seasons(
    conn: sqlite3.Connection,
    client: ESPNClient,
    seasons: Iterable[int],
    dry_run: bool = False,
    force: bool = False,
) -> dict:
    """Recover every played-but-unlogged game across the given seasons.

    One request per athlete-season for the event log, plus one per game
    actually filled. `dry_run` does the classification and stops before the
    stat-line fetches, which is the cheap way to size a run.
    """
    season_list = sorted(set(seasons))
    games = _game_index(conn, season_list)
    abbr = _team_abbreviations(conn)
    keep_stats = known_stat_keys(conn)
    loaded = loaded_athlete_seasons(conn, season_list)

    if not games:
        log.warning("no played games stored for %s - load the schedule first", season_list)
    log.info(
        "%d athlete-seasons to check across %s (%d games in scope)",
        len(loaded), season_list, len(games),
    )

    tally = dict.fromkeys(
        (
            "eventlog_missing",
            "fillable",
            "inactive",
            "unknown_event",
            "filled",
            "no_stat_line",
            "not_actually_scoreless",
            "wrong_team",
        ),
        0,
    )
    per_season: dict[int, dict[str, int]] = {
        s: {"fillable": 0, "inactive": 0, "filled": 0, "scoring": 0} for s in season_list
    }
    scoring_rows: list[tuple[str, str, float]] = []
    rows: list[dict] = []

    for i, ((athlete_id, season), have) in enumerate(sorted(loaded.items()), 1):
        try:
            payload = fetch_eventlog(client, athlete_id, season, force=force)
        except Exception as exc:  # noqa: BLE001 - one bad athlete must not stop the sweep
            log.error("event log failed for %s %s: %s", athlete_id, season, exc)
            tally["eventlog_missing"] += 1
            continue

        split = classify(parse_eventlog(payload), have, games)
        tally["fillable"] += len(split["fillable"])
        tally["inactive"] += len(split["inactive"])
        tally["unknown_event"] += len(split["unknown_event"])
        per_season[season]["fillable"] += len(split["fillable"])
        per_season[season]["inactive"] += len(split["inactive"])

        if not dry_run:
            for entry in split["fillable"]:
                game = games[entry["event_id"]]
                stats: dict[str, float | None] = {}
                if entry["statistics_ref"]:
                    stat_payload = fetch_event_stats(
                        client, athlete_id, entry["event_id"], entry["statistics_ref"], force=force
                    )
                    if stat_payload is None:
                        tally["no_stat_line"] += 1
                    else:
                        stats = parse_event_stats(stat_payload)
                else:
                    tally["no_stat_line"] += 1

                try:
                    row = build_row(
                        athlete_id, game, entry["team_id"], stats, keep_stats, abbr
                    )
                except ValueError as exc:
                    log.error("skipping %s: %s", athlete_id, exc)
                    tally["wrong_team"] += 1
                    continue
                row.update(scoring.all_formats(row["_stats"]))
                if row["fp_ppr"]:
                    # The premise of this whole exercise is that ESPN only omits
                    # games worth nothing. Where that turns out to be false the
                    # real line is stored anyway - a wrong zero would be worse
                    # than a surprising number - but it gets said out loud.
                    tally["not_actually_scoreless"] += 1
                    per_season[season]["scoring"] += 1
                    scoring_rows.append((athlete_id, entry["event_id"], row["fp_ppr"]))
                rows.append(row)
                per_season[season]["filled"] += 1
                tally["filled"] += 1

            if len(rows) >= 200:
                db.upsert_player_games(conn, rows)
                rows.clear()

        if i % 250 == 0:
            log.info(
                "[%d/%d] athlete-seasons checked; %d fillable, %d inactive",
                i, len(loaded), tally["fillable"], tally["inactive"],
            )

    if rows:
        db.upsert_player_games(conn, rows)
    if not dry_run:
        db.rebuild_views(conn)

    if scoring_rows:
        log.warning(
            "%d recovered games were not scoreless after all (first 10: %s)",
            len(scoring_rows),
            ", ".join(f"{a}/{e}={p}" for a, e, p in scoring_rows[:10]),
        )

    return {
        **tally,
        "athlete_seasons": len(loaded),
        "per_season": per_season,
        "seasons": season_list,
    }
