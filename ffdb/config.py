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

# The index endpoint caps page size at 1000 regardless of the limit you ask for.
INDEX_PAGE_SIZE = 1000

USER_AGENT = "Mozilla/5.0 (compatible; ffdb/0.1; personal research project)"

# Politeness: seconds to sleep between live HTTP calls.
REQUEST_DELAY = 0.5
MAX_RETRIES = 4

# ESPN season type ids, matching their own numbering.
SEASON_TYPE_PRE = 1
SEASON_TYPE_REGULAR = 2
SEASON_TYPE_POST = 3


def ensure_dirs() -> None:
    for d in (DATA_DIR, RAW_DIR, EXPORT_DIR):
        d.mkdir(parents=True, exist_ok=True)
