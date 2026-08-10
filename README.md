# Fantasy Football Database

Game-level career data for NFL fantasy-relevant players, built from ESPN's public
APIs into a local SQLite database, for use as a modelling dataset.

Currently holds **64,180 player-games across 1,657 players and 3,215 distinct NFL
games, spanning 2005-2025** — every fantasy-position player who appeared in a game
from 2016 on, plus the earlier careers of the top 200 fantasy scorers of 2025.

**2016 onward is a complete panel**: 508-608 players and 5,700-6,800 player-games
per season, enumerated from box scores rather than rosters, so players who have
since retired, been cut or been hurt are all in it. Before 2016 it thins out fast
(7 players in 2015, 3 in 2010), because those rows exist only as the back end of a
2025 star's career and are therefore conditioned on being good enough to still be
playing in 2025. That survivorship is fine for "how does this player score" and
misleading for "how do players in general age", so start a training window at 2016
unless you have a reason not to.

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
python -m ffdb build-season --season 2016-2025    # everyone who appeared, from box scores
python -m ffdb attendance --season 2016-2025      # games the game log leaves out
python -m ffdb build-top --season 2025 --top 200  # derive the top 200 and load their careers
python -m ffdb rankings --season 2025 --top 50    # show a stored ranking
python -m ffdb defense --season 2024              # team defense game logs, all 32 teams
python -m ffdb defense --season 2024 --team KC    # just one team's schedule
python -m ffdb schedule --season 2026             # a season's matchups, upcoming games included
python -m ffdb add "Josh Allen" "Ja'Marr Chase"   # batch; one failure won't abort the run
python -m ffdb add 4242335 --season 2025 --force  # by id, one season, bypass the cache
python -m ffdb index --search "Justin Tucker"     # look up ESPN athlete ids
python -m unittest discover -s tests              # 105 tests, no network needed
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
3. **Attendance.** The game log only holds games ESPN has a stat line for, so
   `sports.core.api.espn.com/v2/.../athletes/{id}/eventlog` is read afterwards to
   find the games a player was there for and the log skipped — see
   [Games the game log leaves out](#games-the-game-log-leaves-out).
4. **Archive.** Every response is written to `data/raw/` before parsing. Re-parsing
   after a bug fix costs no network calls, and the raw payloads stay auditable.

### Why the JSON API rather than `pd.read_html`

`CLAUDE.md` started from `pd.read_html` on the gamelog page, which works. The JSON
endpoint behind that same page was preferred because it carries machine-readable
stat keys (`rushingYards`) instead of ambiguous display headers (the HTML table has
`YDS` twice, under `Rushing` and `Receiving`), plus the ESPN `event_id`, kickoff
timestamp, opponent id, home/away and final score per game. Those extra keys are
what make the rows joinable to anything else later.

## Building the top-200 dataset

`build-top` derives the player list from realised production rather than anyone's
projections, in three resumable phases:

| phase | what it does | cost |
|---|---|---|
| pool | fetch all 32 rosters, keep QB/RB/WR/TE (~900 players) | ~33 requests |
| rank | load one season for each candidate, rank by fantasy points | ~1 per player |
| backfill | fetch every prior season for the top N | ~1 per player-season |

Every response is archived, so an interrupted run resumes from disk rather than
refetching, and the ranking season is already cached by the time backfill needs it.

**What "top 200" means here.** The list is the 200 highest scorers of a completed
regular season — a fact about what happened, not a draft board. Consequences worth
knowing before modelling on it:

- **Backward-looking by construction.** A 2026 rookie has no 2025 NFL production and
  cannot appear, however high they would go in a real draft. Breakouts only enter the
  list the year *after* they break out.
- **Raw points favour quarterbacks.** QBs out-score skill players per game, so they
  occupy more of the top 200 than a positional draft board would have them. Each row
  stores `position_rank` (QB1, RB1, …) so you can rank within position or apply your
  own replacement-level adjustment.
- **Kickers and defenses are excluded.** Their scoring rules aren't implemented, and
  including them would rank them at zero.
- **Currently rostered players only.** The pool comes from today's rosters, so a
  productive player who is now unsigned is missed. Use `build-season` (below) for a
  complete historical season — this limitation is exactly what it exists to fix.
- **Regular season only.** Postseason games aren't available to every player and would
  reward being on a good team.

## Building a complete season

`build-top` answers "who were the best players?", which is the wrong question for
training data. Backfilling earlier seasons from today's top 200 reproduces the
present in the past: a player who produced in 2021 and left the league by 2025
never appears, so the older a season is, the more it looks like a list of players
who happened to survive.

`build-season` enumerates from **box scores** instead. Every player who recorded a
stat in a game is in that game's box score permanently, so the union over a season's
events is exactly who played it — retired, cut and injured included. Since fantasy
points can only come from passing, rushing or receiving, and each puts a player in
the matching box score category, scanning those three cannot miss anyone who scored.

```bash
python -m ffdb schedule --season 2022      # once, so `games` has the events
python -m ffdb build-season --season 2016-2025
```

| phase | what it does | cost |
|---|---|---|
| discover | scan every event's box score for the season | 1 request per game (~272) |
| resolve | look up each new player's position, keep QB/RB/WR/TE | ~2 per *new* player |
| pull | load the game log for each athlete-season | ~1 per athlete-season |

`--season` takes a year or a range, repeatably. Interrupting is safe: `sync_log`
records each athlete-season as it lands and a rerun skips it, so the job resumes
where it stopped. Pass `--resync` to re-pull anyway.

The season filter is deliberately *not* applied per game. Roughly a quarter of all
player-games score exactly zero, and dropping those individually would filter on the
target — the model would never see what a bust looks like. Every game of every
player who appeared is kept; filter on season totals at query time if you want to:

```sql
WITH scorers AS (
  SELECT athlete_id, season FROM player_games
  WHERE season_type = 2 GROUP BY athlete_id, season HAVING SUM(fp_ppr) > 0
)
SELECT g.* FROM player_games g
JOIN scorers s ON s.athlete_id = g.athlete_id AND s.season = g.season
```

## Games the game log leaves out

A game log is not a list of the games a player was available for. It is a list of the
games ESPN has a stat line for. A player who dressed, took snaps and touched nothing
produces no row, so the season just skips that week — and in `player_games` that is
indistinguishable from being injured. Both are an absent row.

It matters because the absent rows are zeros, and dropping zeros lifts every per-game
average. So when a player is missing a game their team played, is it safe to write a
zero in? Almost always **no**, and `attendance` is the command that works out which
ones it is safe for:

```bash
python -m ffdb attendance --season 2016-2025 --dry-run   # classify the gaps, write nothing
python -m ffdb attendance --season 2016-2025             # fill the ones that are safe
```

It reads ESPN's core-API **event log**, which lists every event an athlete was on a
roster for — played or not — with a flag saying which:

```
sports.core.api.espn.com/v2/.../seasons/{season}/athletes/{id}/eventlog
    events.items[] -> {event {$ref}, teamId, played, statistics {$ref}}
```

That splits a gap three ways, and only the first is fillable:

| in the event log | `played` | what it means | what happens |
|---|---|---|---|
| yes | `true` | played, recorded nothing | row is recovered |
| yes | `false` | on the roster, did not play | left absent |
| no | — | not on the roster that week | left absent |

The third case is the one that makes a naive fill dangerous. Diffing a player's rows
against their team's schedule finds **33,310 gaps across 2016-2025** — against 60,503
real rows — and the overwhelming majority are weeks the player was hurt, inactive, on
IR, or not yet signed. Zero-filling all of them would have grown the table by 55% with
invented rows, cut the mean of every per-game feature by about a third, and taught a
model that a third of the league dresses and does nothing every Sunday.

Running it over all ten seasons — 5,461 athlete-seasons, one request each — the split
came out like this:

| season | played, unlogged | rostered but inactive |
|---|---|---|
| 2016 | 9 | 1,733 |
| 2017 | 10 | 1,691 |
| 2018 | 11 | 1,848 |
| 2019 | 13 | 1,801 |
| 2020 | 24 | 2,085 |
| **2021** | **146** | 2,860 |
| 2022 | 0 | 2,058 |
| 2023 | 0 | 1,813 |
| 2024 | 0 | 1,899 |
| 2025 | 4 | 2,126 |
| total | **217** | 19,914 |

So of 33,310 schedule gaps, **217 were safe to fill and 33,093 were not** — one in 154.
2022 through 2024 have none at all: ESPN's two endpoints agree with each other
perfectly there, which is the best evidence that the flag means what it says.

**Nothing here is assumed to be zero, either.** Each fillable game's `statistics` ref
is fetched and the real line is stored. All 217 came back at exactly 0.00 PPR — which
is precisely why ESPN's game log dropped them — but had any not, the row would have
been written with the numbers that actually happened and reported separately, rather
than flattened into a zero that never occurred.

**What the 217 turn out to be: return specialists, all of them.** 215 of 217 have a
kick- or punt-return line and nothing else; the other two have a stray non-return stat.
None are blank. That makes sense once you see the mechanism — a game log carries only
the offensive vocabulary for that position, so a receiver whose whole afternoon was
"2 kick returns for 41 yards" produces no row at all, while the event log can still
prove they were there because a `returning` stat line exists to point at.

That is also the ceiling on the technique: it recovers games that left a trace in
ESPN's data. A player who touched the ball zero times in every phase leaves none, gets
`played: false` and no `statistics` ref, and stays missing.

Recovered rows are marked `source = 'eventlog'`, so they can be excluded, counted, or
compared against the rest at query time. Only athlete-seasons that already have at
least one game-log row are considered: an athlete-season with none is not a quiet year,
it is one of the empty game logs described under
[known upstream limitations](#data-quality-notes), and filling it would manufacture a
career out of an outage.

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
| `teams` | the 32 NFL teams |
| `games` | one row per NFL game seen: season, week, date, both teams, final score (NULL until played) |
| `player_games` | **the modelling table** - one row per player per game |
| `team_defense_games` | one row per team per game: what that defense did and gave up |
| `rankings` | derived top-N by season and scoring format, with position rank |
| `stat_catalog` | every stat key seen, which table it lives on, and its column |
| `sync_log` | what was loaded when, per athlete-season |

Views (rebuilt on every load): `v_player_games` adds player name/position and
excludes exhibition games; `v_player_seasons` aggregates fantasy points per season;
`v_rankings` is the ranking with names and teams joined on; `v_team_defense` adds
team names; `v_player_games_vs_defense` attaches the opposing defense to every
player-game; `v_team_schedule` is every regular-season game from both teams' points
of view, one row per team per week; `v_team_defense_seasons` is one row per team per
season of per-game defensive averages.

Every `player_games` row carries a `source`: `gamelog` for the normal path, and
`eventlog` for a game ESPN's game log skipped that was recovered from the event log
(see [Games the game log leaves out](#games-the-game-log-leaves-out)). Add
`WHERE source = 'gamelog'` if you want only ESPN's own rows.

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

## Team defense

`python -m ffdb defense --season 2024` loads a full season of team defensive game
logs — 32 teams × 17 games = 544 rows, two per game. Add `--postseason` for playoff
games, or `--team KC` to follow one schedule.

Two endpoints feed it: the team schedule supplies the season's events and final
scores, and a per-competitor statistics endpoint supplies each team's full stat
line for one event. Both competitors are read together, because **a defense's
"allowed" numbers are just the opposing offense's own numbers** — `yards_allowed`
on the Chiefs' row is the Ravens' `totalYards` from the same game.

Columns come in three groups:

| group | examples | source |
|---|---|---|
| matchup | `season`, `week`, `opponent_id`, `home_away`, `result` | team schedule |
| allowed | `points_allowed`, `yards_allowed`, `pass_yards_allowed`, `third_down_conv_allowed`, `turnovers_forced` | the opposing offense's line |
| the defense's own | `sacks`, `interceptions`, `totalTackles`, `QBHits`, `passesDefended` | ESPN's `defensive` categories, dynamic columns |

`points_allowed` is derived from the final score rather than read from ESPN's own
`defensive.pointsAllowed`, which is zero-filled before roughly 2015 and disagrees
with the opponent's total in some later games. ESPN's version is still stored, as
the `pointsAllowed` / `yardsAllowed` columns, if you want to compare.

Joining a defense onto a player-game is `event_id` plus the player's `opponent_id`:

```sql
SELECT pg.display_name, pg.week, pg.rushingYards, td.points_allowed, td.sacks
FROM v_player_games pg
JOIN team_defense_games td
  ON td.event_id = pg.event_id AND td.team_id = pg.opponent_id
WHERE pg.season = 2024;
```

`v_player_games_vs_defense` has that join built in.

> **Careful with this as a feature.** These are the defense's numbers *in that same
> game*, so `points_allowed` includes the points the player's own team just scored.
> Using it directly as a model input leaks the outcome. For opponent strength, build
> a season-to-date or trailing-N average that excludes the current game.

### Season averages

`v_team_defense_seasons` collapses that table to one row per team per season — 320
rows for 2016-2025, 32 teams each:

```sql
SELECT team_abbr, games, points_allowed_pg, yards_allowed_pg, sacks_pg,
       turnovers_forced_pg, third_down_pct_allowed
FROM v_team_defense_seasons
WHERE season = 2025 ORDER BY points_allowed_pg;
```

Every stat column is a **per-game average** (`_pg` suffix), not a season total, so
2020's 16-game season compares directly with the 17-game seasons after it; multiply
by `games` for a total. `third_down_pct_allowed` is summed then divided rather than
averaged per game, because the mean of per-game percentages weights a 1-for-2 game
the same as a 6-for-12.

Regular season only, and 2026 cannot appear — a row exists in `team_defense_games`
only once ESPN publishes a box score, so unplayed games are absent by construction.
Check `games` before trusting a season still in progress.

**Two stats are deliberately left out.** ESPN zero-fills them rather than omitting
them, and a `0` averages in as a real low where a `NULL` would be skipped:

| stat | how bad | left out because |
|---|---|---|
| `redzone_tds_allowed` | `0.0` in all 5,278 rows, every season | a red-zone TD rate built on it reads as a real 0% for all 32 teams |
| `plays_allowed` | `0` in 446 of 2020's 512 games, and in ~93% of every season before that | averaging the zeros puts 2016-2020 near 5 plays per game; skipping them leaves ~1 game per team backing the number |

Both remain on `team_defense_games` if you want to handle them yourself.
`redzone_att_allowed_pg` is unaffected and stays — red-zone trips faced are real, it
is only what happened inside the 20 that is missing.

**Turnovers are a 2021-onward column.** ESPN began publishing
`miscellaneous.totalGiveaways` / `totalTakeaways` in 2021; before that the keys are
simply absent, and only 34-66 games a season carry one:

| season | games | with a turnover figure |
|---|---|---|
| 2016-2019 | 512 each | 34-40 |
| 2020 | 512 | 66 |
| 2021-2025 | 542-544 each | all of them |

They land as `NULL` rather than `0`, which is honest but sets a subtler trap: `AVG()`
skips nulls, so a season average would silently be the mean of about two games per
team. `turnovers_forced_pg` and `takeaways_pg` are therefore guarded on every game in
the season having a value — a real number from 2021 on, `NULL` before it. The
per-game columns are untouched on `team_defense_games`, and `interceptions` and
`fumbles_recovered` are populated throughout if you want to build your own.

> The same leakage warning applies here in a subtler form: a full-season average
> includes the games you are predicting. For a backward-looking feature, build a
> season-to-date average from `team_defense_games` instead.

## Schedule and upcoming games

Predicting week 3 needs the week 3 matchup, and ESPN publishes a full season's
schedule months before it starts. `schedule` loads those events into `games` with
every outcome field left `NULL`:

```bash
python -m ffdb schedule --season 2026   # 272 games, 18 weeks, none played yet
```

This is the one place unplayed events are stored, and it is deliberate: the matchup
(week, kickoff time, both teams, home/away) is known, only the result is missing.
`score IS NULL` is what separates a prediction row from a played one:

```sql
SELECT g.week, a.abbreviation AS away, h.abbreviation AS home, g.game_date
FROM games g
JOIN teams h ON h.team_id = g.home_team_id
JOIN teams a ON a.team_id = g.away_team_id
WHERE g.season = 2026 AND g.score IS NULL
ORDER BY g.week, g.game_date;
```

Re-running upserts on `event_id`, so a game moved by flex scheduling updates in
place and scores fill in as the season is played — no duplicate rows, no separate
"predictions" table to reconcile. Add `--force` to bypass the archive, which you
want whenever the schedule may have shifted.

`v_team_schedule` turns `games` into one row per team per week — every
regular-season game twice, once from each side — which is the shape a per-player
prediction row needs:

```sql
SELECT week, opponent, score FROM v_team_schedule
WHERE season = 2026 AND team = '11' ORDER BY week;   -- the Colts' 2026 season
```

It filters on `season_type = 2` rather than the `season_type_name` text, because
ESPN labels the same thing two ways: schedule loads say `Regular Season` and older
game-log loads say `2019 Regular Season`. Matching the label would quietly drop
every pre-2020 season.

Postseason events don't exist upstream until the bracket is set, so `--postseason`
returns nothing for a future season.

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
- **`sacks` means opposite things in two tables.** Taken on a QB game log, recorded
  on a team defense row. `stat_catalog` is keyed by `(table_name, stat_name)` so both
  meanings coexist.
- **Some ESPN stats are zero-filled rather than absent.** `redzoneTouchdowns` is `0.0`
  in every team-game, and 2020's `totalPlays` is `0` in 446 of 512. A `0` averages in
  as a real low where a `NULL` would be skipped, so `v_team_defense_seasons` leaves
  both out. They stay on `team_defense_games`. Worth checking any new column for this
  before trusting it — `SUM(col) = 0` over a whole season is the tell.
- **2021 weeks 1-14 hold fewer scoreless games than any comparable window.** They store
  20.3 player rows per game at a 13.4% zero rate, against 22.3-24.8 and 24-28% in every
  other season — including 2021 weeks 15-18, which look entirely normal. Whether ESPN is
  short of rows there or the season genuinely ran that way is unresolved; either way, a
  per-game average over that window reads about 20% high, so filter it out
  (`week > 14 OR season <> 2021`) if you are computing rates rather than totals.
- **An abandoned game has a schedule row and no box score.** 2022 week 17 BUF at CIN
  (event `401437947`) was called off after Damar Hamlin's collapse and never resumed;
  it is stored `0-0` with a summary containing no players. 2017 week 1 MIA at TB
  (`400951581`) is the same shape for a different reason — postponed for Hurricane
  Irma and replayed in week 11 as `400981391`, leaving the week 1 entry behind as a
  `0-0` husk, which is why 2017's schedule has 257 rows rather than 256. `build-season`
  and `defense` both log these and move on. No player is lost: every team played its
  full slate.
- **Unplayed games carry no box score.** Schedule entries without a final score are
  dropped from `player_games` and `team_defense_games` rather than stored as empty
  rows, so an in-progress season loads cleanly. `games` is the exception — see
  [Schedule and upcoming games](#schedule-and-upcoming-games).

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

  It is the largest remaining hole, and it is measurable: cross-checking every cached
  box score against `player_games` finds **1,042 fantasy-position player-games that
  are in a box score but have no row**, 2016-2025. About a dozen players account for
  most of it — Kelce (159), Davante Adams (149), Keenan Allen (133), Andy Dalton (101),
  A.J. Green (80) — all of them unrostered as of the 2025 season. These are real
  production, not quiet games, so `attendance` deliberately does not touch them; see
  [Next steps](#next-steps).

## Next steps

- Recover the 1,042 player-games belonging to athletes whose game log ESPN serves
  empty. The machinery already exists: `attendance` fetches a single athlete's stat
  line for a single event from the core API, which is exactly what these need — the
  only new part is enumerating the candidates from box scores rather than from the
  event log, and the athletes' stat lines are real production rather than zeros, so
  they cannot ride along on the same pass.
- Extend `build-season` back past 2016. The box-score enumeration works as far back
  as ESPN's coverage does; the constraint is the thinning stat vocabulary in older
  seasons rather than the loader.
- Derive modelling features (rolling averages, rest days, opponent strength) from
  `player_games`; the `game_date`/`opponent_id` columns exist for exactly this.
  Opponent strength now has `team_defense_games` behind it — build it as a trailing
  average so it doesn't leak the current game.
- Add snap counts / target share, which need a different ESPN endpoint.
- Score team defenses as a fantasy position (DST), which needs a points-allowed tier
  table on top of the sacks/turnovers/TD columns already loaded.
