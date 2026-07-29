"""Unit tests for the parsing and scoring layers.

Run with:  python -m unittest discover -s tests
These use synthetic payloads, so they never touch the network.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffdb import athletes, db, gamelog, ranking, rosters, scoring, teamdefense  # noqa: E402


def make_payload() -> dict:
    """Two real games plus a Pro Bowl, in ESPN's response shape."""
    return {
        "names": ["rushingAttempts", "rushingYards", "rushingTouchdowns", "receptions", "fumbles"],
        "labels": ["CAR", "YDS", "TD", "REC", "FUM"],
        "filters": [
            {
                "name": "season",
                "options": [{"value": "2021"}, {"value": "2020"}, {"value": "bad"}],
            }
        ],
        "events": {
            "1": {
                "id": "1",
                "week": 3,
                "atVs": "@",
                "gameDate": "2021-09-26T17:00:00.000+00:00",
                "homeTeamId": "10",
                "awayTeamId": "11",
                "homeTeamScore": "25",
                "awayTeamScore": "16",
                "gameResult": "L",
                "team": {"id": "11", "abbreviation": "IND", "isAllStar": False},
                "opponent": {"id": "10", "abbreviation": "TEN"},
            },
            "2": {
                "id": "2",
                "week": 6,
                "atVs": "vs",
                "gameDate": "2021-10-17T17:00:00.000+00:00",
                "homeTeamId": "11",
                "awayTeamId": "34",
                "homeTeamScore": "31",
                "awayTeamScore": "3",
                "gameResult": "W",
                "team": {"id": "11", "abbreviation": "IND", "isAllStar": False},
                "opponent": {"id": "34", "abbreviation": "HOU"},
            },
            "99": {
                "id": "99",
                "week": 4,
                "atVs": "vs",
                "gameDate": "2022-02-06T19:00:00.000+00:00",
                "homeTeamId": "31",
                "awayTeamId": "32",
                "homeTeamScore": "41",
                "awayTeamScore": "35",
                "gameResult": "W",
                "team": {"id": "31", "abbreviation": "AFC", "isAllStar": True},
                "opponent": {"id": "32", "abbreviation": "NFC"},
            },
        },
        "seasonTypes": [
            {
                "displayName": "2021 Regular Season",
                "categories": [
                    {
                        "type": "event",
                        "events": [
                            {"eventId": "1", "stats": ["10", "64", "0", "1", "-"]},
                            {"eventId": "2", "stats": ["14", "145", "2", "1", "0"]},
                        ],
                    },
                    # Aggregate split that must be ignored - same event ids, totals.
                    {
                        "type": "total",
                        "events": [{"eventId": "1", "stats": ["24", "209", "2", "2", "0"]}],
                    },
                ],
            },
            {
                "displayName": "2021 Postseason",
                "categories": [
                    {"type": "event", "events": [{"eventId": "99", "stats": ["3", "8", "0", "2", "0"]}]}
                ],
            },
        ],
    }


class TestGamelogParsing(unittest.TestCase):
    def setUp(self) -> None:
        self.games, self.rows = gamelog.parse_gamelog("4242335", 2021, make_payload())
        self.by_event = {r["event_id"]: r for r in self.rows}

    def test_one_row_per_event(self):
        # The 'total' split repeats event 1; it must not create a duplicate.
        self.assertEqual(len(self.rows), 3)
        self.assertEqual(len(self.games), 3)
        self.assertEqual(len({r["event_id"] for r in self.rows}), 3)

    def test_stats_align_with_names(self):
        row = self.by_event["2"]
        self.assertEqual(row["_stats"]["rushingAttempts"], 14.0)
        self.assertEqual(row["_stats"]["rushingYards"], 145.0)
        self.assertEqual(row["_stats"]["rushingTouchdowns"], 2.0)

    def test_dash_becomes_null_not_zero(self):
        # '-' means "stat did not apply", which is not the same as 0.
        self.assertIsNone(self.by_event["1"]["_stats"]["fumbles"])
        self.assertEqual(self.by_event["2"]["_stats"]["fumbles"], 0.0)

    def test_home_away_and_scores(self):
        away = self.by_event["1"]
        self.assertEqual(away["home_away"], "away")
        self.assertEqual(away["team_score"], 16.0)
        self.assertEqual(away["opponent_score"], 25.0)
        self.assertEqual(away["opponent_abbr"], "TEN")

        home = self.by_event["2"]
        self.assertEqual(home["home_away"], "home")
        self.assertEqual(home["team_score"], 31.0)
        self.assertEqual(home["opponent_score"], 3.0)

    def test_pro_bowl_flagged(self):
        self.assertEqual(self.by_event["99"]["is_all_star"], 1)
        self.assertEqual(self.by_event["1"]["is_all_star"], 0)
        self.assertEqual(self.by_event["99"]["season_type"], gamelog.config.SEASON_TYPE_POST)

    def test_raw_stats_preserved(self):
        raw = json.loads(self.by_event["1"]["raw_stats"])
        self.assertEqual(raw["fumbles"], "-")
        self.assertEqual(raw["rushingYards"], "64")

    def test_seasons_from_filters_skips_junk(self):
        self.assertEqual(gamelog.seasons_from_payload(make_payload()), [2020, 2021])

    def test_empty_payload(self):
        self.assertEqual(gamelog.parse_gamelog("1", 2021, {"filters": []}), ([], []))


class TestScoring(unittest.TestCase):
    def test_ppr_formats(self):
        stats = {
            "rushingYards": 145.0,
            "rushingTouchdowns": 2.0,
            "receptions": 3.0,
            "receivingYards": 20.0,
            "fumblesLost": 1.0,
        }
        # 14.5 + 12 + 2 - 2 = 26.5 before receptions
        self.assertAlmostEqual(scoring.compute_points(stats, 0.0), 26.5)
        self.assertAlmostEqual(scoring.compute_points(stats, 0.5), 28.0)
        self.assertAlmostEqual(scoring.compute_points(stats, 1.0), 29.5)

    def test_passing_interceptions_penalised(self):
        qb = {"passingYards": 300.0, "passingTouchdowns": 3.0, "interceptions": 2.0,
              "passingAttempts": 35.0}
        # 12 + 12 - 4
        self.assertAlmostEqual(scoring.compute_points(qb, 1.0), 20.0)

    def test_defensive_interceptions_not_penalised(self):
        # Same key, opposite meaning: a pick by a defender must not subtract.
        defender = {"interceptions": 2.0, "receptions": 0.0}
        self.assertAlmostEqual(scoring.compute_points(defender, 1.0), 0.0)

    def test_missing_stats_are_zero(self):
        self.assertEqual(scoring.compute_points({}, 1.0), 0.0)
        self.assertEqual(scoring.compute_points({"rushingYards": None}, 1.0), 0.0)

    def test_all_formats_keys(self):
        keys = set(scoring.all_formats({"rushingYards": 100.0}))
        self.assertEqual(keys, {"fp_standard", "fp_half_ppr", "fp_ppr"})


class TestNameNormalisation(unittest.TestCase):
    def test_suffix_and_punctuation_stripped(self):
        self.assertEqual(athletes.normalize_name("Ja'Marr Chase"), "jamarr chase")
        self.assertEqual(athletes.normalize_name("Marvin Harrison Jr."), "marvin harrison")
        self.assertEqual(athletes.normalize_name("A.J. Brown"), "aj brown")

    def test_accents_folded(self):
        self.assertEqual(athletes.normalize_name("Equanimeous St. Brown"), "equanimeous st brown")
        self.assertEqual(athletes.normalize_name("José Álvarez"), "jose alvarez")

    def test_active_players_rank_first(self):
        index = [
            {"id": "1", "displayName": "John Smith", "active": False, "experience": {"years": 9}},
            {"id": "2", "displayName": "John Smith", "active": True, "experience": {"years": 2}},
        ]
        self.assertEqual([m["id"] for m in athletes.find_athletes(index, "John Smith")], ["2", "1"])


class TestDatabase(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = db.connect(":memory:")
        db.init_db(self.conn)
        db.upsert_athlete(self.conn, {"athlete_id": "4242335", "display_name": "Jonathan Taylor"})

    def tearDown(self) -> None:
        self.conn.close()

    def _load(self):
        games, rows = gamelog.parse_gamelog("4242335", 2021, make_payload())
        for row in rows:
            row.update(scoring.all_formats(row["_stats"]))
        db.upsert_games(self.conn, games)
        n = db.upsert_player_games(self.conn, rows)
        db.rebuild_views(self.conn)
        return n

    def test_stat_columns_created_on_demand(self):
        self._load()
        columns = db.existing_columns(self.conn, "player_games")
        self.assertIn("rushingYards", columns)
        self.assertIn("receptions", columns)

    def test_reload_is_idempotent(self):
        self.assertEqual(self._load(), 3)
        self.assertEqual(self._load(), 3)
        count = self.conn.execute("SELECT COUNT(*) FROM player_games").fetchone()[0]
        self.assertEqual(count, 3)

    def test_views_exclude_all_star_games(self):
        self._load()
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM v_player_games").fetchone()[0], 2
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM player_games").fetchone()[0], 3
        )

    def test_view_sees_columns_added_after_creation(self):
        db.rebuild_views(self.conn)  # views exist before any stat column does
        self._load()
        row = self.conn.execute(
            "SELECT rushingYards FROM v_player_games WHERE event_id = '2'"
        ).fetchone()
        self.assertEqual(row["rushingYards"], 145.0)

    def test_colliding_stat_name_is_prefixed(self):
        self.assertEqual(db.column_for_stat("week"), "stat_week")
        self.assertEqual(db.column_for_stat("rushingYards"), "rushingYards")


class TestRosterParsing(unittest.TestCase):
    def test_roster_entry_becomes_athlete_row(self):
        athlete = {
            "id": "4242335",
            "firstName": "Jonathan",
            "lastName": "Taylor",
            "displayName": "Jonathan Taylor",
            "weight": 226.0,
            "height": 70.0,
            "dateOfBirth": "1999-01-19T08:00Z",
            "birthPlace": {"city": "Salem", "state": "NJ", "country": "USA"},
            "experience": {"years": 7},
            "jersey": "28",
            "position": {"abbreviation": "RB", "displayName": "Running Back"},
            "status": {"name": "Active", "type": "active"},
        }
        team = {"team_id": "11", "abbreviation": "IND"}
        row = rosters._profile_from_roster(athlete, team)

        self.assertEqual(row["athlete_id"], "4242335")
        self.assertEqual(row["position_abbr"], "RB")
        self.assertEqual(row["team_abbr"], "IND")
        self.assertEqual(row["height_inches"], 70)
        self.assertEqual(row["birth_date"], "1999-01-19")
        self.assertEqual(row["active"], 1)

    def test_shape_matches_athletes_table(self):
        # A roster row is inserted straight into `athletes`, so every key it
        # produces has to be a real column.
        row = rosters._profile_from_roster(
            {"id": "1", "position": {}, "status": {}}, {"team_id": "11"}
        )
        conn = db.connect(":memory:")
        db.init_db(conn)
        self.assertTrue(set(row) <= db.existing_columns(conn, "athletes"))
        db.upsert_athlete(conn, row)  # must not raise
        conn.close()

    def test_inactive_status(self):
        row = rosters._profile_from_roster(
            {"id": "1", "position": {}, "status": {"name": "Injured Reserve", "type": "injured"}},
            {"team_id": "11"},
        )
        self.assertEqual(row["active"], 0)


class TestRanking(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = db.connect(":memory:")
        db.init_db(self.conn)
        # Three players: a big scorer, a mid scorer, and one whose points come
        # from a Pro Bowl and the postseason (neither should count).
        players = [
            ("1", "Big Scorer", "RB"),
            ("2", "Mid Scorer", "WR"),
            ("3", "Exhibition Only", "TE"),
        ]
        for athlete_id, name, position in players:
            db.upsert_athlete(
                self.conn,
                {"athlete_id": athlete_id, "display_name": name, "position_abbr": position},
            )

        rows = [
            self._game("1", "g1", 2, 0, 30.0),
            self._game("1", "g2", 2, 0, 20.0),
            self._game("2", "g3", 2, 0, 15.0),
            self._game("3", "g4", 3, 1, 99.0),   # Pro Bowl
            self._game("3", "g5", 3, 0, 40.0),   # postseason
        ]
        db.upsert_games(
            self.conn, [{"event_id": r["event_id"], "season": 2025} for r in rows]
        )
        db.upsert_player_games(self.conn, rows)
        db.rebuild_views(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    @staticmethod
    def _game(athlete_id, event_id, season_type, all_star, points):
        return {
            "athlete_id": athlete_id,
            "event_id": event_id,
            "season": 2025,
            "season_type": season_type,
            "is_all_star": all_star,
            "fp_ppr": points,
            "fp_half_ppr": points,
            "fp_standard": points,
            "_stats": {"rushingYards": points},
        }

    def test_ranks_by_regular_season_points(self):
        ranked = ranking.compute_rankings(self.conn, 2025, top=10)
        self.assertEqual([r["athlete_id"] for r in ranked], ["1", "2"])
        self.assertEqual(ranked[0]["rank"], 1)
        self.assertEqual(ranked[0]["points_total"], 50.0)
        self.assertEqual(ranked[0]["points_per_game"], 25.0)

    def test_postseason_and_pro_bowl_excluded(self):
        # Player 3 has 139 points, all of it postseason/exhibition -> unranked.
        ranked = ranking.compute_rankings(self.conn, 2025, top=10)
        self.assertNotIn("3", [r["athlete_id"] for r in ranked])

    def test_position_rank_counts_per_position(self):
        ranked = ranking.compute_rankings(self.conn, 2025, top=10)
        self.assertEqual((ranked[0]["position_abbr"], ranked[0]["position_rank"]), ("RB", 1))
        self.assertEqual((ranked[1]["position_abbr"], ranked[1]["position_rank"]), ("WR", 1))

    def test_top_n_truncates(self):
        self.assertEqual(len(ranking.compute_rankings(self.conn, 2025, top=1)), 1)

    def test_rankings_are_replaced_not_appended(self):
        ranking.compute_rankings(self.conn, 2025, top=10)
        ranking.compute_rankings(self.conn, 2025, top=10)
        count = self.conn.execute("SELECT COUNT(*) FROM rankings").fetchone()[0]
        self.assertEqual(count, 2)

    def test_unknown_scoring_format_rejected(self):
        with self.assertRaises(ValueError):
            ranking.compute_rankings(self.conn, 2025, scoring_format="superflex")

    def test_ranked_ids_round_trip(self):
        ranking.compute_rankings(self.conn, 2025, top=10)
        self.assertEqual(ranking.ranked_athlete_ids(self.conn, 2025), ["1", "2"])


def make_schedule_payload() -> dict:
    """One played game and one that has not kicked off yet."""
    return {
        "events": [
            {
                "id": "401671789",
                "date": "2024-09-06T00:40Z",
                "shortName": "BAL @ KC",
                "season": {"year": 2024},
                "seasonType": {"type": 2, "name": "Regular Season"},
                "week": {"number": 1},
                "competitions": [
                    {
                        "id": "401671789",
                        "competitors": [
                            {
                                "id": "12",
                                "homeAway": "home",
                                "score": {"value": 27.0},
                                "team": {"id": "12", "abbreviation": "KC"},
                            },
                            {
                                "id": "33",
                                "homeAway": "away",
                                "score": {"value": 20.0},
                                "team": {"id": "33", "abbreviation": "BAL"},
                            },
                        ],
                    }
                ],
            },
            {
                "id": "999",
                "date": "2024-09-15T17:00Z",
                "season": {"year": 2024},
                "seasonType": {"type": 2, "name": "Regular Season"},
                "week": {"number": 2},
                "competitions": [
                    {
                        "id": "999",
                        "competitors": [
                            {"id": "12", "homeAway": "away", "team": {"id": "12"}},
                            {"id": "3", "homeAway": "home", "team": {"id": "3"}},
                        ],
                    }
                ],
            },
        ]
    }


def make_competitor_stats(**overrides) -> dict:
    """A statistics payload in the core API's category/stat shape."""
    categories = {
        "defensive": {"sacks": 1.0, "totalTackles": 60.0, "pointsAllowed": 0.0},
        "defensiveInterceptions": {"interceptions": 0.0, "interceptionYards": 0.0},
        "passing": {"totalYards": 353.0, "netPassingYards": 281.0},
        "rushing": {"rushingYards": 72.0},
        "scoring": {"passingTouchdowns": 3.0, "rushingTouchdowns": 1.0},
        "miscellaneous": {"totalPlays": 60.0, "firstDowns": 20.0, "totalGiveaways": 1.0},
        "returning": {"fumbleRecoveries": 1.0},
    }
    for category, stats in overrides.items():
        categories.setdefault(category, {}).update(stats)
    return {
        "splits": {
            "categories": [
                {
                    "name": name,
                    "stats": [{"name": k, "value": v} for k, v in stats.items()],
                }
                for name, stats in categories.items()
            ]
        }
    }


class TestScheduleParsing(unittest.TestCase):
    def setUp(self) -> None:
        self.matchups = teamdefense.parse_schedule(make_schedule_payload())

    def test_unplayed_games_dropped(self):
        # The week 2 event has no score, so there are no stats to store.
        self.assertEqual([m["event_id"] for m in self.matchups], ["401671789"])

    def test_home_and_away_identified(self):
        m = self.matchups[0]
        self.assertEqual(m["home"]["team_abbr"], "KC")
        self.assertEqual(m["away"]["team_abbr"], "BAL")
        self.assertEqual((m["season"], m["week"], m["season_type"]), (2024, 1, 2))

    def test_game_row_matches_games_table(self):
        row = teamdefense.game_row(self.matchups[0])
        conn = db.connect(":memory:")
        db.init_db(conn)
        self.assertTrue(set(row) <= db.existing_columns(conn, "games"))
        db.upsert_games(conn, [row])  # must not raise
        conn.close()


class TestTeamDefenseParsing(unittest.TestCase):
    def setUp(self) -> None:
        self.matchup = teamdefense.parse_schedule(make_schedule_payload())[0]
        self.kc = teamdefense.parse_competitor_stats(make_competitor_stats())
        self.bal = teamdefense.parse_competitor_stats(
            make_competitor_stats(
                defensive={"sacks": 2.0, "totalTackles": 55.0},
                defensiveInterceptions={"interceptions": 1.0},
                passing={"totalYards": 452.0, "netPassingYards": 267.0},
                rushing={"rushingYards": 185.0},
                miscellaneous={"totalGiveaways": 1.0},
                returning={"fumbleRecoveries": 0.0},
            )
        )

    def _kc_row(self) -> dict:
        return teamdefense.build_defense_row(
            self.matchup, self.matchup["home"], self.matchup["away"], self.kc, self.bal
        )

    def test_points_allowed_is_the_opponents_score(self):
        row = self._kc_row()
        self.assertEqual(row["points_allowed"], 20.0)
        self.assertEqual(row["team_score"], 27.0)
        self.assertEqual(row["result"], "W")

    def test_allowed_columns_come_from_the_opposing_offense(self):
        row = self._kc_row()
        self.assertEqual(row["yards_allowed"], 452.0)
        self.assertEqual(row["pass_yards_allowed"], 267.0)
        self.assertEqual(row["rush_yards_allowed"], 185.0)

    def test_espn_zero_filled_allowed_stats_are_not_trusted(self):
        # `defensive.pointsAllowed` is 0 in the payload for older seasons; the
        # derived column has to come from the score instead.
        row = self._kc_row()
        self.assertEqual(row["_stats"]["pointsAllowed"], 0.0)
        self.assertEqual(row["points_allowed"], 20.0)

    def test_own_defensive_stats_kept(self):
        row = self._kc_row()
        self.assertEqual(row["_stats"]["sacks"], 1.0)
        self.assertEqual(row["_stats"]["totalTackles"], 60.0)

    def test_takeaways_read_from_the_defenses_own_line(self):
        row = self._kc_row()
        self.assertEqual(row["fumbles_recovered"], 1.0)
        self.assertEqual(row["turnovers_forced"], 1.0)  # opponent giveaways

    def test_both_sides_are_mirror_images(self):
        kc = self._kc_row()
        bal = teamdefense.build_defense_row(
            self.matchup, self.matchup["away"], self.matchup["home"], self.bal, self.kc
        )
        self.assertEqual(bal["points_allowed"], kc["team_score"])
        self.assertEqual(kc["points_allowed"], bal["team_score"])
        self.assertEqual(bal["yards_allowed"], 353.0)
        self.assertEqual(bal["result"], "L")
        self.assertEqual(bal["home_away"], "away")

    def test_missing_payload_yields_no_categories(self):
        self.assertEqual(teamdefense.parse_competitor_stats(None), {})


class TestTeamDefenseStorage(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = db.connect(":memory:")
        db.init_db(self.conn)
        self.matchup = teamdefense.parse_schedule(make_schedule_payload())[0]
        db.upsert_teams(
            self.conn,
            [
                {"team_id": "12", "abbreviation": "KC", "display_name": "Kansas City Chiefs"},
                {"team_id": "33", "abbreviation": "BAL", "display_name": "Baltimore Ravens"},
            ],
        )
        db.upsert_games(self.conn, [teamdefense.game_row(self.matchup)])

    def tearDown(self) -> None:
        self.conn.close()

    def _rows(self) -> list[dict]:
        kc = teamdefense.parse_competitor_stats(make_competitor_stats())
        bal = teamdefense.parse_competitor_stats(
            make_competitor_stats(passing={"totalYards": 452.0})
        )
        return [
            teamdefense.build_defense_row(
                self.matchup, self.matchup["home"], self.matchup["away"], kc, bal
            ),
            teamdefense.build_defense_row(
                self.matchup, self.matchup["away"], self.matchup["home"], bal, kc
            ),
        ]

    def test_two_rows_per_game(self):
        self.assertEqual(db.upsert_team_defense_games(self.conn, self._rows()), 2)
        count = self.conn.execute("SELECT COUNT(*) FROM team_defense_games").fetchone()[0]
        self.assertEqual(count, 2)

    def test_reload_is_idempotent(self):
        db.upsert_team_defense_games(self.conn, self._rows())
        db.upsert_team_defense_games(self.conn, self._rows())
        count = self.conn.execute("SELECT COUNT(*) FROM team_defense_games").fetchone()[0]
        self.assertEqual(count, 2)

    def test_defensive_stat_columns_created(self):
        db.upsert_team_defense_games(self.conn, self._rows())
        columns = db.existing_columns(self.conn, "team_defense_games")
        self.assertIn("sacks", columns)
        self.assertIn("totalTackles", columns)

    def test_stat_catalog_separates_the_two_tables(self):
        # `sacks` is taken on a QB game log and given up on a defense; the two
        # meanings have to coexist in the catalog.
        db.upsert_team_defense_games(self.conn, self._rows())
        db.upsert_athlete(self.conn, {"athlete_id": "1", "display_name": "A QB"})
        db.upsert_player_games(
            self.conn,
            [{"athlete_id": "1", "event_id": "401671789", "_stats": {"sacks": 3.0}}],
        )
        tables = [
            r["table_name"]
            for r in self.conn.execute(
                "SELECT table_name FROM stat_catalog WHERE stat_name = 'sacks' ORDER BY table_name"
            )
        ]
        self.assertEqual(tables, ["player_games", "team_defense_games"])

    def test_baseline_columns_exist_before_any_load(self):
        # v_player_games_vs_defense names these; SQLite would only fail at
        # query time if they were missing.
        columns = db.existing_columns(self.conn, "team_defense_games")
        self.assertTrue(set(db.TEAM_DEFENSE_BASELINE_STATS) <= columns)

    def test_views_are_queryable_on_an_empty_database(self):
        db.rebuild_views(self.conn)
        for view in ("v_team_defense", "v_player_games_vs_defense"):
            self.conn.execute(f"SELECT * FROM {view} LIMIT 1").fetchall()

    def test_defense_joins_onto_a_player_game(self):
        db.upsert_team_defense_games(self.conn, self._rows())
        db.upsert_athlete(self.conn, {"athlete_id": "1", "display_name": "A Back"})
        db.upsert_player_games(
            self.conn,
            [
                {
                    "athlete_id": "1",
                    "event_id": "401671789",
                    "season": 2024,
                    "team_id": "12",
                    "opponent_id": "33",
                    "is_all_star": 0,
                    "_stats": {"rushingYards": 100.0},
                }
            ],
        )
        db.rebuild_views(self.conn)
        row = self.conn.execute(
            "SELECT def_points_allowed, def_sacks FROM v_player_games_vs_defense"
        ).fetchone()
        # The back faced BAL, so the defensive line attached is BAL's.
        self.assertEqual(row["def_points_allowed"], 27.0)
        self.assertEqual(row["def_sacks"], 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
