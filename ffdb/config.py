"""Project-wide paths and constants."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
EXPORT_DIR = DATA_DIR / "exports"
DB_PATH = DATA_DIR / "fantasy_football.db"

# ESPN endpoints.
ATHLETE_INDEX_URL = "https://sports.core.api.espn.com/v3/sports/football/nfl/athletes"
ATHLETE_BIO_URL = "https://sports.core.api.espn.com/v3/sports/football/nfl/athletes/{athlete_id}"
ATHLETE_PROFILE_URL = (
    "https://site.web.api.espn.com/apis/common/v3/sports/football/nfl/athletes/{athlete_id}"
)
GAMELOG_URL = (
    "https://site.web.api.espn.com/apis/common/v3/sports/football/nfl/athletes/{athlete_id}/gamelog"
)
# Fallback name lookup: the athlete index has gaps (it omits some active
# players), but site search still finds them.
SEARCH_URL = "https://site.web.api.espn.com/apis/search/v2"

# Rosters, used to assemble the candidate pool for ranking.
TEAMS_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams"
ROSTER_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/{team_id}/roster"

# Box scores, used to enumerate who actually played in a past season. The
# summary endpoint lists every athlete who recorded a stat in one event.
SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary"

# Attendance. The game log only carries games the athlete has a stat line for,
# so a game they played and recorded nothing in is indistinguishable there from
# a game they sat out. The event log carries both, each flagged `played`, which
# is what separates "scoreless" from "inactive" from "not on the roster".
ATHLETE_EVENTLOG_URL = (
    "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/seasons/{season}"
    "/athletes/{athlete_id}/eventlog"
)
# Page size for the event log. A season is at most ~21 events, so this is one
# page in practice; the loader still follows `pageCount` rather than assume it.
EVENTLOG_PAGE_SIZE = 100

# Season totals, including games played. This is a different pipeline from the
# game log and disagrees with it where the game log is short, which makes it the
# authority on how many games a player actually appeared in. One request returns
# every season of a career. Regular season only - a 2021 Bengal reads 16 here
# against 16 regular-season plus 4 postseason game-log rows.
ATHLETE_STATS_URL = (
    "https://site.web.api.espn.com/apis/common/v3/sports/football/nfl/athletes/{athlete_id}/stats"
)

# Team defense. The schedule supplies the season's events; the per-competitor
# statistics endpoint supplies one team's full stat line for one of them.
TEAM_SCHEDULE_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/{team_id}/schedule"
)
COMPETITOR_STATS_URL = (
    "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/events/{event_id}"
    "/competitions/{event_id}/competitors/{team_id}/statistics"
)

# The index endpoint caps page size at 1000 regardless of the limit you ask for.
INDEX_PAGE_SIZE = 1000

# User-Agent, or None to let requests send its own default.
#
# ESPN's edge blocks this one on an allowlist rather than a blocklist: a UA it
# recognises as a programmatic client passes, and anything else gets a 403
# before the request reaches the API. Measured against the summary endpoint:
#
#     python-requests/2.33.1                        -> 200
#     curl/8.0.1                                    -> 200
#     Mozilla/5.0 (compatible; ffdb/0.1; ...)       -> 403
#     Mozilla/5.0 (Windows NT 10.0; ... Chrome/124) -> 403
#     ffdb/0.1 (personal research project)          -> 403
#
# So a browser-shaped string is worse than useless, and identifying the project
# by name is not on the menu either - `ffdb/0.1` is blocked on its own. None it
# is. This bit silently, because the box scores fetched before the rule changed
# were already cached: the 2020-2024 backfill kept working from disk while a
# fresh 2025 pull failed on every event.
USER_AGENT = None

# Politeness: seconds to sleep between live HTTP calls.
REQUEST_DELAY = 0.5
MAX_RETRIES = 4

# nflverse: preseason roster and depth-chart history, which ESPN does not keep.
# Published as parquet on GitHub releases and fetched directly rather than
# through a client library - see ffdb/preseason.py for why this source exists
# at all. GitHub rejects a request with no User-Agent, so this cannot reuse the
# `USER_AGENT = None` that ESPN's box scores require.
NFLVERSE_RELEASE_URL = "https://github.com/nflverse/nflverse-data/releases/download"
NFLVERSE_USER_AGENT = "fantasy-football-db"
NFLVERSE_DIR = DATA_DIR / "nflverse"
# Earliest season with both weekly rosters and weekly depth charts published.
NFLVERSE_FIRST_SEASON = 2016

# ESPN season type ids, matching their own numbering.
SEASON_TYPE_PRE = 1
SEASON_TYPE_REGULAR = 2
SEASON_TYPE_POST = 3


def ensure_dirs() -> None:
    for d in (DATA_DIR, RAW_DIR, EXPORT_DIR, NFLVERSE_DIR):
        d.mkdir(parents=True, exist_ok=True)
