#!/usr/bin/env python3
"""
run_tests.py — BentoBox integration test runner.

Waits for the Paper server to finish loading, then runs a sequence of
smoke tests by issuing commands on the server console and analysing the
server log.

Commands are sent over the server console (stdin) via the itzg image's
`mc-send-to-console`, NOT over RCON. RCON dispatches commands off the main
thread, and some Paper builds (e.g. 26.2) reject that with
"Asynchronous Cannot perform command async!". Console commands run on the
main thread, so they work regardless of the Paper build. This requires the
container to be started with CREATE_CONSOLE_IN_PIPE=true (see docker-compose.yml).

Usage:
    python run_tests.py [--log-file PATH] [--container NAME]
                        [--config addons.yml] [--junit-output results.xml]
    (--host/--port/--password are accepted for backwards compatibility but
     are no longer used — command execution is console-based.)

Exit codes:
    0 — all tests passed
    1 — one or more tests failed
    2 — server failed to start
"""

import argparse
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent

import yaml


RCON_HOST = "localhost"
RCON_PORT = 25575
RCON_PASSWORD = "bbox-test-harness"
DOCKER_CONTAINER = "bbox-test-server"
STARTUP_TIMEOUT = 600  # seconds total budget (startup + Done line) — 30 addons + world gen can be slow
STARTUP_POLL = 5       # seconds between log-poll attempts
CMD_MAX_WAIT = 25      # max seconds to wait for a single command's console output
# `mc-send-to-console` refuses to run unless invoked as the server user (the itzg
# image's default runtime UID is 1000); `docker exec` otherwise defaults to root.
CONSOLE_UID = "1000"


# ─────────────────────────────────────────────────────────────
# Result model
# ─────────────────────────────────────────────────────────────

@dataclass
class TestResult:
    name: str
    passed: bool
    message: str = ""
    suite: str = "BentoBox Integration Tests"
    skipped: bool = False


@dataclass
class TestSuite:
    name: str
    results: list[TestResult] = field(default_factory=list)

    def add(self, name: str, passed: bool, message: str = "", skipped: bool = False):
        self.results.append(
            TestResult(name=name, passed=passed, message=message, suite=self.name, skipped=skipped)
        )

    @property
    def passed(self):
        return sum(1 for r in self.results if r.passed and not r.skipped)

    @property
    def failed(self):
        return sum(1 for r in self.results if not r.passed and not r.skipped)

    @property
    def skipped(self):
        return sum(1 for r in self.results if r.skipped)


# ─────────────────────────────────────────────────────────────
# Minecraft version compatibility
# ─────────────────────────────────────────────────────────────

def parse_mc_version(version: str) -> tuple[int, ...]:
    """Parse an MC version string ('1.21.11', '26.2') into a comparable tuple.

    Mojang's calendar scheme (26.x) sorts naturally after the old 1.21.x scheme
    because the leading component (26 vs 1) dominates the tuple comparison.
    """
    return tuple(int(p) for p in re.findall(r"\d+", version))


def find_incompatible_addons(config: dict, mc_version: str | None) -> dict[str, str]:
    """Map addon name → required min API for addons that can't run on this server.

    An addon entry in addons.yml may declare `min_api` (the lowest Minecraft API
    version it supports). When the server runs an older MC version, that addon
    will not load — and that is expected, not a failure. Returns {} when the MC
    version is unknown (no env/flag), so nothing is skipped by accident.
    """
    if not mc_version:
        return {}
    server = parse_mc_version(mc_version)
    incompatible: dict[str, str] = {}
    for addon in config.get("addons", []):
        min_api = addon.get("min_api")
        if min_api and parse_mc_version(min_api) > server:
            incompatible[addon["name"]] = min_api
    return incompatible


# ─────────────────────────────────────────────────────────────
# Log helpers
# ─────────────────────────────────────────────────────────────

def get_log_text(log_file: str | None, container: str) -> str:
    """
    Always fetches a fresh copy of the server log from the Docker container
    so stale cached files from previous runs are never used.
    Saves to log_file if given (useful for CI artifact upload).
    Falls back to the cached file only if Docker is unavailable.
    """
    try:
        result = subprocess.run(
            ["docker", "logs", container],
            capture_output=True, text=True, timeout=30
        )
        # Paper writes its output to stderr; stdout is usually empty
        text = result.stdout + result.stderr
        if not text.strip():
            print(f"  WARNING: No output from 'docker logs {container}' — is the container running?")
        if log_file:
            Path(log_file).write_text(text)
            print(f"  Log saved to {log_file}")
        return text
    except FileNotFoundError:
        # Docker CLI not available (e.g. CI environment without Docker on PATH)
        if log_file and Path(log_file).exists():
            print(f"  'docker' not found — reading cached log from {log_file}")
            return Path(log_file).read_text(errors="replace")
        print("  WARNING: 'docker' not found and no cached log — skipping log-based checks.")
        return ""
    except Exception as e:
        print(f"  WARNING: Could not fetch container logs: {e}")
        if log_file and Path(log_file).exists():
            return Path(log_file).read_text(errors="replace")
        return ""


# ─────────────────────────────────────────────────────────────
# Server startup wait
# ─────────────────────────────────────────────────────────────

def _container_running(container: str) -> bool:
    """True if the container is currently running (not exited/crashed)."""
    try:
        r = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", container],
            capture_output=True, text=True, timeout=15,
        )
        return r.stdout.strip() == "true"
    except Exception:
        return False


def _extract_crash(log_text: str) -> str:
    """If the server log shows a fatal crash, return a concise excerpt; else "".

    Looks for Paper's top-level crash markers and returns the exception block plus,
    if present, the first BentoBox-addon stack frame — which usually names the
    addon responsible (e.g. a populator spawning an entity during chunk gen).
    """
    lines = log_text.splitlines()
    is_crash = any(
        ("Encountered an unexpected exception" in l
         or "unrecoverableChunkSystemFailure" in l
         or "crash report has been saved" in l)
        for l in lines
    )
    if not is_crash:
        return ""

    # Build a focused summary instead of dumping the full (mostly internal) stack:
    # the top-level exception, each "Caused by:" (the real root cause), any
    # BentoBox-addon stack frame (names the culprit), and the stop/crash line.
    picked: list[str] = []
    for raw in lines:
        s = _clean_line(raw).strip()
        if not s:
            continue
        if "Encountered an unexpected exception" in s or s.startswith("Caused by:"):
            picked.append(s)
        elif ".jar//world.bentobox" in raw:
            picked.append(s if s.startswith("at ") else "at " + s)
        elif "crash report has been saved" in s or s.endswith("Stopping server"):
            picked.append(s)

    seen: set[str] = set()
    out: list[str] = []
    for l in picked:
        if l not in seen:
            seen.add(l)
            out.append(l)
    return "\n".join(out[:25])


def wait_for_server(timeout: int, container: str = "bbox-test-server") -> bool:
    """Wait for the server to be fully ready.

    Phase 1 — container up: polls until the container is producing log output
    (Paper process started, but plugins may still be loading).

    Phase 2 — Done line: polls docker logs until Paper prints its
    'Done (Xs)! For help' line, which is only emitted once all plugins have
    loaded, all worlds have generated, and the server is fully open for play.
    """
    deadline = time.time() + timeout

    # ── Phase 1: wait for the container to start logging ─────────────────────
    print(f"Phase 1 — waiting for container '{container}' to start (timeout={timeout}s)...")
    container_up = False
    while time.time() < deadline:
        if _docker_logs(container).strip():
            print("  Container is up and producing logs")
            container_up = True
            break
        time.sleep(STARTUP_POLL)

    if not container_up:
        print(f"  TIMEOUT: container '{container}' produced no logs within {timeout}s")
        return False

    # ── Phase 2: wait for Paper's "Done" line in the log ────────────────────
    # Paper logs: [HH:MM:SS INFO]: Done (47.832s)! For help, type "help"
    # This is emitted only after all plugins have loaded and worlds are ready.
    #
    # `docker logs` keeps returning output after the container has exited, so a
    # crashed server's "Done" line is still visible. We therefore (a) check for a
    # fatal crash first, and (b) confirm the container is actually still running
    # before declaring the server ready — otherwise commands would later fail with
    # the confusing "container is not running".
    remaining = deadline - time.time()
    print(f"Phase 2 — waiting for server 'Done' in log (up to {int(remaining)}s remaining)...")
    done_pattern = re.compile(r"Done \(\d+[\d.]*s\)!")
    while time.time() < deadline:
        log = _docker_logs(container)

        crash = _extract_crash(log)
        if crash:
            print("  Server CRASHED during startup:")
            print(crash)
            return False

        if done_pattern.search(log):
            if _container_running(container):
                match = re.search(r"Done \(\d+[\d.]*s\)!.*", log)
                print(f"  Server fully ready: {match.group(0) if match else 'Done'}")
                return True
            print("  Server logged 'Done' but the container is no longer running — it crashed.")
            print(_extract_crash(log) or "  (no crash report found in log)")
            return False

        if not _container_running(container):
            print("  Container exited before startup completed — server crashed.")
            print(_extract_crash(log) or "  (no crash report found in log)")
            return False

        time.sleep(STARTUP_POLL)

    print(f"  TIMEOUT: Server 'Done' line never appeared within {timeout}s")
    return False


# ─────────────────────────────────────────────────────────────
# Console command helpers
# ─────────────────────────────────────────────────────────────

def _docker_logs(container: str) -> str:
    """Return the full current server log from the container, quietly.

    stderr is merged into stdout (stderr=STDOUT) so lines stay in chronological,
    append-only order. Concatenating stdout+stderr separately would interleave
    the two streams incorrectly (Paper logs and JVM warnings go to different
    streams), which breaks the offset-based output capture in console_command().
    """
    try:
        r = subprocess.run(
            ["docker", "logs", container],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", timeout=30,
        )
        return r.stdout
    except Exception:
        return ""


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_SECTION_RE = re.compile(r"§[0-9a-fk-orA-FK-OR]")
# Paper console prefixes, e.g. "[17:19:13 INFO]: " and "[17:19:13] [Server thread/INFO]: "
_PREFIX_THREAD_RE = re.compile(r"^\[\d{1,2}:\d{2}:\d{2}\]\s\[[^\]]+\]:\s?")
_PREFIX_PLAIN_RE = re.compile(r"^\[\d{1,2}:\d{2}:\d{2}\s+[A-Za-z]+\]:\s?")


def _clean_line(line: str) -> str:
    """Strip ANSI/section colour codes and the Paper log prefix from a log line."""
    line = _ANSI_RE.sub("", line)
    line = _SECTION_RE.sub("", line)
    line = _PREFIX_THREAD_RE.sub("", line, count=1)
    line = _PREFIX_PLAIN_RE.sub("", line, count=1)
    return line.rstrip()


def _send_to_console(container: str, *args: str) -> subprocess.CompletedProcess:
    """Write a command to the server's console named pipe via `mc-send-to-console`."""
    return subprocess.run(
        ["docker", "exec", "--user", CONSOLE_UID, container, "mc-send-to-console", *args],
        capture_output=True, text=True, timeout=30,
    )


_marker_seq = 0


def console_command(container: str, command: str, max_wait: float = CMD_MAX_WAIT) -> str:
    """Run a command on the server console (main thread) and return its output.

    Sends the command to the server's console named pipe via the itzg image's
    `mc-send-to-console` (requires CREATE_CONSOLE_IN_PIPE=true). Console commands
    run on the main thread, avoiding the async-dispatch rejection that RCON hits
    on Paper 26.2+.

    Output is captured deterministically: immediately after the command, a unique
    `say <marker>` is sent. Console commands are processed in order on the main
    thread, so once the marker appears in the log, the target command's output is
    exactly the log lines between our starting offset and the marker line. This
    avoids racing against unrelated/lazy log output (e.g. JVM warnings).
    """
    global _marker_seq
    _marker_seq += 1
    marker = f"BBOXTESTMARK{_marker_seq}X{int(time.time() * 1000) % 1000000}"

    before = len(_docker_logs(container).splitlines())

    send = _send_to_console(container, *command.split())
    if send.returncode != 0:
        detail = (send.stderr or send.stdout or "").strip()
        return f"ERROR: could not send '{command}' to console: {detail}"
    _send_to_console(container, "say", marker)

    deadline = time.time() + max_wait
    while time.time() < deadline:
        lines = _docker_logs(container).splitlines()
        for i in range(before, len(lines)):
            if marker in lines[i]:
                # Everything between our offset and the marker line is the response.
                resp = [_clean_line(l) for l in lines[before:i]]
                return "\n".join(resp).strip()
        time.sleep(0.3)

    # Marker never appeared — return the raw delta so failures are still diagnosable.
    lines = _docker_logs(container).splitlines()
    return "\n".join(_clean_line(l) for l in lines[before:]).strip()


# ─────────────────────────────────────────────────────────────
# Test suites
# ─────────────────────────────────────────────────────────────

def test_core_load(bbox_v: str) -> TestSuite:
    """
    Verify BentoBox loaded correctly using the output of 'bbox v'.

    Expected output contains lines like:
        BentoBox version: 3.11.2-SNAPSHOT-LOCAL
        Database: JSON
        Loaded Addons:
        AcidIsland 1.20.1 (ENABLED)
    """
    suite = TestSuite("Core Load")

    suite.add(
        "BentoBox version command responds",
        "BentoBox version:" in bbox_v,
        bbox_v[:300]
    )
    suite.add(
        "Database type reported",
        "Database:" in bbox_v,
        bbox_v[:300]
    )
    suite.add(
        "Addon list present in output",
        "Loaded Addons:" in bbox_v,
        bbox_v[:300]
    )
    suite.add(
        "No ERROR in version output",
        "ERROR" not in bbox_v.upper(),
        f"Found ERROR in: {bbox_v[:300]}"
    )
    return suite


def test_addon_enabled(bbox_v: str, addon_names: list[str],
                       skip_addons: dict[str, str] | None = None) -> TestSuite:
    """
    Check that each addon reports (ENABLED) in the 'bbox v' console output.

    'bbox v' lists every loaded addon with its status:
        AcidIsland 1.20.1 (ENABLED)
        Bank 1.9.0 (DISABLED)

    This is the authoritative post-load state, more reliable than log
    scraping because it reflects the final status after all enabling logic.

    Addons in `skip_addons` (name → required min API) declare a higher minimum
    Minecraft API than this server provides, so they are expected not to load —
    they are recorded as SKIPPED (a warning), not a failure.
    """
    suite = TestSuite("Addon Load")
    skip_addons = skip_addons or {}

    if not bbox_v:
        suite.add("bbox v output available", False,
                  "No 'bbox v' response — cannot verify addon status")
        return suite

    for addon_name in addon_names:
        if addon_name in skip_addons:
            suite.add(
                f"{addon_name} is ENABLED",
                True,
                f"SKIPPED: {addon_name} requires Minecraft API {skip_addons[addon_name]}, "
                f"newer than this server — incompatibility is expected, not a failure",
                skipped=True,
            )
            continue

        enabled_pattern = re.compile(
            rf"^\s*{re.escape(addon_name)}\s+\S+\s+\(ENABLED\)",
            re.IGNORECASE | re.MULTILINE
        )
        disabled_pattern = re.compile(
            rf"^\s*{re.escape(addon_name)}\s+\S+\s+\(DISABLED\)",
            re.IGNORECASE | re.MULTILINE
        )

        if enabled_pattern.search(bbox_v):
            suite.add(f"{addon_name} is ENABLED", True, "")
        elif disabled_pattern.search(bbox_v):
            suite.add(
                f"{addon_name} is ENABLED",
                False,
                f"{addon_name} loaded but is DISABLED (missing dependency?)"
            )
        else:
            suite.add(
                f"{addon_name} is ENABLED",
                False,
                f"'{addon_name} x.x.x (ENABLED)' not found in 'bbox v' output"
            )
    return suite


def test_worlds_registered(bbox_v: str, expected_worlds: list[tuple[str, str]],
                          skip_addons: dict[str, str] | None = None) -> TestSuite:
    """
    Check that each expected game world appears in the 'bbox v' console output.

    'bbox v' lists loaded game worlds:
        acidisland_world (AcidIsland): Overworld, Nether, The End
        bskyblock_world (BSkyBlock): Overworld, Nether, The End

    `expected_worlds` is a list of (world_name, owning_addon) pairs. A world whose
    owning addon is in `skip_addons` (incompatible with this server's MC version)
    is recorded as SKIPPED rather than a failure.
    """
    suite = TestSuite("World Registration")
    skip_addons = skip_addons or {}

    if not bbox_v:
        suite.add("bbox v output available", False,
                  "No 'bbox v' response — cannot verify world registration")
        return suite

    for world_name, owner in expected_worlds:
        if owner in skip_addons:
            suite.add(
                f"World '{world_name}' registered",
                True,
                f"SKIPPED: {owner} requires Minecraft API {skip_addons[owner]}, "
                f"newer than this server — world is expected to be absent",
                skipped=True,
            )
            continue

        pattern = re.compile(
            rf"\b{re.escape(world_name)}\b",
            re.IGNORECASE
        )
        found = bool(pattern.search(bbox_v))
        suite.add(
            f"World '{world_name}' registered",
            found,
            "" if found else f"'{world_name}' not found in 'bbox v' output"
        )
    return suite


def test_commands_registered(container: str,
                            skip_addons: dict[str, str] | None = None) -> TestSuite:
    """
    Verify key BentoBox and addon commands are registered by running them
    with no arguments and checking for usage/help output rather than
    'Unknown command'.

    Commands owned by an addon in `skip_addons` (incompatible with this server's
    MC version) are recorded as SKIPPED rather than a failure.
    """
    suite = TestSuite("Command Registration")
    skip_addons = skip_addons or {}

    # Each entry: (command, expected_pattern, label, owning_addon)
    # "only available in-game" is an acceptable response — it means the command
    # IS registered with the server; it just needs a player sender (not console).
    # owning_addon is None for core commands (always expected).
    commands_to_check = [
        # BentoBox core
        ("bbox",      r"(bentobox|usage|admin|version)",        "BentoBox admin",         None),
        # Game-mode admin commands (confirmed names from tastybento)
        ("bsbadmin",  r"(usage|bsb|skyblock|sub-command)",      "BSkyBlock admin",        "BSkyBlock"),
        ("acid",      r"(usage|acid|acidisland|sub-command)",   "AcidIsland command",     "AcidIsland"),
        ("obadmin",   r"(usage|ob|oneblock|sub-command)",       "AOneBlock admin",        "AOneBlock"),
        ("cbadmin",   r"(usage|cb|caveblock|sub-command)",      "CaveBlock admin",        "CaveBlock"),
        ("boxadmin",  r"(usage|box|boxed|sub-command)",         "Boxed admin",            "Boxed"),
        ("sgadmin",   r"(usage|sg|skygrid|sub-command)",        "SkyGrid admin",          "SkyGrid"),
        ("padmin",    r"(usage|poseidon|sub-command)",          "Poseidon admin",         "Poseidon"),
        ("stranger",  r"(usage|stranger|sub-command)",          "StrangerRealms command", "StrangerRealms"),
        ("parkour",   r"(usage|parkour|sub-command)",           "Parkour command",        "Parkour"),
    ]

    for cmd, pattern, label, owner in commands_to_check:
        if owner and owner in skip_addons:
            suite.add(
                f"/{cmd} — {label}",
                True,
                f"SKIPPED: {owner} requires Minecraft API {skip_addons[owner]}, "
                f"newer than this server — command is expected to be absent",
                skipped=True,
            )
            continue

        response = console_command(container, cmd)
        matched   = bool(re.search(pattern, response, re.IGNORECASE))
        unknown   = "unknown command" in response.lower()
        in_game   = "only available in-game" in response.lower()
        # Pass if pattern matched, or if the server says "in-game only"
        # (command is registered; the console just lacks a player context)
        passed = (matched and not unknown) or in_game
        suite.add(
            f"/{cmd} — {label}",
            passed,
            f"Response: {response[:150]}"
        )
    return suite


def test_no_console_errors(log_text: str,
                          skip_addons: dict[str, str] | None = None) -> TestSuite:
    """
    Parse the server log for WARN/ERROR/SEVERE entries related to BentoBox
    or any addon. This is the log-file equivalent of your current manual scan.

    Lines mentioning an addon in `skip_addons` are ignored: that addon is known
    to be incompatible with this server's MC version, so any load warning/error
    it produces is expected, not a regression.
    """
    suite = TestSuite("Console Health")
    skip_addons = skip_addons or {}

    if not log_text:
        suite.add("Log available for health checks", False,
                  "No log text — skipping console health checks")
        return suite

    lines = log_text.splitlines()
    if skip_addons:
        skip_re = re.compile("|".join(re.escape(name) for name in skip_addons), re.IGNORECASE)
        lines = [l for l in lines if not skip_re.search(l)]

    # ERROR/SEVERE lines mentioning BentoBox or addon packages
    bbox_errors = [
        line for line in lines
        if re.search(r"\[(ERROR|SEVERE)\]", line)
        and re.search(r"(bentobox|BentoBox|addon)", line, re.IGNORECASE)
    ]
    bbox_warns = [
        line for line in lines
        if "[WARN]" in line
        and re.search(r"(bentobox|BentoBox|addon)", line, re.IGNORECASE)
        and "No metrics" not in line
        and "bStats" not in line
    ]

    suite.add(
        "No BentoBox ERROR/SEVERE in log",
        len(bbox_errors) == 0,
        "\n".join(bbox_errors[:10]) if bbox_errors else ""
    )
    suite.add(
        "No unexpected BentoBox WARNs in log",
        len(bbox_warns) == 0,
        f"{len(bbox_warns)} warnings:\n" + "\n".join(bbox_warns[:5]) if bbox_warns else ""
    )

    # Plugin load failures — catches both hard load errors and Pladdon/class issues
    failed_enables = [
        line for line in lines
        if re.search(
            r"(failed to enable|could not load|encountered an error"
            r"|InvalidPluginException|NoClassDefFoundError|ClassNotFoundException)",
            line, re.IGNORECASE
        )
    ]
    # Deduplicate (stack traces repeat the same class name many times)
    seen = set()
    unique_failures = []
    for line in failed_enables:
        key = re.sub(r"\[[\d:]+\]", "", line).strip()  # strip timestamp
        if key not in seen:
            seen.add(key)
            unique_failures.append(line)

    suite.add(
        "No plugin load failures in log",
        len(unique_failures) == 0,
        "\n".join(unique_failures[:15]) if unique_failures else ""
    )

    return suite


# ─────────────────────────────────────────────────────────────
# JUnit XML output (for GitHub Actions test reporting)
# ─────────────────────────────────────────────────────────────

def write_junit_xml(suites: list[TestSuite], output_path: str):
    root = ET.Element("testsuites")
    for suite in suites:
        ts = ET.SubElement(root, "testsuite",
                           name=suite.name,
                           tests=str(len(suite.results)),
                           failures=str(suite.failed),
                           skipped=str(suite.skipped))
        for result in suite.results:
            tc = ET.SubElement(ts, "testcase",
                               name=result.name,
                               classname=result.suite)
            if result.skipped:
                skipped = ET.SubElement(tc, "skipped", message=result.message[:500])
                skipped.text = result.message
            elif not result.passed:
                failure = ET.SubElement(tc, "failure", message=result.message[:500])
                failure.text = result.message
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    tree.write(output_path, encoding="unicode", xml_declaration=True)
    print(f"\nJUnit XML written to {output_path}")


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="BentoBox integration test runner (console-based)")
    # --host/--port/--password are accepted for backwards compatibility (the CI
    # workflow passes them) but are unused: commands run over the server console.
    parser.add_argument("--host", default=RCON_HOST, help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, default=RCON_PORT, help=argparse.SUPPRESS)
    parser.add_argument("--password", default=RCON_PASSWORD, help=argparse.SUPPRESS)
    parser.add_argument("--log-file", default=None,
                        help="Path to write/read the server log (fetched via docker logs if missing)")
    parser.add_argument("--container", default=DOCKER_CONTAINER,
                        help="Docker container name to fetch logs from")
    parser.add_argument("--debug-log", action="store_true",
                        help="Print the lines around 'Loaded Addons' from the server log for regex debugging")
    parser.add_argument("--config", default=str(SCRIPT_DIR.parent / "addons.yml"))
    parser.add_argument("--junit-output", default=None,
                        help="Write JUnit XML results to this path (for CI)")
    parser.add_argument("--timeout", type=int, default=STARTUP_TIMEOUT)
    parser.add_argument("--mc-version", default=os.environ.get("MC_VERSION"),
                        help="Minecraft version under test (defaults to the MC_VERSION env var). "
                             "Used to skip addons whose declared min_api is newer than this server.")
    args = parser.parse_args()

    # Load addon list
    with open(args.config) as f:
        config = yaml.safe_load(f)
    addon_names = [a["name"] for a in config["addons"]]

    # Addons that can't run on this MC version (declared via `min_api` in addons.yml).
    # Their load/world/command checks become SKIPPED warnings instead of failures.
    skip_addons = find_incompatible_addons(config, args.mc_version)
    if skip_addons:
        print(f"\nMinecraft {args.mc_version}: the following addons require a newer API "
              f"and will be skipped (warnings, not failures):")
        for name, min_api in skip_addons.items():
            print(f"  - {name} (requires API {min_api})")

    # Expected game worlds as (world name prefix, owning addon) so worlds belonging
    # to a skipped/incompatible addon can be skipped too.
    expected_worlds = [
        ("acidisland_world", "AcidIsland"),
        ("boxed_world",      "Boxed"),
        ("bskyblock_world",  "BSkyBlock"),
        ("caveblock-world",  "CaveBlock"),
        ("oneblock_world",   "AOneBlock"),
        ("parkour_world",    "Parkour"),
        ("poseidon_world",   "Poseidon"),
        ("skygrid-world",    "SkyGrid"),
        ("stranger_world",   "StrangerRealms"),
    ]

    # Wait for server — two-phase: container up, then Paper "Done" line in log
    if not wait_for_server(args.timeout, container=args.container):
        log_text = get_log_text(args.log_file, args.container)
        crash = _extract_crash(log_text)
        # Emit a JUnit failure so the CI test reporter shows a clear cause rather
        # than an opaque "container is not running" on every later command.
        suite = TestSuite("Server Startup")
        suite.add(
            "Server reached ready state without crashing",
            False,
            crash or "Server did not reach 'Done' within the timeout (no crash report found).",
        )
        if args.junit_output:
            write_junit_xml([suite], args.junit_output)
        print("\nFATAL: Server never became fully ready (crash or timeout). Aborting tests.")
        if crash:
            print("\n--- crash excerpt ---\n" + crash)
        sys.exit(1)

    # Fetch log text now (server is confirmed fully started) — used by multiple suites
    print("Fetching server log...")
    log_text = get_log_text(args.log_file, args.container)

    if args.debug_log:
        lines = log_text.splitlines()
        print(f"\n--- DEBUG: log has {len(lines)} lines total ---")

        print("\n  [First 5 lines]")
        for i, l in enumerate(lines[:5]):
            print(f"  {i:4d}: {l}")

        print("\n  [Lines containing 'BentoBox' (first 20)]")
        bbox_lines = [(i, l) for i, l in enumerate(lines) if "bentobox" in l.lower()]
        for i, l in bbox_lines[:20]:
            print(f"  {i:4d}: {l}")
        if not bbox_lines:
            print("  (none found)")

        print("\n  [Lines containing 'AcidIsland' or 'BSkyBlock' (first 10)]")
        addon_lines = [(i, l) for i, l in enumerate(lines)
                       if "acidisland" in l.lower() or "bskyblock" in l.lower()]
        for i, l in addon_lines[:10]:
            print(f"  {i:4d}: {l}")
        if not addon_lines:
            print("  (none found)")

        print("\n  [Lines containing 'ENABLED' or 'loaded addon' (first 20)]")
        enabled_lines = [(i, l) for i, l in enumerate(lines)
                         if "enabled" in l.lower() or "loaded addon" in l.lower()]
        for i, l in enabled_lines[:20]:
            print(f"  {i:4d}: {l}")
        if not enabled_lines:
            print("  (none found)")

        print("--- END DEBUG ---\n")

    # Run all suites
    # Fetch 'bbox v' once — it drives Core Load, Addon Load, and World Registration.
    all_suites: list[TestSuite] = []
    print("\nFetching 'bbox v' output via the server console...")
    bbox_v = console_command(args.container, "bbox v")
    print(f"  Got {len(bbox_v)} chars")

    print("\n--- Suite: Core Load ---")
    all_suites.append(test_core_load(bbox_v))

    print("--- Suite: Command Registration ---")
    all_suites.append(test_commands_registered(args.container, skip_addons))

    print("--- Suite: Addon Load ---")
    all_suites.append(test_addon_enabled(bbox_v, addon_names, skip_addons))

    print("--- Suite: World Registration ---")
    all_suites.append(test_worlds_registered(bbox_v, expected_worlds, skip_addons))

    print("--- Suite: Console Health ---")
    all_suites.append(test_no_console_errors(log_text, skip_addons))

    # Print results
    print(f"\n{'='*60}")
    print(f"  BentoBox Integration Test Results  —  {datetime.now():%Y-%m-%d %H:%M}")
    print(f"{'='*60}")
    total_pass = 0
    total_fail = 0
    total_skip = 0
    for suite in all_suites:
        counts = f"{suite.passed} pass, {suite.failed} fail"
        if suite.skipped:
            counts += f", {suite.skipped} skip"
        print(f"\n  {suite.name}  ({counts})")
        for result in suite.results:
            icon = "⊘" if result.skipped else ("✓" if result.passed else "✗")
            print(f"    [{icon}] {result.name}")
            if (result.skipped or not result.passed) and result.message:
                for line in result.message.splitlines()[:3]:
                    print(f"          {line}")
        total_pass += suite.passed
        total_fail += suite.failed
        total_skip += suite.skipped

    print(f"\n{'='*60}")
    skip_note = f", {total_skip} skipped" if total_skip else ""
    print(f"  TOTAL: {total_pass} passed, {total_fail} failed{skip_note}")
    print(f"{'='*60}\n")

    if args.junit_output:
        write_junit_xml(all_suites, args.junit_output)

    sys.exit(0 if total_fail == 0 else 1)


if __name__ == "__main__":
    main()
