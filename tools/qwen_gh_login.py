#!/usr/bin/env python3
"""Qwen (chat.qwen.ai) GitHub-OAuth session scraper.

Headful Chrome automation via ``nodriver`` (no Playwright / camoufox
dependency). Opens chat.qwen.ai in a persistent Chrome profile; if the page
has no localStorage token, clicks "Continue with GitHub" and lets the user
log in. Once localStorage["token"] holds a JWT, collects the token, all
cookies for .qwen.ai, and the browser User-Agent, then POSTs them to the
router's /api/chat-lane/qwen/import endpoint.

Usage:
    python tools/qwen_gh_login.py --profile data/qwen_profiles/<name>
    python tools/qwen_gh_login.py --profile data/qwen_profiles/<name> --headless
    python tools/qwen_gh_login.py --profile data/qwen_profiles/<name> --json-out path.json
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urljoin

# nodriver is the only third-party dependency; everything else is stdlib.
import asyncio

import nodriver

QWEN_URL = "https://chat.qwen.ai"
IMPORT_PATH = "/api/chat-lane/qwen/import"
POLL_TIMEOUT_S = 180
POLL_INTERVAL_S = 2

# Windows Chrome stores per-profile data under LocalAppData; the cookie DB and
# the encryption key material (Local State) live in these two relative paths.
_CHROME_LOCALSTATE_REL = Path("Local State")
_CHROME_COOKIES_REL = Path("Network/Cookies")


def _chrome_user_data_dir() -> Path | None:
    """Locate the user's real Chrome user-data dir (standard install).

    Standard layout: %LOCALAPPDATA%\\Google\\Chrome\\User Data — 'User Data'
    is the user-data-dir root that contains 'Local State' and 'Default\\'.
    """
    cands = [
        Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "User Data",
        Path.home() / "AppData" / "Local" / "Google" / "Chrome",
    ]
    for c in cands:
        if (c / _CHROME_LOCALSTATE_REL).exists():
            return c
    return None


def _copy_locked_file(src: Path, dst: Path) -> bool:
    """Copy a file that Chrome holds an exclusive lock on.

    Plain copy fails with PermissionError while Chrome runs. ``esentutl /y
    /vss`` copies via Volume Shadow Copy — needs elevation, which the
    collector child inherits from the (elevated) router process.
    """
    import subprocess

    try:
        r = subprocess.run(
            ["esentutl", "/y", str(src), "/d", str(dst), "/vss"],
            capture_output=True, timeout=30,
        )
        return r.returncode == 0 and dst.exists()
    except Exception:
        return False


def _sniff_qwen_token_live(user_data: Path | None = None) -> str:
    """Best-effort scan of the REAL Chrome Local Storage for a qwen JWT.

    Reads the leveldb files raw (VSS fallback for files Chrome holds locked)
    and looks for a JWT in a file that also mentions qwen.ai. Used ONLY to
    detect that the user finished logging in inside their own browser; the
    authoritative token read happens afterwards via CDP on the snapshot.
    """
    ud = user_data or _chrome_user_data_dir()
    if not ud:
        return ""
    lsdb = ud / "Default" / "Local Storage" / "leveldb"
    if not lsdb.exists():
        return ""
    pat = re.compile(rb"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
    try:
        files = sorted(lsdb.iterdir())
    except Exception:
        return ""
    # FIX 2026-08-28: accumulators for the freshest non-expired token scan.
    _now = int(time.time())
    _best_token, _best_exp = "", 0
    for f in files:
        if not f.is_file():
            continue
        try:
            data = f.read_bytes()
        except PermissionError:
            fd, tmpname = tempfile.mkstemp(prefix="bsl-ldb-")
            os.close(fd)
            tmp = Path(tmpname)
            ok = _copy_locked_file(f, tmp)
            data = tmp.read_bytes() if ok and tmp.exists() else b""
            try:
                tmp.unlink()
            except Exception:
                pass
            if not data:
                continue
        except Exception:
            continue
        if b"qwen.ai" not in data:
            continue
        # FIX 2026-08-28 (stale-first-match): leveldb keeps OLD token entries
        # alongside the new one after a re-login. The previous first-match
        # logic kept returning an EXPIRED token, so the 180s login-wait always
        # timed out even though the user had signed in. Scan ALL JWTs and keep
        # the FRESHEST NON-EXPIRED one instead.
        for m in pat.finditer(data):
            tok = m.group(0).decode("ascii", "ignore")
            exp = _decode_jwt_exp(tok)
            if exp and exp > _now and exp > _best_exp:
                _best_token, _best_exp = tok, exp
    return _best_token


def _make_skeleton_profile(dest: Path) -> str:
    """Copy a minimal 'skeleton' of the REAL Chrome profile into dest.

    Returns "" on success, or an error message explaining why the snapshot
    failed. Chrome 127+ app-bound encryption means only chrome.exe can decrypt
    the cookie DB, but it CAN when given a copied Local State + Network/Cookies.
    The Qwen JWT lives in localStorage, so we must also copy the Local Storage
    leveldb folder. Copying just these (not the multi-GB profile) gives the
    spawned Chrome the user's real qwen.ai session WITHOUT touching the live
    profile (no lock conflict with an already-running Chrome).
    """
    src = _chrome_user_data_dir()
    if not src:
        return "real Chrome user-data dir not found (standard install expected)"
    try:
        ls_src = src / _CHROME_LOCALSTATE_REL
        ck_src = src / _CHROME_COOKIES_REL
        lsdb_src = src / "Default" / "Local Storage" / "leveldb"
        if not ls_src.exists():
            return "real Chrome 'Local State' not found"
        if not ck_src.exists() and not lsdb_src.exists():
            return "real Chrome has neither Cookies nor Local Storage — not logged in?"
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ls_src, dest / _CHROME_LOCALSTATE_REL)
        if ck_src.exists():
            (dest / "Default" / "Network").mkdir(parents=True, exist_ok=True)
            # Cookies AND Cookies-wal: Chrome stages recent cookie writes in
            # the SQLite WAL — copying only the main DB yields a logged-OUT
            # snapshot (live bug 2026-08-27: popup asked for login).
            for rel in ("Cookies", "Cookies-wal"):
                src_f = src / "Default" / "Network" / rel
                if not src_f.exists():
                    continue
                dst_f = dest / "Default" / "Network" / rel
                try:
                    shutil.copy2(src_f, dst_f)
                except PermissionError:
                    if not _copy_locked_file(src_f, dst_f) and rel == "Cookies":
                        return (
                            "real Chrome is running and its cookie DB is locked. "
                            "Either close Chrome first, or run the router elevated "
                            "(VSS shadow copy)."
                        )
        if lsdb_src.exists():
            dst_lsdb = dest / "Default" / "Local Storage" / "leveldb"
            dst_lsdb.mkdir(parents=True, exist_ok=True)
            for f in lsdb_src.iterdir():
                if not f.is_file():
                    continue
                try:
                    shutil.copy2(f, dst_lsdb / f.name)
                except PermissionError:
                    _copy_locked_file(f, dst_lsdb / f.name)  # best effort
        return ""
    except PermissionError as exc:
        return (
            f"cannot copy real Chrome data (file locked: {exc.filename or exc}). "
            "Close Chrome windows for qwen.ai or use incognito mode."
        )
    except Exception as exc:
        return f"skeleton snapshot failed: {exc}"


def _b64url_decode(data: str) -> bytes:
    rem = len(data) % 4
    if rem:
        data += "=" * (4 - rem)
    return base64.urlsafe_b64decode(data)


def _decode_jwt_exp(token: str) -> int:
    parts = token.split(".")
    if len(parts) != 3:
        return 0
    try:
        raw = _b64url_decode(parts[1])
        payload = json.loads(raw.decode("utf-8"))
        return int(payload.get("exp") or 0)
    except Exception:
        return 0


def _days_left(token: str) -> int:
    exp = _decode_jwt_exp(token)
    if not exp:
        return -1
    return max(0, (exp - int(time.time())) // 86400)


def _sniffed_token_expired(token: str) -> bool:
    """FIX 2026-08-28: True when the JWT exists but its exp has passed.

    Used by the mybrowser flow so a stale token in the real Chrome is treated
    as absent (re-login) instead of being injected and re-exported forever.
    Unparseable tokens return False — the caller's structural checks handle
    those separately.
    """
    exp = _decode_jwt_exp(token)
    return bool(exp) and exp <= int(time.time())


def _save_error_screenshot(profile_dir: Path, page, message: str) -> None:
    """Dump a screenshot to <profile>/error.png and print the message."""
    error_dir = profile_dir
    error_dir.mkdir(parents=True, exist_ok=True)
    png_path = error_dir / "error.png"
    try:
        page.save(png_path)
    except Exception:
        pass
    print(f"ERROR: {message}", file=sys.stderr)
    print(f"  Screenshot saved to: {png_path}", file=sys.stderr)


async def _collect(
    profile_dir: Path,
    *,
    router: str,
    conn_name: str | None,
    json_out: Path | None,
    headless: bool,
    mode: str = "profile",
) -> int:
    """Run the collect flow. Returns 0 on success, 1 on failure.

    Modes:
      * ``profile``   — classic persistent profile (saved logins survive).
      * ``mybrowser`` — skeleton snapshot of the user's REAL Chrome profile;
                        if you are logged into Qwen there, collect is instant.
      * ``incognito`` — fresh throwaway profile for the new-account loop
                        (login GitHub here -> login Qwen -> collect -> close).
    """
    browser_args: list[str] = []
    effective_dir = profile_dir
    cleanup_dir: Path | None = None
    inject_token = ""

    if mode == "mybrowser":
        # REAL-BROWSER TOKEN-INJECTION FLOW (v3, 2026-08-27): cookie snapshots
        # are worthless on Chrome 127+ (app-bound encryption invalidates
        # copied cookies — root cause of the 'empty/clean browser' failures).
        # But the Qwen JWT sits in localStorage as PLAINTEXT, which we can
        # sniff from the real Chrome. Flow: read token from real Chrome
        # (opening a tab in the user's browser to complete login if needed)
        # -> open a CLEAN collect browser -> inject the JWT into
        # chat.qwen.ai's localStorage -> the site logs itself in and sets
        # fresh WAF cookies itself.
        token_live = _sniff_qwen_token_live()
        if not token_live:
            import webbrowser
            webbrowser.open(QWEN_URL)
            print("Opened chat.qwen.ai as a tab in YOUR Chrome.", flush=True)
            print("Finish the login there (GitHub is already signed in — one click).", flush=True)
            print("Waiting for the Qwen token", end="", flush=True)
            deadline = time.time() + POLL_TIMEOUT_S
            while time.time() < deadline:
                time.sleep(POLL_INTERVAL_S)
                token_live = _sniff_qwen_token_live()
                if token_live:
                    break
                print(".", end="", flush=True)
            print(flush=True)
            if not token_live:
                print(
                    f"ERROR: no Qwen token appeared in your browser after "
                    f"{POLL_TIMEOUT_S}s. Did the login complete in the tab?",
                    file=sys.stderr,
                )
                return 1
        else:
            # FIX 2026-08-28 (dead-token loop): a sniffed token whose exp has
            # passed is worthless — injecting it makes the site log out and the
            # collect re-exports the SAME dead token (import then 422s with
            # "Token has expired"). Treat an expired token as absent so the
            # user is prompted to re-login in their real Chrome instead.
            if _sniffed_token_expired(token_live):
                print(
                    "Qwen token in your real Chrome is EXPIRED — need a fresh login.",
                    flush=True,
                )
                token_live = ""
                import webbrowser
                webbrowser.open(QWEN_URL)
                print("Opened chat.qwen.ai as a tab in YOUR Chrome.", flush=True)
                print(
                    "Sign OUT (avatar menu) then sign IN again (GitHub) — the "
                    "old token must be replaced.",
                    flush=True,
                )
                print("Waiting for the fresh Qwen token", end="", flush=True)
                deadline = time.time() + POLL_TIMEOUT_S
                while time.time() < deadline:
                    time.sleep(POLL_INTERVAL_S)
                    candidate = _sniff_qwen_token_live()
                    if candidate and not _sniffed_token_expired(candidate):
                        token_live = candidate
                        break
                    print(".", end="", flush=True)
                print(flush=True)
                if not token_live:
                    print(
                        f"ERROR: no FRESH Qwen token appeared in your browser "
                        f"after {POLL_TIMEOUT_S}s. The old token is expired — "
                        "complete the sign-out/sign-in at chat.qwen.ai first.",
                        file=sys.stderr,
                    )
                    return 1
            else:
                print("Found Qwen token in your real Chrome.", flush=True)
        inject_token = token_live
        tmp = Path(tempfile.mkdtemp(prefix="bsl-qwen-collect-"))
        cleanup_dir = tmp
        effective_dir = tmp
        print("collecting headless (no window will appear)...", flush=True)
    elif mode == "incognito":
        tmp = Path(tempfile.mkdtemp(prefix="bsl-qwen-incognito-"))
        cleanup_dir = tmp
        effective_dir = tmp
        browser_args = ["--incognito"]
        print("Fresh incognito window — log in with GitHub, then Qwen; "
              "the collector will pick the token up automatically.", flush=True)

    # ZERO-WINDOW collect (2026-08-27): mybrowser mode must NEVER show a
    # visible window. The token is sniffed from the user's real Chrome (no
    # window); the collect browser below runs HEADLESS — it injects the JWT,
    # the site logs itself in and sets fresh WAF cookies, we collect them via
    # CDP, import, and exit. The user sees nothing.
    _force_headless = headless or mode == "mybrowser"
    try:
        browser = await nodriver.start(
            headless=_force_headless,
            browser_args=[f"--user-data-dir={effective_dir}"] + browser_args,
        )
    except Exception as exc:
        print(f"ERROR: failed to start browser: {exc}", file=sys.stderr)
        if cleanup_dir:
            shutil.rmtree(cleanup_dir, ignore_errors=True)
        return 1

    try:
        page = await browser.get(QWEN_URL)
        # Wait for the SPA to settle.
        await page.wait()
        await page.sleep(3)

        # TOKEN INJECTION (mybrowser): plant the JWT sniffed from the user's
        # real Chrome into this clean browser's localStorage on the qwen.ai
        # origin, then reload — the site sees a valid session, logs in and
        # sets fresh WAF cookies itself.
        if inject_token:
            try:
                await page.evaluate(
                    f"localStorage.setItem('token', {json.dumps(inject_token)})"
                )
                print("Token injected into collect browser — reloading...", flush=True)
                page = await browser.get(QWEN_URL)
                await page.wait()
                await page.sleep(3)
            except Exception as exc:
                print(f"WARN: token injection failed: {exc}", file=sys.stderr)

        # Try to read the token from localStorage.
        token = ""
        try:
            token = (await page.evaluate("localStorage.getItem('token')")) or ""
        except Exception:
            pass

        # If no token, click the GitHub OAuth button — EXCEPT in mybrowser mode,
        # where the window carries the user's REAL session: the user completes
        # any needed click there (GitHub is already logged in via the snapshot),
        # so auto-clicking a guessed button is wrong (caused the 'no GitHub
        # OAuth button' failure on the real-profile layout).
        if not token and mode != "mybrowser":
            # Selector discovery at runtime: find an anchor/button whose text or
            # href contains 'github' or 'oauth'.
            clicked = False
            try:
                # Try known selectors first (chat.qwen.ai renders a GitHub button).
                for sel in (
                    'a[href*="github"]',
                    'button[data-variant*="github" i]',
                    '[class*="github"]',
                ):
                    try:
                        el = await page.query_selector(sel, timeout=3)
                        if el:
                            await el.click()
                            clicked = True
                            break
                    except Exception:
                        continue
            except Exception:
                pass

            if not clicked:
                # Fallback: scan all clickable elements.
                try:
                    els = await page.query_selector_all("a, button, [role=button]")
                    for el in els:
                        try:
                            txt = (await el.get_text()).lower()
                            href = await el.get_attribute("href") or ""
                            if "github" in txt or "github" in href.lower() or "oauth" in txt:
                                await el.click()
                                clicked = True
                                break
                        except Exception:
                            continue
                except Exception:
                    pass

            if not clicked:
                _save_error_screenshot(
                    profile_dir, page,
                    "Could not find a GitHub OAuth button on the page. "
                    "The page layout may have changed."
                )
                return 1

            print("Clicked GitHub button — waiting for login + token...", flush=True)

        # Poll localStorage until a JWT appears (or timeout).
        deadline = time.time() + POLL_TIMEOUT_S
        while time.time() < deadline:
            try:
                token = (await page.evaluate("localStorage.getItem('token')")) or ""
            except Exception:
                token = ""
            if token and len(token.split(".")) == 3:
                break
            await page.sleep(POLL_INTERVAL_S)
            print(".", end="", flush=True)

        if not token or len(token.split(".")) != 3:
            _save_error_screenshot(
                profile_dir, page,
                f"No JWT token found in localStorage after {POLL_TIMEOUT_S}s. "
                "Please complete the GitHub login manually and retry."
            )
            return 1

        print()  # newline after dots

        # FIX 2026-08-28 (dead-token loop): refuse to export an expired token.
        # The import endpoint 422s on it and the account never shows up in the
        # UI — fail loudly here with actionable instructions instead.
        if _sniffed_token_expired(token):
            _save_error_screenshot(
                profile_dir, page,
                "collected Qwen token is EXPIRED. In YOUR Chrome, open "
                "chat.qwen.ai, sign out, sign back in (GitHub), then re-run "
                "this collect.",
            )
            return 1

        # collect cookies via CDP (nodriver browser.cookies API).
        # Include httpOnly ones — required: cnaui, aui, sca, xlly_s, cna,
        # token, _bl_uid, x-ap where present.
        cookies = await browser.cookies.get_all()
        qwen_cookies = [c for c in cookies if c.domain and "qwen.ai" in c.domain]
        cookie_header = "; ".join(f"{c.name}={c.value}" for c in qwen_cookies)

        # Capture the browser's actual User-Agent.
        ua = ""
        try:
            ua = (await page.evaluate("navigator.userAgent")) or ""
        except Exception:
            pass

        name = (conn_name or "").strip()
        if not name:
            # Default name = "qwen-" + first 8 chars of the JWT id claim (sub).
            try:
                payload_raw = _b64url_decode(token.split(".")[1])
                payload = json.loads(payload_raw.decode("utf-8"))
                uid = payload.get("sub") or payload.get("id") or payload.get("user_id") or ""
                name = f"qwen-{uid[:8]}" if uid else f"qwen-{int(time.time())}"
            except Exception:
                name = f"qwen-{int(time.time())}"

        payload = {
            "token": token,
            "cookies": cookie_header,
            "ua": ua,
            "name": name,
        }

        # Print human summary.
        exp = _decode_jwt_exp(token)
        days = _days_left(token)
        print(f"\ncollected account: {name}")
        print(f"  Token expires : {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(exp)) if exp else 'unknown'}")
        print(f"  Days left     : {days}")
        print(f"  Cookies       : {len(qwen_cookies)} entries ({len(cookie_header)} chars)")
        print(f"  UA            : {ua[:80]}...")

        # Try to POST to the router import endpoint.
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
                    print(f"\nRouter returned HTTP {resp.status_code}: {resp.text[:300]}", file=sys.stderr)
        except Exception as net_exc:
            print(f"\nNetwork error posting to router ({import_url}): {net_exc}", file=sys.stderr)

        # On network failure (or always if --json-out), write to file.
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
        if cleanup_dir:
            try:
                shutil.rmtree(cleanup_dir, ignore_errors=True)
            except Exception:
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="collect a Qwen (chat.qwen.ai) GitHub-OAuth token via a persistent Chrome profile.",
    )
    parser.add_argument(
        "--profile",
        required=True,
        help="Persistent Chrome profile directory (e.g. data/qwen_profiles/alice).",
    )
    parser.add_argument(
        "--name",
        default=None,
        help="Connection name for the router (default: auto-derived from JWT sub).",
    )
    parser.add_argument(
        "--router",
        default="http://localhost:6969",
        help="BSL Router base URL (default: http://localhost:6969).",
    )
    parser.add_argument(
        "--json-out",
        default=None,
        help="Also write credentials to this JSON file (fallback on network failure).",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run Chrome headless (useful for re-runs on warm profiles).",
    )
    parser.add_argument(
        "--mode",
        choices=["profile", "mybrowser", "incognito"],
        default="profile",
        help=(
            "profile: saved persistent profile (default). "
            "mybrowser: snapshot of your REAL Chrome profile (instant if you "
            "are logged into Qwen there). "
            "incognito: fresh window for adding a NEW GitHub/Qwen account."
        ),
    )
    args = parser.parse_args(argv)

    profile_dir = Path(args.profile)
    json_out = Path(args.json_out) if args.json_out else None

    return asyncio_run(_collect(
        profile_dir,
        router=args.router,
        conn_name=args.name,
        json_out=json_out,
        headless=args.headless,
        mode=args.mode,
    ))


def asyncio_run(coro):
    """Run a coroutine — works on Py 3.12+ (no loop param) and older."""
    try:
        return asyncio.run(coro)
    except TypeError:
        # Python < 3.10: asyncio.run exists; for very old versions fall back.
        import asyncio as _a
        loop = _a.new_event_loop()
        _a.set_event_loop(loop)
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()


if __name__ == "__main__":
    raise SystemExit(main())
