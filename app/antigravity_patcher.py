"""Local Antigravity runtime integration; no Node.js or external patcher required."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import tempfile

MARKER = "/* bsl-router-antigravity-patch: v1 */"
BLOCK_SIZE = 4 * 1024 * 1024


def _atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _jsonc_mask(text):
    """Blank comments without changing offsets or touching quoted strings."""
    return re.sub(r'"(?:\\.|[^"\\])*"|//[^\r\n]*|/\*[\s\S]*?\*/',
                  lambda m: m[0] if m[0].startswith('"') else re.sub(r'[^\r\n]', ' ', m[0]), text)


def patch_ide_settings(path, port=6969, restore=False):
    path = Path(path)
    if restore and not path.exists():
        return "not_installed"
    original = path.read_text(encoding="utf-8-sig") if path.exists() else "{}\n"
    masked = _jsonc_mask(original)
    # Validate JSONC first; never replace an unreadable user settings file.
    json.loads(re.sub(r',\s*([}\]])', r'\1', masked))
    match = re.search(r'"jetski\.cloudCodeUrl"\s*:\s*"(?:\\.|[^"\\])*"', masked)
    if match:
        if restore:
            start, end = match.span()
            after = re.match(r'\s*,', masked[end:])
            before = re.search(r',\s*$', masked[:start])
            if after:
                end += after.end()
            elif before:
                start = before.start()
            updated = original[:start] + original[end:]
        else:
            updated = original[:match.start()] + '"jetski.cloudCodeUrl": ' + json.dumps(f"http://127.0.0.1:{int(port)}") + original[match.end():]
    elif restore:
        return "restored"
    else:
        end = masked.rfind("}")
        previous = masked[:end].rstrip()
        comma = "" if previous.endswith(("{", ",")) else ","
        updated = original[:end] + comma + '\n  "jetski.cloudCodeUrl": ' + json.dumps(f"http://127.0.0.1:{int(port)}") + "\n" + original[end:]
    backup = path.with_name(path.name + ".bsl.bak")
    if not restore and path.exists() and not backup.exists():
        shutil.copy2(path, backup)
    _atomic_write(path, updated.encode("utf-8"))
    return "restored" if restore else "patched"


def unpack_asar(data):
    """Return the ASAR header and packed file bytes, preserving unpacked links."""
    if len(data) < 16:
        raise ValueError("Truncated ASAR header")
    size_size, header_size, pickle_size, json_size = struct.unpack_from("<4I", data)
    if size_size != 4 or json_size > header_size - 8 or 8 + header_size > len(data):
        raise ValueError("Invalid ASAR pickle header")
    header = json.loads(data[16:16 + json_size])
    files = {}

    def visit(node, prefix=""):
        for name, entry in node.get("files", {}).items():
            key = prefix + name
            if "files" in entry:
                visit(entry, key + "/")
            elif not entry.get("unpacked") and "offset" in entry:
                start = 8 + header_size + int(entry["offset"])
                end = start + int(entry["size"])
                if start < 8 + header_size or end > len(data):
                    raise ValueError("ASAR entry outside archive: " + key)
                files[key] = data[start:end]
    visit(header)
    return header, files


def pack_asar(header, files):
    header = json.loads(json.dumps(header))
    chunks = []
    offset = 0

    def visit(node, prefix=""):
        nonlocal offset
        for name, entry in node.get("files", {}).items():
            key = prefix + name
            if "files" in entry:
                visit(entry, key + "/")
            elif key in files and not entry.get("unpacked"):
                data = files[key]
                entry.update(size=len(data), offset=str(offset))
                entry["integrity"] = {
                    "algorithm": "SHA256", "hash": hashlib.sha256(data).hexdigest(),
                    "blockSize": BLOCK_SIZE,
                    "blocks": [hashlib.sha256(data[i:i + BLOCK_SIZE]).hexdigest()
                               for i in range(0, len(data), BLOCK_SIZE)],
                }
                chunks.append(data)
                offset += len(data)
    visit(header)
    encoded = json.dumps(header, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    padding = (-len(encoded)) % 4
    header_size = 8 + len(encoded) + padding
    return struct.pack("<4I", 4, header_size, header_size - 4, len(encoded)) + encoded + b"\0" * padding + b"".join(chunks)


def _patch_language_server(source, url):
    if MARKER in source:
        raise ValueError("Backup is already BSL-patched; refusing to use it as vendor source")
    pattern = r'((?:async\s+)?(?:function\s+startLanguageServer\s*\([^)]*\)|startLanguageServer\s*\([^)]*\))\s*\{)'
    injection = MARKER + "\nconst __bslRouterUrl = (typeof options === 'object' && options && options.wsl) ? undefined : " + json.dumps(url) + ";\n"
    source, count = re.subn(pattern, lambda m: m[0] + injection, source, count=1)
    if not count:
        raise ValueError("Unsupported languageServer.js: startLanguageServer not found")
    count = 0
    for flag in ("api_server_url", "cloud_code_endpoint", "inference_api_server_url"):
        pattern = r"(['\"]--" + flag + r"['\"]\s*,\s*)(['\"])(https://[^'\"]+)\2"
        source, n = re.subn(pattern, lambda m: m[1] + "(__bslRouterUrl || " + json.dumps(m[3]) + ")", source)
        count += n
    if not count:
        raise ValueError("Unsupported languageServer.js: endpoint arguments not found")
    if "--inference_api_server_url" not in source:
        # The inference endpoint must follow the same WSL-safe fallback as Cloud Code.
        pattern = r"(['\"]--cloud_code_endpoint['\"]\s*,\s*\(__bslRouterUrl \|\| ([^)]*)\))"
        source, n = re.subn(pattern, lambda m: m[0] + ", '--inference_api_server_url', (__bslRouterUrl || " + m[2] + ")", source, count=1)
        if not n:
            raise ValueError("Cannot install inference endpoint argument")
    return source


def _patch_main(source, url):
    if MARKER in source:
        raise ValueError("Backup is already BSL-patched")
    match = re.search(r'(?P<app>(?:[A-Za-z_$][\w$]*\.)*app)\s*\.whenReady\s*\(', source)
    if not match:
        raise ValueError("Unsupported main.js: app.whenReady not found")
    app_expression = match.group("app")
    # Register before the application's ready handlers. Requiring Electron here
    # avoids relying on the bundler's local variable names for session.
    hook = MARKER + "\n" + app_expression + ".whenReady().then(() => { require('electron').session.defaultSession.webRequest.onBeforeRequest((details, callback) => { try { const u = new URL(details.url); const lsPort = require('./languageServer').getLsPort(); if (['127.0.0.1', 'localhost'].includes(u.hostname) && (!lsPort || Number(u.port) === Number(lsPort))) { if (u.pathname.endsWith('LanguageServerService/SetCloudCodeURL')) return callback({cancel:true}); if (u.pathname.endsWith('LanguageServerService/GetAvailableModels')) return callback({redirectURL:" + json.dumps(url + "/GetAvailableModels?ls=") + " + encodeURIComponent(details.url)}); } } catch {} callback({}); }); });\n"
    return source[:match.start()] + hook + source[match.start():]


def _default_paths():
    roaming = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
    local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    ide = [roaming / "Antigravity IDE/User/settings.json"]
    alternate = roaming / "Antigravity/User/settings.json"
    if alternate.exists():
        ide.append(alternate)
    return ide, local / "Programs/antigravity/resources/app.asar"


def _original_backup(asar):
    backup = asar.parent / ".bsl-router-patch/original.app.asar"
    if backup.exists():
        return backup
    legacy = asar.parent / ".antigravity-model-patch"
    candidates = []
    if (legacy / "backups").exists():
        # Only use backups inside the known recovery directory, never arbitrary
        # paths supplied by another application's state file.
        candidates = sorted((legacy / "backups").glob("*/app.asar"), key=lambda p: p.stat().st_mtime)
    source = candidates[0] if candidates else asar
    header, files = unpack_asar(source.read_bytes())
    if any(marker in files.get(name, b"") for name in ("dist/main.js", "dist/languageServer.js") for marker in (MARKER.encode(), b"__ag_model_patch__")):
        raise ValueError("No clean vendor ASAR backup available")
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, backup)
    return backup


def apply_staged_asar_if_present(asar_path=None):
    asar = Path(asar_path) if asar_path else _default_paths()[1]
    staged = asar.with_name("app.asar.bsl-staged")
    if not staged.exists():
        return False
    try:
        os.replace(staged, asar)
        if asar_path is None:
            try:
                (Path.home() / ".gemini" / "antigravity" / "custom_models.json").unlink(missing_ok=True)
            except OSError:
                pass
        return True
    except PermissionError:
        return False


def restart_and_apply_staged_asar(asar_path=None):
    import subprocess
    import time
    asar = Path(asar_path) if asar_path else _default_paths()[1]
    exe = asar.parent.parent / "Antigravity.exe"
    subprocess.run(["taskkill", "/F", "/IM", "Antigravity.exe"], capture_output=True, check=False)
    applied = False
    for _ in range(50):
        if apply_staged_asar_if_present(asar):
            applied = True
            break
        if not asar.with_name("app.asar.bsl-staged").exists():
            applied = True
            break
        time.sleep(0.1)
    if exe.exists():
        subprocess.Popen(["explorer.exe", str(exe)])
    return {
        "ok": applied,
        "applied": applied,
        "desktop_status": integration_status(asar_path=asar)["desktop_status"],
        "message": "Antigravity restarted with runtime patch applied." if applied else "Could not swap staged archive; ensure Antigravity is closed.",
    }


def integration_status(ide_paths=None, asar_path=None):
    default_ide, default_asar = _default_paths()
    paths = default_ide if ide_paths is None else [Path(p) for p in ide_paths]
    asar = Path(asar_path) if asar_path else default_asar
    ide = []
    for path in paths:
        text = path.read_text(encoding="utf-8-sig") if path.exists() else ""
        ide.append({"path": str(path), "installed": path.parent.exists(), "patched": bool(re.search(r'"jetski\.cloudCodeUrl"\s*:\s*"http://127\.0\.0\.1:', _jsonc_mask(text)))})
    desktop = "not_installed"
    if asar.exists():
        try:
            _, files = unpack_asar(asar.read_bytes())
            desktop = "patched" if all(MARKER.encode() in files.get(n, b"") for n in ("dist/main.js", "dist/languageServer.js")) else "original"
        except (ValueError, KeyError):
            desktop = "invalid_archive"
    if asar.with_name("app.asar.bsl-staged").exists():
        desktop = "staged_restart_required"
    return {"ide": ide, "desktop_status": desktop, "desktop_path": str(asar)}


def patch_antigravity(port=6969, restore=False, ide_paths=None, asar_path=None):
    default_ide, default_asar = _default_paths()
    paths = default_ide if ide_paths is None else [Path(p) for p in ide_paths]
    asar = Path(asar_path) if asar_path else default_asar
    results = []
    for path in paths:
        if path.parent.exists() or ide_paths is not None:
            results.append({"path": str(path), "status": patch_ide_settings(path, port, restore)})
    desktop = "not_installed"
    if asar.exists():
        backup = _original_backup(asar)
        data = backup.read_bytes()
        if not restore:
            header, files = unpack_asar(data)
            url = f"http://127.0.0.1:{int(port)}"
            files["dist/main.js"] = _patch_main(files["dist/main.js"].decode("utf-8"), url).encode("utf-8")
            files["dist/languageServer.js"] = _patch_language_server(files["dist/languageServer.js"].decode("utf-8"), url).encode("utf-8")
            data = pack_asar(header, files)
        try:
            _atomic_write(asar, data)
            asar.with_name("app.asar.bsl-staged").unlink(missing_ok=True)
            desktop = "restored" if restore else "patched"
        except PermissionError:
            _atomic_write(asar.with_name("app.asar.bsl-staged"), data)
            desktop = "staged_restart_required"
    return {"ok": True, "ide": results, "desktop_status": desktop,
            "message": "Close Antigravity and apply the staged archive, then restart." if desktop == "staged_restart_required" else "Original restored." if restore else "Runtime patch applied. Restart Antigravity."}


def restore_antigravity(**kwargs):
    return patch_antigravity(restore=True, **kwargs)
