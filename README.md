# BentoBox Integration Test Harness

Automates smoke testing of BentoBox and all its addons on a live Paper server using Docker. Commands are issued over the server console (main thread) rather than RCON, so it works on Paper 26.2+ where RCON rejects off-main-thread command dispatch.

## What it tests

| Suite | Checks |
|---|---|
| **Core Load** | `bbox version` responds, database is reported, no ERROR in output |
| **Addon Load** | Every known addon shows `(ENABLED)` in BentoBox's startup report |
| **World Registration** | All game worlds are registered with BentoBox |
| **Command Registration** | Key admin commands respond (not "Unknown command") |
| **Console Health** | Server log is scanned for ERROR/SEVERE and failed-enable messages |

## Directory layout

```
bbox-test-harness/
├── docker-compose.yml              Paper server definition (itzg/minecraft-server)
├── addons.yml                      Master list of addons + GitHub repos
├── server/
│   └── server.properties           offline-mode, peaceful, small view distance (console pipe set in compose)
├── plugins/                        Mounted as /data/plugins inside the container
│   ├── BentoBox-x.y.z.jar          (git-ignored) — BentoBox plugin JAR
│   └── BentoBox/
│       ├── config.yml              Minimal BentoBox config (JSON DB, no economy)
│       └── addons/                 (git-ignored) — addon JARs loaded by BentoBox
│           ├── AcidIsland-x.y.jar
│           └── ...
└── scripts/
    ├── requirements.txt
    ├── fetch_jars.py               Downloads latest release JARs from GitHub
    └── run_tests.py                console + log test runner → JUnit XML output
```

> **Important**: Addon JARs go in `plugins/BentoBox/addons/`, **not** `plugins/`. BentoBox has its own addon loader that reads from that directory — Paper's plugin loader must not see them.

## Running locally

### Prerequisites

- Docker + Docker Compose
- Python 3.12+
- A GitHub personal access token (recommended — see below)

### Getting a GitHub token

All the addon repos are public, so the token needs **no special permissions at all** — it is used only to identify your requests and raise the API rate limit from 60 to 5,000 requests/hour, preventing the 403 errors you'll otherwise hit when querying ~34 repos in quick succession.

1. Go to **GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens**
   (or visit https://github.com/settings/personal-access-tokens/new directly)
2. Give it a name, e.g. `bbox-test-harness`
3. Set **Expiration** to whatever suits you (e.g. 1 year)
4. Under **Repository access** leave it on **Public Repositories (read-only)** — no extra permissions needed
5. Click **Generate token** and copy the value (it starts with `github_pat_…`)

Then export it in your shell before running the script:

```bash
export GITHUB_TOKEN=github_pat_yourtoken
```

You can add that line to your `~/.zshrc` so it's always available.

```bash
cd test/bbox-test-harness

# 1. Create and activate a virtual environment (required on Python 3.14+ / Homebrew)
python3 -m venv .venv
source .venv/bin/activate   # re-run this in each new terminal session

# 2. Install Python deps
pip install -r scripts/requirements.txt

# 3. Download latest release JARs
#    BentoBox JAR goes to plugins/, addon JARs go to plugins/BentoBox/addons/
#    Use --bentobox-jar to supply your local snapshot build (recommended, since
#    addon releases are often compiled against the latest snapshot API):
export GITHUB_TOKEN=ghp_yourtoken
python scripts/fetch_jars.py \
  --bentobox-jar ~/git/bentobox/build/libs/BentoBox-3.11.2-SNAPSHOT-LOCAL.jar
#    Or omit --bentobox-jar to download the latest stable release instead.

# 4. Start the server
docker compose up -d

# 5. Run tests (waits up to 5 min for server to start)
#    The script fetches the server log from Docker automatically
python scripts/run_tests.py --log-file /tmp/bbox.log

# 6. Tear down
docker compose down -v
```

### Supplying your own JARs

Just copy JARs manually into `plugins/` before `docker compose up`. `fetch_jars.py` is optional — useful for CI but not required locally.

## CI / GitHub Actions

The workflow at `.github/workflows/integration-test.yml` runs nightly and can also be triggered manually via the Actions tab. It:

1. Downloads the latest successful CI build for every addon
2. Spins up the Paper server in Docker
3. Runs the full test suite
4. Publishes results as a JUnit report in the Actions UI
5. Uploads the full server log as an artifact

Add `GITHUB_TOKEN` as a repository secret. The token needs no special scopes (read-only public access is enough) — it's used purely to avoid GitHub's burst rate limit when querying 34 repos in sequence. In GitHub Actions, `secrets.GITHUB_TOKEN` is available automatically.

## Adding a new addon

Edit `addons.yml` and add an entry under `addons:`:

```yaml
- name: MyAddon
  repo: BentoBoxWorld/MyAddon
```

If an addon only supports a newer Minecraft version, add `min_api` so older servers
in the matrix skip it (recorded as a warning, not a failure) instead of failing:

```yaml
- name: CaveBlock
  repo: BentoBoxWorld/CaveBlock
  min_api: "1.21.11"   # skipped on servers older than 1.21.11
```

A game mode also needs its world added to `expected_worlds` and its admin command
to `commands_to_check`, both in `scripts/run_tests.py`.

## Silencing a known-benign log line

`test_no_console_errors` fails on any BentoBox/addon `ERROR`, `SEVERE` or `WARN`.
When a line is genuinely harmless, add a regex to `log_whitelist` in `addons.yml`
**with the reason it is safe** — an unexplained whitelist decays into "ignore
everything" and the check goes quietly blind:

```yaml
log_whitelist:
  - pattern: "Unknown listing in blocks section:"
    reason: >
      Level's config targets current Minecraft, so older servers in the matrix
      report materials that do not exist yet. Tracks server age, not a defect.
```

Every run prints how many lines each pattern swallowed, so a pattern that has
grown too broad shows up in the CI output. Prefer fixing the addon over
whitelisting; if you must whitelist a real bug to keep the matrix green, mark it
`KNOWN BUG` and delete the entry when the upstream fix ships.

## Extending the tests

`run_tests.py` is structured as composable `TestSuite` functions. To add a new check, either append to an existing suite or add a new `test_*` function and call it from `main()`. The `console_command()` helper sends a command to the server console (via `mc-send-to-console`), captures its output from the server-log delta using a unique marker, and strips Minecraft colour codes and log prefixes so you can use plain regex against the response.
