#!/usr/bin/env python3
"""
run_tests.py — BentoBox integration test runner.

Waits for the Paper server to finish loading, then runs a sequence of
smoke tests via RCON and server log analysis.

Usage:
    python run_tests.py [--host HOST] [--port PORT] [--password PASSWORD]
                        [--log-file PATH] [--container NAME]
                        [--config addons.yml] [--junit-output results.xml]

Exit codes:
    0 — all tests passed
    1 — one or more tests failed
    2 — server failed to start / could not connect
"""

import argparse
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

try:
    from mcrcon import MCRcon
except ImportError:
    print("ERROR: mcrcon is not installed. Run: pip install mcrcon")
    sys.exit(2)


RCON_HOST = "localhost"
RCON_PORT = 25575
RCON_PASSWORD = "bbox-test-harness"
DOCKER_CONTAINER = "bbox-test-server"
STARTUP_TIMEOUT = 600  # seconds total budget (RCON + Done line) — 30 addons + world gen can be slow
STARTUP_POLL = 5       # seconds between connection/log-poll attempts


# ─────────────────────────────────────────────────────────────
# Result model
# ─────────────────────────────────────────────────────────────

@dataclass
class TestResult:
    name: str
    passed: bool
    message: str = ""
    suite: str = "BentoBox Integration Tests"


@dataclass
class TestSuite:
    name: str
    results: list[TestResult] = field(default_factory=list)

    def add(self, name: str, passed: bool, message: str = ""):
        self.results.append(TestResult(name=name, passed=passed, message=message, suite=self.name))

    @property
    def passed(self):
        return sum(1 for r in self.results if r.passed)

    @property
    def failed(self):
        return sum(1 for r in self.results if not r.passed)


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

def wait_for_server(host: str, port: int, password: str, timeout: int,
                    container: str = "bbox-test-server") -> bool:
    """Wait for the server to be fully ready.

    Phase 1 — RCON alive: polls until RCON accepts connections (Paper process up,
    but plugins may still be loading).

    Phase 2 — Done line: polls docker logs until Paper prints its
    'Done (Xs)! For help' line, which is only emitted once all plugins have
    loaded, all worlds have generated, and the server is fully open for play.

    mcrcon uses signal.SIGALRM internally, which can fire during time.sleep()
    and raise MCRconException outside the inner try block. We guard both
    sleep() calls in their own try/except for this reason.
    """
    deadline = time.time() + timeout

    # ── Phase 1: wait for RCON ───────────────────────────────────────────────
    print(f"Phase 1 — waiting for RCON at {host}:{port} (timeout={timeout}s)...")
    attempts = 0
    rcon_ready = False
    while time.time() < deadline:
        try:
            with MCRcon(host, password, port=port, timeout=30) as mcr:
                mcr.command("list")
                elapsed = attempts * STARTUP_POLL
                print(f"  RCON ready after ~{elapsed}s")
                rcon_ready = True
                break
        except Exception:
            attempts += 1
            try:
                time.sleep(STARTUP_POLL)
            except Exception:
                pass

    if not rcon_ready:
        print(f"  TIMEOUT: RCON never responded within {timeout}s")
        return False

    # ── Phase 2: wait for Paper's "Done" line in the log ────────────────────
    # Paper logs: [HH:MM:SS INFO]: Done (47.832s)! For help, type "help"
    # This is emitted only after all plugins have loaded and worlds are ready.
    remaining = deadline - time.time()
    print(f"Phase 2 — waiting for server 'Done' in log (up to {int(remaining)}s remaining)...")
    done_pattern = re.compile(r"Done \(\d+[\d.]*s\)!")
    while time.time() < deadline:
        try:
            log = subprocess.run(
                ["docker", "logs", container],
                capture_output=True, text=True, errors="replace"
            )
            combined = log.stdout + log.stderr
            if done_pattern.search(combined):
                # Extract the Done line for a nice message
                match = re.search(r"Done \(\d+[\d.]*s\)!.*", combined)
                print(f"  Server fully ready: {match.group(0) if match else 'Done'}")
                return True
        except Exception as e:
            print(f"  WARNING: could not read docker logs: {e}")
        try:
            time.sleep(STARTUP_POLL)
        except Exception:
            pass

    print(f"  TIMEOUT: Server 'Done' line never appeared within {timeout}s")
    return False


# ─────────────────────────────────────────────────────────────
# RCON helpers
# ─────────────────────────────────────────────────────────────

def rcon(mcr: MCRcon, command: str) -> str:
    """Send a command and return the stripped response."""
    # RCON responses contain Minecraft color codes — strip them
    raw = mcr.command(command)
    clean = re.sub(r"§[0-9a-fk-orA-FK-OR]", "", raw)
    return clean.strip()


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


def test_addon_enabled(bbox_v: str, addon_names: list[str]) -> TestSuite:
    """
    Check that each addon reports (ENABLED) in the 'bbox v' RCON output.

    'bbox v' lists every loaded addon with its status:
        AcidIsland 1.20.1 (ENABLED)
        Bank 1.9.0 (DISABLED)

    This is the authoritative post-load state, more reliable than log
    scraping because it reflects the final status after all enabling logic.
    """
    suite = TestSuite("Addon Load")

    if not bbox_v:
        suite.add("bbox v output available", False,
                  "No 'bbox v' response — cannot verify addon status")
        return suite

    for addon_name in addon_names:
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


def test_worlds_registered(bbox_v: str, expected_worlds: list[str]) -> TestSuite:
    """
    Check that each expected game world appears in the 'bbox v' RCON output.

    'bbox v' lists loaded game worlds:
        acidisland_world (AcidIsland): Overworld, Nether, The End
        bskyblock_world (BSkyBlock): Overworld, Nether, The End
    """
    suite = TestSuite("World Registration")

    if not bbox_v:
        suite.add("bbox v output available", False,
                  "No 'bbox v' response — cannot verify world registration")
        return suite

    for world_name in expected_worlds:
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


def test_commands_registered(mcr: MCRcon) -> TestSuite:
    """
    Verify key BentoBox and addon commands are registered by running them
    with no arguments and checking for usage/help output rather than
    'Unknown command'.
    """
    suite = TestSuite("Command Registration")

    # Each entry: (rcon_command, expected_pattern, label)
    # "only available in-game" is an acceptable response — it means the command
    # IS registered with the server; it just needs a player sender (not RCON).
    commands_to_check = [
        # BentoBox core
        ("bbox",      r"(bentobox|usage|admin|version)",        "BentoBox admin"),
        # Game-mode admin commands (confirmed names from tastybento)
        ("bsbadmin",  r"(usage|bsb|skyblock|sub-command)",      "BSkyBlock admin"),
        ("acid",      r"(usage|acid|acidisland|sub-command)",   "AcidIsland command"),
        ("obadmin",   r"(usage|ob|oneblock|sub-command)",       "AOneBlock admin"),
        ("cbadmin",   r"(usage|cb|caveblock|sub-command)",      "CaveBlock admin"),
        ("boxadmin",  r"(usage|box|boxed|sub-command)",         "Boxed admin"),
        ("sgadmin",   r"(usage|sg|skygrid|sub-command)",        "SkyGrid admin"),
        ("padmin",    r"(usage|poseidon|sub-command)",          "Poseidon admin"),
        ("stranger",  r"(usage|stranger|sub-command)",          "StrangerRealms command"),
        ("parkour",   r"(usage|parkour|sub-command)",           "Parkour command"),
    ]

    for cmd, pattern, label in commands_to_check:
        response = rcon(mcr, cmd)
        matched   = bool(re.search(pattern, response, re.IGNORECASE))
        unknown   = "unknown command" in response.lower()
        in_game   = "only available in-game" in response.lower()
        # Pass if pattern matched, or if the server says "in-game only"
        # (command is registered; RCON just lacks a player context)
        passed = (matched and not unknown) or in_game
        suite.add(
            f"/{cmd} — {label}",
            passed,
            f"Response: {response[:150]}"
        )
    return suite


def test_no_console_errors(log_text: str) -> TestSuite:
    """
    Parse the server log for WARN/ERROR/SEVERE entries related to BentoBox
    or any addon. This is the log-file equivalent of your current manual scan.
    """
    suite = TestSuite("Console Health")

    if not log_text:
        suite.add("Log available for health checks", False,
                  "No log text — skipping console health checks")
        return suite

    lines = log_text.splitlines()

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
                           failures=str(suite.failed))
        for result in suite.results:
            tc = ET.SubElement(ts, "testcase",
                               name=result.name,
                               classname=result.suite)
            if not result.passed:
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
    parser = argparse.ArgumentParser(description="BentoBox RCON integration test runner")
    parser.add_argument("--host", default=RCON_HOST)
    parser.add_argument("--port", type=int, default=RCON_PORT)
    parser.add_argument("--password", default=RCON_PASSWORD)
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
    args = parser.parse_args()

    # Load addon list
    with open(args.config) as f:
        config = yaml.safe_load(f)
    addon_names = [a["name"] for a in config["addons"]]

    # Expected game worlds (world name prefix from BentoBox startup log)
    expected_worlds = [
        "acidisland_world",
        "boxed_world",
        "bskyblock_world",
        "caveblock-world",
        "oneblock_world",
        "parkour_world",
        "poseidon_world",
        "skygrid-world",
        "stranger_world",
    ]

    # Wait for server — two-phase: RCON alive, then Paper "Done" line in log
    if not wait_for_server(args.host, args.port, args.password, args.timeout,
                           container=args.container):
        print("FATAL: Server never became fully ready. Aborting tests.")
        sys.exit(2)

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
    with MCRcon(args.host, args.password, port=args.port, timeout=30) as mcr:
        print("\nFetching 'bbox v' output via RCON...")
        bbox_v = rcon(mcr, "bbox v")
        print(f"  Got {len(bbox_v)} chars")

        print("\n--- Suite: Core Load ---")
        all_suites.append(test_core_load(bbox_v))

        print("--- Suite: Command Registration ---")
        all_suites.append(test_commands_registered(mcr))

    print("--- Suite: Addon Load ---")
    all_suites.append(test_addon_enabled(bbox_v, addon_names))

    print("--- Suite: World Registration ---")
    all_suites.append(test_worlds_registered(bbox_v, expected_worlds))

    print("--- Suite: Console Health ---")
    all_suites.append(test_no_console_errors(log_text))

    # Print results
    print(f"\n{'='*60}")
    print(f"  BentoBox Integration Test Results  —  {datetime.now():%Y-%m-%d %H:%M}")
    print(f"{'='*60}")
    total_pass = 0
    total_fail = 0
    for suite in all_suites:
        print(f"\n  {suite.name}  ({suite.passed} pass, {suite.failed} fail)")
        for result in suite.results:
            icon = "✓" if result.passed else "✗"
            print(f"    [{icon}] {result.name}")
            if not result.passed and result.message:
                for line in result.message.splitlines()[:3]:
                    print(f"          {line}")
        total_pass += suite.passed
        total_fail += suite.failed

    print(f"\n{'='*60}")
    print(f"  TOTAL: {total_pass} passed, {total_fail} failed")
    print(f"{'='*60}\n")

    if args.junit_output:
        write_junit_xml(all_suites, args.junit_output)

    sys.exit(0 if total_fail == 0 else 1)


if __name__ == "__main__":
    main()
