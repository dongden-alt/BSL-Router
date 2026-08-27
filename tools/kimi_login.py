#!/usr/bin/env python3
"""Kimi (kimi.com) session scraper.

Headful Chrome automation via ``nodriver`` — same skeleton as
``tools/qwen_gh_login.py``. Opens kimi.com with a persistent Chrome
profile; if no token is in localStorage, waits for the user to log in
manually. Prefers the longer-lived refresh token; falls back to the
access token (the router's kimi module refreshes either). Then POSTs to
/api/chat-lane/kimi/import.

Usage:
python tools/kimi_login.py --profile data/kimi_profiles/<name>
python tools/kimi_login.py --profile data/kimi_profiles/<name> --json-out path.json
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import nodriver

KIMI_URL = "https://www.kimi.com"
IMPORT_PATH = "/api/chat-lane/kimi/import"
POLL_TIMEOUT_S = 300          # manual login can be slow — 5 minutes
POLL_INTERVAL_S = 2

# kimi.com localStorage key names (known versions); refresh_token is
# preferred because the access token's server TTL is ~15 minutes.
TOKEN_KEYS = ("refresh_token", "token", "access_token")


def _b64url_decode(data: str) -> bytes:
    rem = len(data) % 4
    if rem:
        data += "=" * (4 - rem)
    return base64.urlsafe_b64decode(data)


def _is_jwt(value: str) -> bool:
    parts = value.split(".")
    if len(parts) != 3:
        return False
    try:
        payload = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
        return isinstance(payload, dict)
    except Exception:
        return False


async def _read_token(page) -> tuple[str, str]:
    """Return (token, source_key). Prefers refresh_token over access."""
    try:
        store = await page.evaluate("JSON.stringify(localStorage)") or "{}"
    except Exception:
        return "", ""
    try:
        items = json.loads(store)
    except Exception:
        return "", ""
    if not isinstance(items, dict):
        return "", ""
    for key in TOKEN_KEYS:
        v = items.get(key)
        if isinstance(v, str) and _is_jwt(v):
            return v, key
    return "", ""


async def _collect(
    profile_dir: Path,
    *,
    router: str,
    conn_name: str | None,
    json_out: Path | None,
    headless: bool,
) -> int:
    try:
        browser = await nodriver.start(
            headless=headless,
            browser_args=[f"--user-data-dir={profile_dir}"],
        )
    except Exception as exc:
        print(f"ERROR: failed to start browser: {exc}", file=sys.stderr)
        return 1

    try:
        page = await browser.get(KIMI_URL)
        await page.wait()
        await page.sleep(3)

        token, key = await _read_token(page)

        if not token:
            print(
                "No token found — please LOG IN in the opened Chrome window.\n"
                "Waiting up to 5 minutes for a token to appear...",
                flush=True,
            )
            deadline = time.time() + POLL_TIMEOUT_S
            while time.time() < deadline:
                await page.sleep(POLL_INTERVAL_S)
                token, key = await _read_token(page)
                if token:
                    break
                print(".", end="", flush=True)

        if not token:
            print(
                f"\nERROR: no token appeared in localStorage after {POLL_TIMEOUT_S}s.",
                file=sys.stderr,
            )
            return 1
        print(f"\nToken found via localStorage key: {key}"
              + (" (refresh)" if "refresh" in key else " (access — shorter-lived)"))

        name = (conn_name or "").strip() or f"kimi-{int(time.time())}"
        payload = {"token": token, "name": name}

        print(f"collected account: {name} (token {len(token)} chars)")

        router_base = router.rstrip("/")
        import_url = urljoin(router_base, IMPORT_PATH)
        posted = False
        try:
            import httpx

            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(import_url, json=payload)
                if resp.status_code in (200, 201):
                    print(f"\nImported via router: {resp.json()}")
                    posted = True
                else:
                    print(
                        f"\nRouter returned HTTP {resp.status_code}: {resp.text[:300]}",
                        file=sys.stderr,
                    )
        except Exception as net_exc:
            print(f"\nNetwork error posting to router ({import_url}): {net_exc}", file=sys.stderr)

        if json_out or not posted:
            out_path = json_out or (profile_dir / f"{name}.json")
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"Credentials written to: {out_path}")

        return 0
    finally:
        try:
            browser.stop()
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="collect a Kimi (kimi.com) token via a persistent Chrome profile.",
    )
    parser.add_argument("--profile", required=True,
                        help="Persistent Chrome profile directory (e.g. data/kimi_profiles/alice).")
    parser.add_argument("--name", default=None,
                        help="Connection name for the router (default: kimi-<timestamp>).")
    parser.add_argument("--router", default="http://localhost:6969",
                        help="BSL Router base URL (default: http://localhost:6969).")
    parser.add_argument("--json-out", default=None,
                        help="Also write credentials to this JSON file (fallback on network failure).")
    parser.add_argument("--headless", action="store_true",
                        help="Run Chrome headless (useful for re-runs on warm profiles).")
    args = parser.parse_args(argv)

    profile_dir = Path(args.profile)
    json_out = Path(args.json_out) if args.json_out else None
    return asyncio.run(_collect(
        profile_dir,
        router=args.router,
        conn_name=args.name,
        json_out=json_out,
        headless=args.headless,
    ))


if __name__ == "__main__":
    raise SystemExit(main())
