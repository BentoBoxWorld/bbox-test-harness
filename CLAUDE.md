# CLAUDE.md

This file provides guidance to Claude when working with code in this repository.

## Overview

BentoBox Integration Test Harness: automated smoke testing for BentoBox (Minecraft plugin) and 31 addons. Uses Docker to spin up a Paper Minecraft server and validates that BentoBox core and all addons load correctly. Commands are issued over the server **console** (main thread) via the itzg image's `mc-send-to-console`, not RCON — RCON dispatches commands off the main thread, which Paper 26.2+ rejects with "Cannot perform command async!".

## Commands

### Setup
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r scripts/requirements.txt
```

### Fetch JARs
```bash
# With local BentoBox snapshot (common during development)
export GITHUB_TOKEN=ghp_yourtoken
python scripts/fetch_jars.py --bentobox-jar ~/path/to/BentoBox-x.y.z-SNAPSHOT.jar

# Latest release only
python scripts/fetch_jars.py
```

### Run Tests
```bash
docker compose up -d
python scripts/run_tests.py --log-file /tmp/bbox.log --junit-output /tmp/results.xml --timeout 600
docker compose down -v
```

Add `--debug-log` to `run_tests.py` for verbose output.

## Architecture

**Three layers:**

1. **JAR Procurement** (`scripts/fetch_jars.py`) — Queries GitHub Releases API for latest addon JARs. BentoBox JAR goes to `plugins/`, addon JARs go to `plugins/BentoBox/addons/` (critical: Paper must not see addon JARs directly; they are loaded by BentoBox's own addon loader).

2. **Server Orchestration** (`docker-compose.yml`) — Paper in Docker, 4GB RAM, peaceful/offline/seed 12345. `CREATE_CONSOLE_IN_PIPE=true` enables the console input pipe used for command execution. RCON is left enabled for ad-hoc debugging but the harness no longer uses it.

3. **Test Execution** (`scripts/run_tests.py`) — Two-phase readiness check (container producing logs + Paper "Done (Xs)!" line in docker logs), then runs 5 test suites, outputs JUnit XML. Commands run via `console_command()`: send through `mc-send-to-console` (as `--user 1000`), then capture the command's output from the `docker logs` delta, bounded by a unique `say` marker.

## Test Suites (run_tests.py)

| Suite | What it checks |
|-------|---------------|
| `test_core_load` | `bbox v` console response contains version, database type, and addon list |
| `test_addon_enabled` | Each of 31 addons reports `(ENABLED)` in `bbox v` console output |
| `test_worlds_registered` | 9 game worlds appear in `bbox v` console output (acidisland, bskyblock, caveblock, oneblock, parkour, poseidon, skygrid, stranger, boxed) |
| `test_commands_registered` | 10 key commands respond via the console: `bbox`, `bsbadmin`, `acid`, `obadmin`, `cbadmin`, `boxadmin`, `sgadmin`, `padmin`, `stranger`, `parkour` |
| `test_no_console_errors` | No ERROR/SEVERE in BentoBox/addon log context; filters bStats noise |

**Notes on test design:**
- `test_addon_enabled` and `test_worlds_registered` use the `bbox v` console command (not log scraping) — this is the authoritative post-load status.
- `test_commands_registered` accepts "only available in-game" as a pass — it means the command IS registered, it just requires a player sender that the console cannot provide.
- `test_no_console_errors` uses `docker logs` output.
- **API-version skips:** an addon may declare `min_api` in `addons.yml` (its lowest supported Minecraft API). When the server runs an older MC version, that addon legitimately won't load — so its addon/world/command checks are recorded as **SKIPPED** (a warning, JUnit `<skipped>`), not failures, and its log noise is excluded from `test_no_console_errors`. The current MC version comes from `--mc-version` (defaults to the `MC_VERSION` env var). CaveBlock declares `min_api: 1.21.11`, so it is skipped on 1.21.5/1.21.7/1.21.8/1.21.10 and tested normally on 1.21.11 / 26.x.
- The startup wait has a 600s budget shared across both phases: phase 1 waits for the container to start producing logs, phase 2 waits for Paper's `Done (Xs)!` line (which only appears after all plugins and worlds are fully loaded).

**Exit codes:** 0 = all pass, 1 = test failures, 2 = server failed to start.

## BentoBox Server Layout

```
plugins/
  BentoBox.jar                  ← loaded by Paper
  Vault.jar                     ← economy API bridge (Paper plugin)
  EssentialsX-x.x.x.jar        ← economy provider (Paper plugin)
  BentoBox/
    config.yml                  ← committed; controls database, language, etc.
    addons/
      AcidIsland-x.x.x.jar     ← loaded by BentoBox (NOT Paper)
      BSkyBlock-x.x.x.jar
      ... (31 addons total)
```

The `server/` directory is NOT committed — it is the Docker volume and is generated automatically on first `docker compose up`. It contains worlds, logs, Paper internals, and server.properties. Similarly, `plugins/BentoBox/locales/`, `database/`, and `panels/` are extracted from JARs at runtime and are gitignored.

## Key Configuration Files

- `addons.yml` — Master list of 31 addon GitHub repos (`BentoBoxWorld/AddonName` format) plus a `server_plugins` section for Paper plugins (Vault, EssentialsX) and the `minecraft_versions` test matrix, used by `fetch_jars.py` and `run_tests.py`. An addon entry may carry an optional `min_api` (lowest supported Minecraft API) — when the server is older, the harness skips that addon instead of failing.
- `plugins/BentoBox/config.yml` — JSON database, economy enabled (requires Vault + EssentialsX), en-US language
- `docker-compose.yml` — Paper server definition; RCON password is `bbox-test-harness`

## CI (GitHub Actions)

`.github/workflows/integration-test.yml` runs nightly at 03:00 UTC and on manual dispatch. Steps: checkout → fetch JARs → start Docker server → run tests → collect logs → upload artifacts → publish results via `dorny/test-reporter`.

`GITHUB_TOKEN` is auto-provided by GitHub Actions (no secret setup needed). Use a PAT locally for the higher rate limit (5,000 req/hr vs 60 unauthenticated).
