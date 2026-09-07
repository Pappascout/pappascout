# Pappascout

Hobby scouting tool for **Pappaliiga**, a Finnish amateur CS2 league organized on
the FACEIT platform.

Pappascout writes a short tactical summary of our team's **upcoming league
opponents**: it finds the opponent's recent FACEIT matches where (nearly) the
full roster played, downloads and parses the demos, classifies every round, and
renders a Markdown report the team reads before the match.

**The tool's own output is in Finnish** — it is written for one Finnish team, and
every message is meant to be read by someone who does not want to read code.
Everything else in this repository is English.

## Status

Working end to end from demos that are already on disk. The automatic download
path is complete, reviewed and tested, but **it has never run**: FACEIT's
Downloads API access is still in the approval queue, so demos are currently
imported by hand from the browser. Epic 3 was accepted with that as a known open
item.

Non-commercial free-time project for a single team. No demo files are
redistributed, and volume is roughly 10–30 demos per month during the season.

## Install

```powershell
uv sync
```

API keys live in a machine-local file, **not in this repo and not in OneDrive**
(OneDrive makes conflict copies and keeps rotated keys in version history).
Create `%USERPROFILE%\.pappascout\.env`:

```
FACEIT_API_KEY=<Data API server-side key>
FACEIT_DOWNLOADS_TOKEN=<Downloads API token>
```

`FACEIT_DOWNLOADS_TOKEN` is a separate credential with a Downloads API scope —
it is not the Data API key, and it only appears in the portal after the
application is approved. Everything except `fetch` and `collect` works without
it.

## Usage

The pipeline has two halves: the first gets the material, the second turns it
into a report. Every command is safe to run again.

```powershell
# --- what is configured and what is on disk ---
uv run pappascout info                 # settings, archive state, key presence
uv run pappascout info --koko          # same, plus total archive size

# --- getting the material (needs the network) ---
uv run pappascout discover                            # division match + team index
uv run pappascout select --team "Rcave"               # pick maps by roster threshold
uv run pappascout fetch --team "Rcave"                # download that team's sample
uv run pappascout collect                             # download the whole division
uv run pappascout import --match <match_id> --map 1   # file a browser-downloaded demo

# --- turning it into a report (no network) ---
uv run pappascout parse <file|map_demo_id>            # demo to rounds, positions, events
uv run pappascout classify <map_demo_id> --team <key> --show
uv run pappascout classify <map_demo_id> --kaikki-joukkueet
uv run pappascout aggregate --team <key>              # writes report.json
uv run pappascout report --team <key>                 # writes Markdown
```

Notes that matter in practice:

- **`discover` first.** `select`, `fetch` and `collect` all read the match index
  it writes; they never refresh it themselves.
- **`--kylla` skips confirmation prompts** in `fetch`, `collect` and `import` —
  except the map-name check in `import`, which is the one question the flag
  cannot silence. A demo filed under the wrong map would corrupt a report
  silently.
- **`fetch` and `collect` are the same download with a different unit set.**
  `fetch` reads one team's selection file; `collect` takes every played match in
  the division. Use `collect` to save demos before FACEIT deletes them after
  about 30 days.
- **`import` does not download demos** — the demo is the file you give it. It
  does reach FACEIT for the match's veto data, to check the map name.

## How it works

Seven stages. Each is a function whose only inputs are archive files, its own
typed settings section and the ports it is given, and whose only outputs are
archive files. A stage never calls another stage; the user decides the order,
one command at a time.

| Stage | Reads | Writes |
| --- | --- | --- |
| `discover` | FACEIT championship | `index/matches.json`, `index/teams.json` |
| `select` | both indexes | `index/selections/<team_key>.json` |
| `fetch` / `collect` | selection file / match index | `demos/<map_demo_id>.dem.zst` + `.meta.json` |
| `parse` | one demo | `parsed/<map_demo_id>/*.parquet` |
| `classify` | parsed rounds | `classified/<team_key>/<map_demo_id>.parquet` |
| `aggregate` | classified rounds | `aggregates/<team_key>/report.json` |
| `report` | `report.json` | `reports/<team_key>/<timestamp>.md` |

Three ideas carry most of the design:

**Every claim in the report carries its sample.** A line says `3/7` before it
says anything else, so a pattern seen twice never reads like a habit.

**Identity is the roster, not the identifier.** A FACEIT `faction_id` changes
between seasons and a lineup hash changes the moment a substitute plays, so
teams are matched by shared players instead. Maps enter a sample through a
roster threshold — the full roster, or four regulars plus one outsider — and the
report keeps those two classes apart.

**A failing unit does not stop the run.** Each match or demo carries its own
status and reason; an exception is raised only when nothing could be processed
at all, or when settings or keys are missing. Advice belongs to the failure
itself, not to a shared heading, because two failures rarely have the same fix.

## Archive layout

The archive lives in OneDrive and is shared by both machines. The code repo is
deliberately outside OneDrive — git and OneDrive do not work together.

```
raw/faceit/                                   HTTP cache, safe to delete
index/matches.json                            written only by discover
index/teams.json                              written only by discover
index/selections/<team_key>.json              written only by select
demos/<map_demo_id>.dem.zst  + .meta.json     written only by fetch / import
import/                                       inbox; only import reads it
parsed/<map_demo_id>/*.parquet                + manifest
classified/<team_key>/<map_demo_id>.parquet   + manifest
aggregates/<team_key>/report.json             + manifest
reports/<team_key>/<timestamp>.md             + manifest
logs/<host>/
```

**One writer per file.** Writes go through a host-tagged temporary file and then
a rename, because the archive is in OneDrive. Every result gets a manifest, and
a matching manifest means the stage is skipped — so changing a threshold re-runs
`classify` in seconds without touching `parse`.

The `.dem.zst` files are 140–240 MB each and are the only large thing in the
project; everything derived from them is under a megabyte. They can be deleted
after parsing without losing data.

## Configuration

`settings.toml` holds every number, partitioned so that each stage reads only
its own section: `[project] [league] [parse] [thresholds] [aggregate] [report]
[economy] [faceit]`. Each value's comment names its source — research, a
measurement, or a calibration still waiting for data.

Two environment variables override settings for a single run, which is also how
the tests stay off the real archive:

- `PAPPASCOUT_ARCHIVE_ROOT` — point the whole archive somewhere else
- `PAPPASCOUT_DEMOS_ROOT` — point only the demo directory somewhere else

## The FACEIT cache is split by call type

Two endpoints, two opposite rules, and the split is the whole decision.

The **match list** (`/championships/{id}/matches`) is **never cached**. It is one
call per run, and it changes every week — during the season most matches are
still `SCHEDULED` — so a cached list would save one call and cost correctness.

A **single match** (`/matches/{id}`) is cached **permanently, but only once its
status is `FINISHED`**. A finished match never changes again, and one `collect`
run over a full division can make dozens of these calls. `CANCELLED` is
deliberately left out: a cancellation can itself be reversed, while a demo lost
to FACEIT's 30-day deletion is gone for good, and the two mistakes do not cost
the same.

The read path is **self-correcting**. The same condition applies to **reading**,
not only to writing, so a file left behind by an older version that holds a
`SCHEDULED` match is ignored instead of being served forever. `raw/faceit/` is
therefore safe to delete at any time; it is an HTTP cache and nothing else
depends on it.

## Layout

| Path | Contents |
| --- | --- |
| `src/pappascout/domain/` | Pure logic, no I/O: Polars schemas, typed settings, round numbering, round-type economy, position sampling, roster threshold, team identity, utility geometry, and the report model with its aggregation |
| `src/pappascout/stages/` | The seven stages above. Each writes only its own output area |
| `src/pappascout/adapters/` | Everything that talks to the outside: the FACEIT client (the only place that makes HTTP calls), the demoparser2 implementation (the only place that knows the game's property names), zstd decompression, and the port protocols stages take as parameters |
| `src/pappascout/archive/` | Directory layout as relative paths, atomic write, manifests |
| `src/pappascout/render/` | The report's view model (**what** is said) and a Jinja2 template (**how** it is said), so wording changes without touching code |
| `src/pappascout/cli/` | Typer commands. Thin: reads settings, picks stages, prints the result |

The dependency arrow is `cli -> stages -> {domain, adapters, archive, render}`,
with `render -> domain` and `adapters -> domain`. `tests/test_layering.py`
enforces it, which is why "render computes nothing" is a structural promise
rather than a habit.

## Development

```powershell
uv run pytest                                  # everything
uv run pytest -m "not demo"                    # skip tests needing a real demo file
uv run pytest -m "not demo and not archive"    # skip tests needing the real archive
```

Two markers gate the tests that need real data: `demo` needs a demo file on
disk, `archive` needs a parsed archive but no demo. Both skip with a message
naming the path they looked for. Everything else runs from tables built by hand.

Tests never reach the network, and that is structural rather than policed: every
stage takes its ports as parameters, and an autouse fixture clears the API keys
and redirects `HOME` and `USERPROFILE`, so a real call would fail on a missing
key.

Three conventions worth knowing before editing:

- **A test that cannot fail is worse than no test.** Several guards here exist
  because a defect was proved by breaking the code and watching the suite stay
  green. After a fix, break it again and check that a test fails — and that it
  fails for the right reason.
- **Do not state a number you did not measure.** Docstrings here carry measured
  values and dates, and a wrong one looks like evidence.
- **A claim in a comment is part of the code.** Several tests read the source and
  assert that a docstring does not promise something that no longer exists.

## Where the detail lives

The design rationale is in the docstrings rather than here: each module explains
why it is shaped the way it is, next to the code that has to honour it, and
`settings.toml` explains every threshold at the line where it is set.

Planning artifacts — PRD, architecture, epics, per-story specs, calibration
measurements and retrospectives — live outside this repo, alongside the archive.
