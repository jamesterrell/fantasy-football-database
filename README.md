# Fantasy Football Database

Game-level career data for NFL fantasy-relevant players, built from ESPN's public
APIs into a local SQLite database. The end goal is a modelling dataset covering
the top ~200 fantasy players; this is the iterative build described in `CLAUDE.md`,
currently populated with **Jonathan Taylor (86 career games, 2020-2025)**.

## Quick start

```bash
pip install -r requirements.txt

python -m ffdb add "Jonathan Taylor"      # load a full career
python -m ffdb show "Jonathan Taylor"     # season-by-season fantasy summary
python -m ffdb players                    # what's loaded
python -m ffdb export                     # v_player_games -> data/exports/
```

Other commands:

```bash
python -m ffdb add "Josh Allen" "Ja'Marr Chase"   # batch; one failure won't abort the run
python -m ffdb add 4242335 --season 2025 --force  # by id, one season, bypass the cache
python -m ffdb index --search "Justin Tucker"     # look up ESPN athlete ids
python -m unittest discover -s tests              # 21 tests, no network needed
```

`--force` re-fetches from ESPN instead of reading the local archive. Use it for the
in-progress season; everything else is immutable once played.

## How it works

```
ESPN athlete index  ->  athlete_id  ->  gamelog per season  ->  parse  ->  SQLite
   (+ site search fallback)              (raw JSON archived to data/raw/)
```

1. **Name -> id.** `sports.core.api.espn.com/v3/.../athletes` paginates ~20k athletes
   at 1000/page. Names are normalised (accents, punctuation and `Jr./III` suffixes
   stripped) so `"Ja'Marr Chase"` and `"Marvin Harrison Jr."` resolve cleanly. When
   several athletes share a name, the active one wins; if that is still ambiguous
   the CLI prints the candidates and asks for an explicit id.
2. **Game logs.** `site.web.api.espn.com/apis/common/v3/.../athletes/{id}/gamelog?season=YYYY`
   returns one JSON document per athlete-season, with a `names[]` array that is
   index-aligned to each event's `stats[]`. The seasons a player has are read from
   the payload's own `filters` block, so career span is discovered, never assumed.
3. **Archive.** Every response is written to `data/raw/` before parsing. Re-parsing
   after a bug fix costs no network calls, and the raw payloads stay auditable.

### Why the JSON API rather than `pd.read_html`

`CLAUDE.md` started from `pd.read_html` on the gamelog page, which works. The JSON
endpoint behind that same page was preferred because it carries machine-readable
stat keys (`rushingYards`) instead of ambiguous display headers (the HTML table has
`YDS` twice, under `Rushing` and `Receiving`), plus the ESPN `event_id`, kickoff
timestamp, opponent id, home/away and final score per game. Those extra keys are
what make the rows joinable to anything else later.

## Querying the data

`python examples/explore.py` runs eight worked queries against the real database —
copy any of them into your own script. The short version:

```python
import sqlite3, pandas as pd
conn = sqlite3.connect("data/fantasy_football.db")
df = pd.read_sql_query("SELECT * FROM v_player_games WHERE season = 2025", conn)
```

For clicking around rather than writing SQL, [DB Browser for SQLite](https://sqlitebrowser.org)
opens the `.db` file directly. There is also a `sqlite3` shell:

```bash
sqlite3 data/fantasy_football.db     # .tables, .schema player_games, .headers on, .quit
```

Query `v_player_games` rather than `player_games` unless you specifically want the
exhibition games back — the view adds player name/position and drops Pro Bowls.
Regular season is `season_type = 2`, postseason is `3`.

## Schema

| table | contents |
|---|---|
| `athletes` | one row per player: position, team, height/weight, birth date, experience |
| `games` | one row per NFL game seen: season, week, date, both teams, final score |
| `player_games` | **the modelling table** - one row per player per game |
| `stat_catalog` | every stat key seen, and the column it maps to |
| `sync_log` | what was loaded when, per athlete-season |

Views (rebuilt on every load): `v_player_games` adds player name/position and
excludes exhibition games; `v_player_seasons` aggregates fantasy points per season.

**Stat columns are dynamic.** ESPN publishes a different stat vocabulary per
position — a QB log has `passingYards` and `QBRating`, a RB log has
`receivingTargets` — and extends it over time. Rather than hardcode a column list
that silently drops anything new, `player_games` grows a `REAL` column the first
time a stat key appears. Each row also keeps the untouched key/value mapping in
`raw_stats` (JSON), so no parsing decision made today can lose data.

Fantasy points are computed at load time into `fp_standard`, `fp_half_ppr` and
`fp_ppr`, so the table ships with a ready target variable. Verified against known
totals — Taylor's 2021 is 373.1 PPR (1811 rush yds, 18 rush TD, 40 rec, 360 rec
yds, 2 rec TD, 2 fumbles lost).

## Data quality notes

Things that were found the hard way and are handled in code:

- **Pro Bowls are filed as postseason games.** Taylor's 2021 "postseason" entry is
  AFC vs NFC, with a week number and real-looking stats — in a season the Colts
  missed the playoffs entirely. They are flagged `is_all_star = 1` and excluded
  from both views. The raw rows stay in `player_games` if you want them.
- **`-` is not `0`.** A dash means the stat did not apply to that game; it is stored
  as `NULL` so it can't be averaged in as a zero.
- **Aggregate splits share event ids.** Each `seasonTypes[].categories[]` list mixes
  per-event rows with totals/by-opponent rollups. Only `type == "event"` is parsed.
- **`interceptions` is position-dependent.** It means picks *thrown* on a QB log and
  picks *caught* on a defender's. The -2 only applies to players who threw a pass.

Known upstream limitations, not worked around:

- **The athlete index is incomplete.** Justin Tucker (id 15683) is absent from all
  21 pages despite being an active athlete on the same API. Name resolution falls
  back to ESPN's site search, which does find him.
- **Some players have no game log at all.** Travis Kelce and Justin Tucker return an
  empty payload for every season ESPN itself lists for them. This is upstream, not a
  parsing failure: the HTML page that `read_html` reads has no gamelog table for them
  either, and the core-API athlete tree now 404s for every player. Both are currently
  unrostered, which is the likely cause. The loader logs a `WARNING` rather than
  failing quietly, so gaps like this surface during a bulk load instead of becoming
  silent holes in the training data.

## Next steps

- Assemble the top-200 player list, then `build_players()` it — batch loading,
  per-player error isolation and the dynamic schema are already in place and
  tested against QB/RB/WR logs.
- Derive modelling features (rolling averages, rest days, opponent strength) from
  `player_games`; the `game_date`/`opponent_id` columns exist for exactly this.
- Add snap counts / target share, which need a different ESPN endpoint.
