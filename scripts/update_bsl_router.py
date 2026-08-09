#!/usr/bin/env python3
"""BSL Router Auto-Updater.

Downloads the latest release ZIP from GitHub, extracts it over the
current installation (preserving config.yaml and .venv), then restarts
the server via bslrouter.ps1.

NEVER touches:
  - config.yaml (user database, provider keys)
  - .venv/ (virtual environment)
  - .brain/ (agent state)

Usage:
  python scripts/update_bsl_router.py --url <zip_url> --version <tag>
  python scripts/update_bsl_router.py --check
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from urllib.request import urlopen, Request
from urllib.error import URLError

# Files/dirs that must NEVER be overwritten by an update
PROTECTED_PATHS = {
    "config.yaml",
    "config.yaml.bak",
    "config.yaml.pre-encryption.bak",
    ".venv",
    ".brain",
    ".bsl_key",
    "VERSION",  # Updated separately after successful extraction
}

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def log(msg: str) -> None:
    print(f"[BSL-UPDATER] {msg}", flush=True)


def download_file(url: str, dest: str) -> bool:
    """Download a file from URL to dest."""
    try:
        req = Request(url, headers={"User-Agent": "BSL-Router-Updater"})
        with urlopen(req, timeout=120) as resp, open(dest, "wb") as f:
            shutil.copyfileobj(resp, f)
        return True
    except (URLError, OSError) as e:
        log(f"Download failed: {e}")
        return False


def verify_zip(zip_path: str) -> bool:
    """Verify the ZIP is valid."""
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            bad = zf.testzip()
            if bad:
                log(f"Corrupt entry in ZIP: {bad}")
                return False
        return True
    except (zipfile.BadZipFile, OSError) as e:
        log(f"ZIP verification failed: {e}")
        return False


def extract_update(zip_path: str, target_dir: str) -> bool:
    """Extract ZIP contents, skipping protected paths.

    GitHub source ZIPs have a top-level directory prefix (e.g. bsl-router-v1.2.3/).
    We strip it so files land directly in target_dir.
    """
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            # Detect common prefix
            names = zf.namelist()
            if not names:
                log("ZIP is empty")
                return False

            # Check for common top-level directory
            prefix = ""
            first_parts = names[0].split("/")
            if len(first_parts) > 1:
                candidate = first_parts[0] + "/"
                if all(n.startswith(candidate) for n in names):
                    prefix = candidate
                    log(f"Stripping prefix: {prefix}")

            extracted = 0
            for info in zf.infolist():
                # Skip directories
                if info.is_dir():
                    continue

                # Strip prefix
                rel_path = info.filename
                if prefix and rel_path.startswith(prefix):
                    rel_path = rel_path[len(prefix):]
                elif prefix:
                    continue  # File outside expected prefix, skip

                if not rel_path:
                    continue

                # Check protected paths
                top_level = rel_path.split("/")[0]
                if top_level in PROTECTED_PATHS:
                    log(f"Skipping protected path: {rel_path}")
                    continue

                # Extract
                dest = os.path.join(target_dir, rel_path)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with zf.open(info) as src, open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                extracted += 1

            log(f"Extracted {extracted} files")
            return extracted > 0

    except (zipfile.BadZipFile, OSError) as e:
        log(f"Extraction failed: {e}")
        return False


def restart_server() -> bool:
    """Restart BSL Router using the canonical launcher."""
    launcher = os.path.join(PROJECT_ROOT, "scripts", "bslrouter.ps1")
    if not os.path.exists(launcher):
        log(f"Launcher not found: {launcher}")
        log("Manual restart required.")
        return False

    try:
        # Spawn restart as detached process
        creation_flags = 0x08000000  # CREATE_NO_WINDOW
        if sys.platform == "win32":
            creation_flags |= 0x00000008  # DETACHED_PROCESS

        subprocess.Popen(
            ["powershell", "-ExecutionPolicy", "Bypass", "-File", launcher, "restart"],
            creationflags=creation_flags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        log("Restart signal sent via bslrouter.ps1")
        return True
    except Exception as e:
        log(f"Restart failed: {e}")
        log("Manual restart required: run scripts/bslrouter.ps1 restart")
        return False


def update_version_file(version: str) -> None:
    """Write the new version to VERSION file."""
    version_path = os.path.join(PROJECT_ROOT, "VERSION")
    try:
        with open(version_path, "w", encoding="utf-8") as f:
            f.write(version.strip() + "\n")
        log(f"Version updated to {version}")
    except OSError as e:
        log(f"Failed to update VERSION file: {e}")


def run_update(zip_url: str, version: str) -> int:
    """Main update flow."""
    log(f"Starting update to version {version}")
    log(f"Download URL: {zip_url}")

    # Step 1: Download
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False, dir=tempfile.gettempdir()) as tmp:
        tmp_zip = tmp.name

    try:
        log("Downloading update package...")
        if not download_file(zip_url, tmp_zip):
            return 1

        # Step 2: Verify
        log("Verifying download...")
        if not verify_zip(tmp_zip):
            return 1

        # Step 3: Backup current config
        config_path = os.path.join(PROJECT_ROOT, "config.yaml")
        if os.path.exists(config_path):
            backup_path = config_path + ".update-bak"
            shutil.copy2(config_path, backup_path)
            log(f"Config backed up to {backup_path}")

        # Step 4: Extract
        log("Extracting update files...")
        if not extract_update(tmp_zip, PROJECT_ROOT):
            return 1

        # Step 5: Update version
        update_version_file(version)

        # Step 6: Restore config if it was somehow touched
        if os.path.exists(config_path + ".update-bak"):
            shutil.copy2(config_path + ".update-bak", config_path)
            log("Config restored from backup")

        # Step 7: Restart
        log("Restarting server...")
        time.sleep(2)  # Give the API response time to flush
        restart_server()

        log(f"Update to {version} completed successfully!")
        return 0

    finally:
        try:
            os.unlink(tmp_zip)
        except OSError:
            pass


def check_latest() -> int:
    """Check for latest release and print info."""
    import json
    try:
        url = "https://api.github.com/repos/bsl-router/bsl-router/releases/latest"
        req = Request(url, headers={"Accept": "application/vnd.github.v3+json", "User-Agent": "BSL-Router-Updater"})
        with urlopen(req, timeout=15) as resp:
            release = json.loads(resp.read().decode("utf-8"))

        # Read current version
        version_path = os.path.join(PROJECT_ROOT, "VERSION")
        current = "0.0.0"
        if os.path.exists(version_path):
            with open(version_path, "r") as f:
                current = f.read().strip()

        latest = release.get("tag_name", "").lstrip("v")
        print(f"Current version: {current}")
        print(f"Latest version:  {latest}")
        print(f"Release URL:     {release.get('html_url', '')}")
        if release.get("body"):
            print(f"Release notes:\n{release['body'][:500]}")
        return 0
    except Exception as e:
        print(f"Check failed: {e}")
        return 1


def main():
    parser = argparse.ArgumentParser(description="BSL Router Auto-Updater")
    parser.add_argument("--url", help="Update ZIP download URL")
    parser.add_argument("--version", help="Target version tag")
    parser.add_argument("--check", action="store_true", help="Check for latest release")
    args = parser.parse_args()

    if args.check:
        sys.exit(check_latest())

    if not args.url or not args.version:
        parser.error("--url and --version are required (or use --check)")

    sys.exit(run_update(args.url, args.version))


if __name__ == "__main__":
    main()
