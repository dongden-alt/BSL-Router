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


def _router_root_dir():
    return str(Path(__file__).resolve().parents[1])


def _build_resilient_bridge_js(url):
    target_json = json.dumps(url)
    health_json = json.dumps(url + "/health")
    root_json = json.dumps(_router_root_dir())
    return (
        "try { if (!globalThis.__bslBridgeServer) { "
        "const http = require('http'), https = require('https'), fs = require('fs'), path = require('path'), cp = require('child_process'); "
        f"const bslTarget = new URL({target_json}); "
        "let lastSpawnMs = 0; "
        "const maybeAutoStart = () => { try { "
        "const now = Date.now(); if (now - lastSpawnMs < 30000) return; lastSpawnMs = now; "
        f"const root = process.env.BSL_ROUTER_ROOT || {root_json}; "
        "if (fs.existsSync(path.join(root, '.brain', 'logs', 'manual-stop.flag'))) return; "
        "const ps1 = path.join(root, 'scripts', 'bslrouter.ps1'); "
        "if (fs.existsSync(ps1)) { cp.spawn('powershell.exe', ['-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden', '-ExecutionPolicy', 'Bypass', '-File', ps1, 'start'], { detached: true, stdio: 'ignore', windowsHide: true }).unref(); } "
        "} catch {} }; "
        f"const checkHealth = () => {{ try {{ const req = http.get({health_json}, {{ timeout: 800 }}, res => {{ globalThis.__bslRouterAlive = (res.statusCode === 200); res.resume(); }}); req.on('error', () => {{ const was = globalThis.__bslRouterAlive; globalThis.__bslRouterAlive = false; if (was === undefined) maybeAutoStart(); }}); req.on('timeout', () => {{ req.destroy(); globalThis.__bslRouterAlive = false; }}); }} catch {{}} }}; "
        "checkHealth(); try { setInterval(checkHealth, 3000).unref(); } catch {} "
        "let qsCache = null, qsCacheTs = 0; "
        "const loadOfflineQuotaSaver = () => { try { const now = Date.now(); if (qsCache && now - qsCacheTs < 2000) return qsCache; qsCacheTs = now; const home = process.env.USERPROFILE || process.env.HOME || ''; const p = path.join(home, '.gemini', 'blacksand-studio', 'quota-saver-config.json'); if (fs.existsSync(p)) { qsCache = JSON.parse(fs.readFileSync(p, 'utf8')); return qsCache; } } catch {} return qsCache; }; "
        "const applyOfflineQuotaSaver = (rawBuf, pStr, isGen) => { try { if (!isGen || !rawBuf || !rawBuf.length) return { buf: rawBuf, offloaded: false, isHydrogen: false }; const qs = loadOfflineQuotaSaver(); if (!qs || qs.enabled === false || qs.native_hydrogen_offload === false) return { buf: rawBuf, offloaded: false, isHydrogen: false }; const obj = JSON.parse(rawBuf.toString('utf8')); if (!obj || typeof obj !== 'object') return { buf: rawBuf, offloaded: false, isHydrogen: false }; for (const k of Object.keys(obj)) { if (k.startsWith('_bsl_')) delete obj[k]; } const inner = (obj.request && typeof obj.request === 'object') ? obj.request : obj; const rawM = String(obj.model || inner.model || obj.modelId || inner.modelId || '').trim(); const pMap = { 'MODEL_PLACEHOLDER_M427':'gemini-4-carbon-high','MODEL_PLACEHOLDER_M437':'gemini-4-carbon-medium','MODEL_PLACEHOLDER_M438':'gemini-4-carbon-low','MODEL_PLACEHOLDER_M417':'gemini-4-argon-high','MODEL_PLACEHOLDER_M418':'gemini-4-argon-medium','MODEL_PLACEHOLDER_M419':'gemini-4-argon-low','MODEL_PLACEHOLDER_M528':'gemini-4-hydrogen-xhigh','MODEL_PLACEHOLDER_M525':'gemini-4-hydrogen-high','MODEL_PLACEHOLDER_M526':'gemini-4-hydrogen-medium','MODEL_PLACEHOLDER_M527':'gemini-4-hydrogen-low' }; const mKey = rawM.replace(/^models\\//i, '').toUpperCase(); const normM = pMap[mKey] || rawM.replace(/^models\\//i, ''); const lowM = normM.toLowerCase(); const isGeminiHeavy = lowM.includes('gemini-4-carbon') || lowM.includes('gemini-4-argon'); const isHeavy = isGeminiHeavy || lowM.includes('claude-opus') || lowM.includes('claude-sonnet'); let targetM = normM, capBudget = 0, didOffload = false; if (isHeavy) { const contents = Array.isArray(inner.contents) ? inner.contents : (Array.isArray(obj.contents) ? obj.contents : []); let lastUser = '', allTxt = '', toolTurns = 0, lastToolErr = false, lastIsToolCont = false; for (let ci = 0; ci < contents.length; ci++) { const c = contents[ci]; if (!c || typeof c !== 'object') continue; const parts = Array.isArray(c.parts) ? c.parts : []; let uTxt = '', hasFr = false, hasUserTxt = false; for (const pt of parts) { if (!pt || typeof pt !== 'object') continue; if (pt.functionCall || pt.functionResponse) { toolTurns++; if (pt.functionResponse) { hasFr = true; if (ci === contents.length - 1) { const frS = JSON.stringify(pt.functionResponse).toLowerCase(); if (/traceback|exception|error:|failed|errno|exit code [1-9]|command exited with code [1-9]|\"is_error\":\\s*true/.test(frS)) lastToolErr = true; } } } if (typeof pt.text === 'string') { allTxt += ' ' + pt.text; if (!pt.thought && pt.text.trim()) { hasUserTxt = true; if (c.role === 'user') uTxt += ' ' + pt.text; } } } if (ci === contents.length - 1 && hasFr && !hasUserTxt) lastIsToolCont = true; if (c.role === 'user' && uTxt.trim()) lastUser = uTxt.trim(); } let isSubagent = false; if (qs.offload_subagents !== false) { const toolsArr = Array.isArray(inner.tools) ? inner.tools : []; let hasTools = false, hasSendMsg = false, hasInvokeSub = false; for (const tg of toolsArr) { const fds = tg && Array.isArray(tg.functionDeclarations) ? tg.functionDeclarations : []; for (const fd of fds) { if (!fd || !fd.name) continue; hasTools = true; if (fd.name === 'send_message') hasSendMsg = true; if (fd.name === 'invoke_subagent') hasInvokeSub = true; } } const siStr = inner.systemInstruction ? JSON.stringify(inner.systemInstruction).toLowerCase() : ''; if ((hasTools && hasSendMsg && !hasInvokeSub) || siStr.includes('research subagent with read-only tools')) isSubagent = true; } const cleanUser = lastUser.replace(/<(context|user_information|system-reminder|mcp_servers|skills|user_rules)[\\s\\S]*?<\\/\\1>/gi, '').trim(); const lowAll = allTxt.toLowerCase(), lowClean = cleanUser.toLowerCase(); const explicitHeavyLock = /\\b(use|run\\s+with|stay\\s+on)\\s+(gemini[- ]?4[- ]?)?(carbon|argon)\\b/i.test(cleanUser); const isCompaction = (lowAll.includes('<context_summary>') || lowAll.includes('summarize the conversation') || lowAll.includes('context compaction')) && !lastUser.includes('</CONTEXT_SUMMARY>'); const hasComplex = /architect|refactor|root cause|deep think|forensic|crucible|multi-file|security audit|concurrency|deadlock|stack trace|traceback|implement a full|design a complete/.test(lowClean) || cleanUser.includes('```'); if (!explicitHeavyLock && !lastToolErr) { if (isCompaction) { targetM = qs.fast_native_model || 'gemini-4-hydrogen-low'; capBudget = 1024; didOffload = true; } else if (isSubagent && !hasComplex && cleanUser.length <= 1400) { targetM = qs.routine_native_model || 'gemini-4-hydrogen-medium'; capBudget = 4096; didOffload = true; } else if (qs.offload_tool_followups === true && lastIsToolCont && isGeminiHeavy && toolTurns >= 2 && toolTurns <= 48) { targetM = qs.routine_native_model || 'gemini-4-hydrogen-medium'; capBudget = 4096; didOffload = true; } else if (!hasComplex && cleanUser.length > 0 && cleanUser.length <= 260 && toolTurns < 12) { if (cleanUser.length <= 90 && toolTurns < 4) { targetM = qs.fast_native_model || 'gemini-4-hydrogen-low'; capBudget = 1024; didOffload = true; } else { targetM = qs.routine_native_model || 'gemini-4-hydrogen-medium'; capBudget = 2048; didOffload = true; } } } } const isHyd = String(targetM).toLowerCase().includes('hydrogen'); if (targetM !== rawM || capBudget > 0) { if (obj.model !== undefined || !obj.request) obj.model = targetM; if (obj.request && typeof obj.request === 'object') { if (obj.request.model !== undefined) obj.request.model = targetM; if (capBudget > 0) { const gc = (obj.request.generationConfig && typeof obj.request.generationConfig === 'object') ? obj.request.generationConfig : (obj.request.generationConfig = {}); const tc = (gc.thinkingConfig && typeof gc.thinkingConfig === 'object') ? gc.thinkingConfig : (gc.thinkingConfig = {}); if (typeof tc.thinkingBudget === 'number' && tc.thinkingBudget > capBudget) tc.thinkingBudget = capBudget; } } return { buf: Buffer.from(JSON.stringify(obj), 'utf8'), offloaded: didOffload, isHydrogen: isHyd }; } return { buf: rawBuf, offloaded: false, isHydrogen: isHyd }; } catch {} return { buf: rawBuf, offloaded: false, isHydrogen: false }; }; "
        "const rewriteNativeModelBuf = (rawBuf, nextM) => { try { const o = JSON.parse(rawBuf.toString('utf8')); if (o && typeof o === 'object') { if (o.model !== undefined || !o.request) o.model = nextM; if (o.request && typeof o.request === 'object' && o.request.model !== undefined) o.request.model = nextM; return Buffer.from(JSON.stringify(o), 'utf8'); } } catch {} return rawBuf; }; "
        "const srv = http.createServer((req, res) => { "
        "res.on('error', () => {}); "
        "const chunks = []; req.on('data', c => chunks.push(c)); req.on('end', () => { "
        "const body = Buffer.concat(chunks); "
        "const pathStr = req.url || '/'; "
        "const isGenerate = pathStr.includes(':generateContent') || pathStr.includes(':streamGenerateContent') || pathStr.includes(':generateChat') || pathStr.includes(':streamGenerateChat') || pathStr.includes(':completeCode') || pathStr.includes(':tabChat'); "
        "let forwarded = false; let timedOut = false; "
        "const safeWriteHead = (code, hdrs) => { if (res.headersSent || res.writableEnded) return false; try { res.writeHead(code, hdrs); return true; } catch { return false; } }; "
        "const safeEndJson = (code, msg) => { try { if (!res.headersSent && !res.writableEnded) res.writeHead(code, { 'content-type': 'application/json' }); if (!res.writableEnded) res.end(JSON.stringify({ error: { code, message: msg, status: 'UNAVAILABLE' } })); } catch {} }; "
        "const forwardNative = () => { "
        "if (forwarded || res.headersSent || res.writableEnded) return; forwarded = true; "
        "const isGenLang = pathStr.startsWith('/v1beta') || pathStr.startsWith('/v1alpha'); "
        "const host = isGenLang ? 'generativelanguage.googleapis.com' : 'daily-cloudcode-pa.googleapis.com'; "
        "const qsRes = applyOfflineQuotaSaver(body, pathStr, isGenerate); "
        "const nativeFbChain = ['gemini-3-flash', 'gemini-2.5-flash']; "
        "const sendNativeAttempt = (curBuf, fbIdx) => { "
        "const hdrs = { ...req.headers, host }; delete hdrs['connection']; hdrs['content-length'] = String(curBuf.length); "
        "const uReq = https.request({ hostname: host, port: 443, path: pathStr, method: req.method, headers: hdrs, servername: host }, uRes => { try { "
        "uRes.on('error', () => { try { if (!res.writableEnded) res.end(); } catch {} }); "
        "if (!isGenLang && isGenerate && (qsRes.offloaded || qsRes.isHydrogen) && (uRes.statusCode === 429 || uRes.statusCode === 404) && fbIdx < nativeFbChain.length && !res.headersSent && !res.writableEnded) { uRes.resume(); return sendNativeAttempt(rewriteNativeModelBuf(curBuf, nativeFbChain[fbIdx]), fbIdx + 1); } "
        "if (!safeWriteHead(uRes.statusCode || 502, uRes.headers)) { uRes.resume(); return; } "
        "uRes.pipe(res); } catch {} }); "
        "uReq.on('error', err => { safeEndJson(502, err && err.message ? err.message : 'Upstream error'); }); "
        "uReq.end(curBuf); }; "
        "sendNativeAttempt(qsRes.buf, 0); }; "
        "if (globalThis.__bslRouterAlive === false) { maybeAutoStart(); return forwardNative(); } "
        "const lReq = http.request({ hostname: bslTarget.hostname, port: bslTarget.port, path: pathStr, method: req.method, headers: { ...req.headers, host: bslTarget.host }, timeout: isGenerate ? 180000 : 8000 }, lRes => { try { "
        "lRes.on('error', () => { try { if (!res.writableEnded) res.end(); } catch {} }); "
        "if (!isGenerate && (lRes.statusCode === 502 || lRes.statusCode === 503 || lRes.statusCode === 504)) { lRes.resume(); return forwardNative(); } "
        "globalThis.__bslRouterAlive = true; "
        "if (!safeWriteHead(lRes.statusCode || 200, lRes.headers)) { lRes.resume(); return; } "
        "lRes.pipe(res); } catch {} }); "
        "lReq.on('error', err => { if (res.headersSent || res.writableEnded) { try { if (!res.writableEnded) res.end(); } catch {} return; } if (timedOut && isGenerate) { safeEndJson(504, 'BSL Router request timed out'); return; } globalThis.__bslRouterAlive = false; maybeAutoStart(); forwardNative(); }); "
        "lReq.on('timeout', () => { timedOut = true; lReq.destroy(); }); "
        "lReq.end(body); }); }); "
        "srv.listen(0, '127.0.0.1', () => { try { globalThis.__bslBridgeUrl = 'http://127.0.0.1:' + srv.address().port; } catch {} }); "
        "srv.unref(); globalThis.__bslBridgeServer = srv; } } catch {} "
    )


def _patch_language_server(source, url):
    if MARKER in source:
        raise ValueError("Backup is already BSL-patched; refusing to use it as vendor source")
    pattern = r'((?:async\s+)?(?:function\s+startLanguageServer\s*\([^)]*\)|startLanguageServer\s*\([^)]*\))\s*\{)'
    target_json = json.dumps(url)
    health_json = json.dumps(url + "/health")
    root_json = json.dumps(_router_root_dir())
    resolver_iife = (
        "(function() { "
        "if (typeof options === 'object' && options && options.wsl) return undefined; "
        "if (globalThis.__bslBridgeUrl) return globalThis.__bslBridgeUrl; "
        f"const target = {target_json}; "
        "try { "
        "const cp = require('child_process'), fs = require('fs'), path = require('path'); "
        "const probe = (ms, waitErrMs) => { "
        f"const code = `const http=require('http');const fail=()=>setTimeout(()=>process.exit(1),${{waitErrMs||0}});const req=http.get({health_json},{{timeout:${{ms}}}},res=>{{process.exit(res.statusCode===200?0:1)}});req.on('error',fail);req.on('timeout',()=>{{req.destroy();fail()}});`; "
        "const r = cp.spawnSync(process.execPath, ['-e', code], { env: { ...process.env, ELECTRON_RUN_AS_NODE: '1' }, timeout: ms + (waitErrMs || 0) + 500, windowsHide: true }); "
        "return Boolean(r && r.status === 0); }; "
        "if (probe(600, 0)) { globalThis.__bslRouterAlive = true; return globalThis.__bslBridgeUrl || target; } "
        f"const root = process.env.BSL_ROUTER_ROOT || {root_json}; "
        "const ps1 = path.join(root, 'scripts', 'bslrouter.ps1'); "
        "if (fs.existsSync(ps1) && !fs.existsSync(path.join(root, '.brain', 'logs', 'manual-stop.flag'))) { "
        "try { cp.spawn('powershell.exe', ['-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden', '-ExecutionPolicy', 'Bypass', '-File', ps1, 'start'], { detached: true, stdio: 'ignore', windowsHide: true }).unref(); "
        "for (let i = 0; i < 7; i++) { if (probe(700, 450)) { globalThis.__bslRouterAlive = true; return globalThis.__bslBridgeUrl || target; } } } catch {} } "
        "if (globalThis.__bslBridgeUrl) return globalThis.__bslBridgeUrl; globalThis.__bslRouterAlive = false; return undefined; "
        "} catch {} return globalThis.__bslBridgeUrl || target; })()"
    )
    injection = MARKER + "\nconst __bslRouterUrl = " + resolver_iife + ";\n"
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
    # Guard logStream against unhandled ENOSPC error events in Electron main process
    source = re.sub(
        r"(const\s+logStream\s*=\s*fs\.createWriteStream\([^;]+\);)",
        r"\1 logStream.on('error', () => { try { logStreamEnded = true; } catch {} });",
        source,
        count=1,
    )
    source = re.sub(
        r"logStream\.write\(line\s*\+\s*'\\n'\);",
        r"try { logStream.write(line + '\\n'); } catch { logStreamEnded = true; }",
        source,
        count=1,
    )
    return source


def _patch_storage(source):
    if MARKER in source:
        raise ValueError("Backup storage.js is already BSL-patched")
    if "class StorageManager" not in source or "async updateItems(" not in source:
        return source
    replacement_methods = (
        MARKER + "\n"
        "    async getRawItems() {\n"
        "        const tryParse = async (p) => {\n"
        "            if (!(0, fs_1.existsSync)(p)) return null;\n"
        "            const content = await fs.readFile(p, 'utf-8');\n"
        "            if (!content || content.trim() === '') return null;\n"
        "            const parsed = JSON.parse(content);\n"
        "            return (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) ? parsed : null;\n"
        "        };\n"
        "        if (this._bslMemDirty && this._bslMemItems) { return { ...this._bslMemItems }; }\n"
        "        try {\n"
        "            const primary = await tryParse(this.storagePath);\n"
        "            if (primary) { this._bslMemItems = { ...primary }; return primary; }\n"
        "        } catch (e) {\n"
        "            console.error('Error reading primary storage items:', e);\n"
        "        }\n"
        "        try {\n"
        "            const backup = await tryParse(this.storagePath + '.bak');\n"
        "            if (backup) { this._bslMemItems = { ...backup }; return backup; }\n"
        "        } catch {}\n"
        "        return this._bslMemItems ? { ...this._bslMemItems } : {};\n"
        "    }\n"
        "    async getItems() {\n"
        "        const items = await this.getRawItems();\n"
        "        const merged = { ...items };\n"
        "        if (this.defaults) {\n"
        "            for (const [key, value] of this.defaults.entries()) {\n"
        "                if (merged[key] === undefined) {\n"
        "                    merged[key] = String(value);\n"
        "                }\n"
        "            }\n"
        "        }\n"
        "        return merged;\n"
        "    }\n"
        "    async updateItems(changes) {\n"
        "        const isEnospc = (err) => Boolean(err && (err.code === 'ENOSPC' || /ENOSPC|no space left on device/i.test(String(err.message || err))));\n"
        "        try {\n"
        "            const currentItems = await this.getRawItems();\n"
        "            for (const [key, value] of Object.entries(changes || {})) {\n"
        "                if (value === null) {\n"
        "                    delete currentItems[key];\n"
        "                } else {\n"
        "                    currentItems[key] = value;\n"
        "                }\n"
        "            }\n"
        "            this._bslMemItems = { ...currentItems };\n"
        "            const serialized = JSON.stringify(currentItems, null, 2);\n"
        "            const tmpPath = `${this.storagePath}.tmp.${process.pid}.${Date.now()}`;\n"
        "            const bakPath = `${this.storagePath}.bak`;\n"
        "            try {\n"
        "                const dir = path.dirname(this.storagePath);\n"
        "                if (!(0, fs_1.existsSync)(dir)) {\n"
        "                    await fs.mkdir(dir, { recursive: true });\n"
        "                }\n"
        "                await fs.writeFile(tmpPath, serialized, 'utf-8');\n"
        "                if ((0, fs_1.existsSync)(this.storagePath)) {\n"
        "                    try { await fs.copyFile(this.storagePath, bakPath); } catch {}\n"
        "                }\n"
        "                await fs.rename(tmpPath, this.storagePath);\n"
        "                this._bslMemDirty = false;\n"
        "            } catch (writeErr) {\n"
        "                try { if ((0, fs_1.existsSync)(tmpPath)) await fs.unlink(tmpPath); } catch {}\n"
        "                if (isEnospc(writeErr)) {\n"
        "                    this._bslMemDirty = true;\n"
        "                    console.warn('[BSL Storage] Disk full (ENOSPC) — kept storage update in memory without truncating app_storage.json');\n"
        "                } else {\n"
        "                    throw writeErr;\n"
        "                }\n"
        "            }\n"
        "            const windows = electron_1.BrowserWindow.getAllWindows();\n"
        "            for (const win of windows) {\n"
        "                try { if (!win.isDestroyed()) win.webContents.send('storage:changed', changes); } catch {}\n"
        "            }\n"
        "            this.emitter.emit('changed', changes);\n"
        "        } catch (e) {\n"
        "            console.error('Error updating storage items:', e);\n"
        "            if (!isEnospc(e)) throw e;\n"
        "        }\n"
        "    }\n"
    )
    patched, count = re.subn(
        r"    /\*\*\s*\n\s*\*\s*Gets raw items from the storage file\.[\s\S]*?async updateItems\(changes\)\s*\{[\s\S]*?\n    \}\n",
        lambda _m: replacement_methods,
        source,
        count=1,
    )
    if not count:
        raise ValueError("Unsupported storage.js: StorageManager methods pattern not found")
    return patched


def _build_enospc_guard_js():
    return (
        "try { if (!globalThis.__bslEnospcGuardInstalled) { globalThis.__bslEnospcGuardInstalled = true; "
        "const isEnospcErr = (err) => Boolean(err && (err.code === 'ENOSPC' || /ENOSPC|no space left on device/i.test(String(err.message || err)))); "
        "process.on('uncaughtException', (err) => { if (isEnospcErr(err)) { try { process.stderr.write('[BSL ENOSPC Guard] Suppressed fatal main-process ENOSPC exception: ' + (err.message || err) + '\\n'); } catch {} return; } if (process.listenerCount('uncaughtException') <= 1) { try { require('electron').dialog.showErrorBox('A JavaScript error occurred in the main process', err && err.stack ? err.stack : String(err)); } catch {} } }); "
        "process.on('unhandledRejection', (reason) => { if (isEnospcErr(reason)) { try { process.stderr.write('[BSL ENOSPC Guard] Suppressed unhandled ENOSPC rejection: ' + (reason.message || reason) + '\\n'); } catch {} } }); "
        "try { const elog = require('electron-log/main'); const ft = elog && (elog.default || elog).transports && (elog.default || elog).transports.file; if (ft && typeof ft.writeFn === 'function' && !ft.__bslWrapped) { const origW = ft.writeFn.bind(ft); ft.writeFn = (msg) => { try { return origW(msg); } catch (e) { if (!isEnospcErr(e)) throw e; } }; ft.__bslWrapped = true; } } catch {} "
        "} } catch {} "
    )


def _patch_main(source, url):
    if MARKER in source:
        raise ValueError("Backup is already BSL-patched")
    # Self-heal if a prior partial strip left an unclosed onBeforeRequest try-block in original.app.asar
    source = re.sub(
        r"(?:[A-Za-z_$][\w$]*\.)*app\.whenReady\(\)\.then\(\(\)\s*=>\s*\{\s*require\('electron'\)\.session\.defaultSession\.webRequest\.onBeforeRequest\(\(details,\s*callback\)\s*=>\s*\{\s*try\s*\{[\s\S]*?callback\(\{\}\);\s*\}\);\s*\}\);\s*\n?",
        "",
        source,
    )
    match = re.search(r'(?P<app>(?:[A-Za-z_$][\w$]*\.)*app)\s*\.whenReady\s*\(', source)
    if not match:
        raise ValueError("Unsupported main.js: app.whenReady not found")
    app_expression = match.group("app")
    enospc_js = _build_enospc_guard_js()
    bridge_js = _build_resilient_bridge_js(url)
    # Register before the application's ready handlers. Requiring Electron here
    # avoids relying on the bundler's local variable names for session.
    hook = (
        MARKER + "\n"
        + enospc_js
        + bridge_js
        + app_expression
        + ".whenReady().then(() => { require('electron').session.defaultSession.webRequest.onBeforeRequest((details, callback) => { try { const u = new URL(details.url); const lsPort = require('./languageServer').getLsPort(); if (['127.0.0.1', 'localhost'].includes(u.hostname) && (!lsPort || Number(u.port) === Number(lsPort))) { if (u.pathname.endsWith('LanguageServerService/SetCloudCodeURL')) return callback({cancel:true}); if (globalThis.__bslRouterAlive !== false && u.pathname.endsWith('LanguageServerService/GetAvailableModels')) return callback({redirectURL:"
        + json.dumps(url + "/GetAvailableModels?ls=")
        + " + encodeURIComponent(details.url)}); } } catch {} callback({}); }); });\n"
    )
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
            if "dist/storage.js" in files:
                files["dist/storage.js"] = _patch_storage(files["dist/storage.js"].decode("utf-8")).encode("utf-8")
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
