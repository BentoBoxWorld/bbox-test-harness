#!/usr/bin/env python3
"""
fetch_jars.py — Downloads the latest release JAR for BentoBox and all its addons.

Uses the GitHub Releases API. All repos are public, but GitHub's unauthenticated
rate limit (60 req/hr) and burst limits mean a token is recommended for reliability.

A fine-grained token with no special permissions (read-only public access) is enough.
Set via GITHUB_TOKEN env var or --token flag.

Usage:
    python fetch_jars.py [--output-dir ../plugins] [--config ../addons.yml] [--token TOKEN]
"""

import argparse
import os
import sys
import time
from pathlib import Path

import requests
import yaml

GITHUB_API = "https://api.github.com"
SCRIPT_DIR = Path(__file__).parent

# Delay between API calls to avoid triggering GitHub's burst/abuse rate limit.
# With a token this can be lower; without one it needs to be more conservative.
DELAY_WITH_TOKEN = 0.2     # seconds
DELAY_WITHOUT_TOKEN = 1.5  # seconds


def get_latest_release_jar(repo: str, session: requests.Session, asset_prefix: str | None = None) -> tuple[str, str] | None:
    """
    Returns (filename, download_url) for the JAR asset in the latest release,
    or None if no JAR asset is found.
    Skips sources/javadoc JARs.
    If asset_prefix is given, only assets whose name starts with that prefix are considered.
    """
    url = f"{GITHUB_API}/repos/{repo}/releases/latest"
    resp = session.get(url, timeout=15)

    if resp.status_code == 404:
        print(f"  SKIP: No releases found for {repo}")
        return None
    if resp.status_code == 403:
        reset = resp.headers.get("X-RateLimit-Reset", "unknown")
        remaining = resp.headers.get("X-RateLimit-Remaining", "unknown")
        print(f"  FAIL: 403 Forbidden — likely rate limited (remaining={remaining}, reset={reset})")
        print(f"        Set GITHUB_TOKEN env var or pass --token to avoid this.")
        raise requests.HTTPError(response=resp)

    resp.raise_for_status()

    release = resp.json()
    tag = release.get("tag_name", "unknown")

    jar_assets = [
        asset for asset in release.get("assets", [])
        if asset["name"].endswith(".jar")
        and not any(x in asset["name"] for x in ("-sources", "-javadoc", "-slim"))
        and (asset_prefix is None or asset["name"].startswith(asset_prefix))
    ]

    if not jar_assets:
        print(f"  SKIP: No JAR asset in release {tag} for {repo}")
        return None

    # Prefer the largest JAR if there are multiple (avoids stub/api JARs)
    asset = max(jar_assets, key=lambda a: a["size"])
    print(f"  Found: {asset['name']} (tag {tag})")
    return asset["name"], asset["browser_download_url"]


def download_jar(name: str, filename: str, url: str, output_dir: Path, session: requests.Session):
    dest = output_dir / filename
    print(f"  Downloading {filename}...")
    # browser_download_url is a plain HTTPS redirect — use a bare session without
    # the GitHub API Accept header so the CDN serves the file directly.
    resp = requests.get(url, timeout=60, stream=True)
    resp.raise_for_status()
    with open(dest, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)
    size_kb = dest.stat().st_size // 1024
    print(f"  ✓ {filename} ({size_kb} KB)")


def main():
    parser = argparse.ArgumentParser(
        description="Fetch latest release JARs for BentoBox and all addons."
    )
    parser.add_argument("--output-dir", default=str(SCRIPT_DIR.parent / "plugins"))
    parser.add_argument("--config", default=str(SCRIPT_DIR.parent / "addons.yml"))
    parser.add_argument(
        "--token",
        default=os.environ.get("GITHUB_TOKEN"),
        help="GitHub token (or set GITHUB_TOKEN env var). Recommended to avoid rate limits.",
    )
    parser.add_argument(
        "--bentobox-jar",
        default=None,
        help=(
            "Path to a local BentoBox JAR to use instead of the latest GitHub release. "
            "Use this when testing a snapshot build that addons require but hasn't been released yet. "
            "Example: --bentobox-jar ~/git/bentobox/build/libs/BentoBox-3.11.2-SNAPSHOT.jar"
        ),
    )
    args = parser.parse_args()

    # BentoBox JAR → plugins/
    # Addon JARs  → plugins/BentoBox/addons/   (BentoBox's own addon loader reads from here)
    plugins_dir = Path(args.output_dir).resolve()
    addons_dir = plugins_dir / "BentoBox" / "addons"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    addons_dir.mkdir(parents=True, exist_ok=True)

    with open(args.config) as f:
        config = yaml.safe_load(f)

    session = requests.Session()
    session.headers.update({"Accept": "application/vnd.github+json"})

    if args.token:
        session.headers["Authorization"] = f"Bearer {args.token}"
        delay = DELAY_WITH_TOKEN
        print("Using GitHub token (authenticated).")
    else:
        delay = DELAY_WITHOUT_TOKEN
        print("No GitHub token — using unauthenticated requests (slower, may hit rate limits).")
        print("Tip: create a free token at https://github.com/settings/tokens (no scopes needed)")
        print("     then run: export GITHUB_TOKEN=<your_token>\n")

    print(f"\nLayout:")
    print(f"  BentoBox JAR  → {plugins_dir}")
    print(f"  Addon JARs    → {addons_dir}")
    print(f"  Server plugins → {plugins_dir}")

    failed = []

    # ── BentoBox itself ──────────────────────────────────────────────────────
    print(f"\n[BentoBox]")
    if args.bentobox_jar:
        src = Path(args.bentobox_jar).expanduser().resolve()
        if src.exists():
            import shutil
            dest = plugins_dir / src.name
            shutil.copy2(src, dest)
            print(f"  ✓ Copied local JAR: {src.name}")
        else:
            print(f"  FAIL: --bentobox-jar path not found: {src}")
            failed.append("BentoBox")
    else:
        print(f"  Fetching from GitHub releases ({config['bentobox']['repo']})...")
        try:
            result = get_latest_release_jar(config["bentobox"]["repo"], session)
            if result:
                filename, url = result
                download_jar("BentoBox", filename, url, plugins_dir, session)
            else:
                failed.append("BentoBox")
        except Exception as e:
            print(f"  FAIL: {e}")
            failed.append("BentoBox")
        time.sleep(delay)

    # ── Addons (go into plugins/BentoBox/addons/) ────────────────────────────
    for entry in config["addons"]:
        name = entry["name"]
        repo = entry["repo"]
        print(f"\n[{name}] {repo}")
        try:
            result = get_latest_release_jar(repo, session)
            if result:
                filename, url = result
                download_jar(name, filename, url, addons_dir, session)
            else:
                failed.append(name)
        except requests.HTTPError as e:
            print(f"  FAIL: HTTP {e.response.status_code}")
            failed.append(name)
        except Exception as e:
            print(f"  FAIL: {e}")
            failed.append(name)

        time.sleep(delay)

    # ── Server plugins (go into plugins/, same as BentoBox) ──────────────────
    for entry in config.get("server_plugins", []):
        name = entry["name"]
        repo = entry["repo"]
        asset_prefix = entry.get("asset_prefix")
        print(f"\n[{name}] {repo}")
        try:
            result = get_latest_release_jar(repo, session, asset_prefix)
            if result:
                filename, url = result
                download_jar(name, filename, url, plugins_dir, session)
            else:
                failed.append(name)
        except requests.HTTPError as e:
            print(f"  FAIL: HTTP {e.response.status_code}")
            failed.append(name)
        except Exception as e:
            print(f"  FAIL: {e}")
            failed.append(name)

        time.sleep(delay)

    total = 1 + len(config["addons"]) + len(config.get("server_plugins", []))
    succeeded = total - len(failed)
    print(f"\n{'='*50}")
    print(f"Downloaded {succeeded}/{total} JARs")
    print(f"  BentoBox + server plugins → {plugins_dir}")
    print(f"  Addons                   → {addons_dir}")
    if failed:
        print(f"Failed: {', '.join(failed)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
