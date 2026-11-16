"""Deterministic unit & Node runtime tests for antigravity_patcher ENOSPC + Quota-Saver hardening."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from app.antigravity_patcher import (
    MARKER,
    _patch_language_server,
    _patch_main,
    _patch_storage,
    pack_asar,
    unpack_asar,
)


SAMPLE_STORAGE_JS = """"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.StorageManager = void 0;
const fs = require("fs/promises");
const fs_1 = require("fs");
const path = require("path");
const electron_1 = { BrowserWindow: { getAllWindows: () => [] } };
const events_1 = require("events");
class StorageManager {
    constructor(storagePath, defaults) {
        this.storagePath = storagePath;
        this.defaults = defaults;
        this.emitter = new events_1.EventEmitter();
    }
    /**
     * Gets raw items from the storage file.
     */
    async getRawItems() {
        try {
            if (!(0, fs_1.existsSync)(this.storagePath)) {
                return {};
            }
            const content = await fs.readFile(this.storagePath, 'utf-8');
            if (!content || content.trim() === '') {
                return {};
            }
            return JSON.parse(content);
        }
        catch (e) {
            console.error('Error reading storage items:', e);
            return {};
        }
    }
    async getItems() {
        const items = await this.getRawItems();
        return { ...items };
    }
    async updateItems(changes) {
        try {
            const currentItems = await this.getRawItems();
            for (const [key, value] of Object.entries(changes)) {
                if (value === null) {
                    delete currentItems[key];
                }
                else {
                    currentItems[key] = value;
                }
            }
            const dir = path.dirname(this.storagePath);
            if (!(0, fs_1.existsSync)(dir)) {
                await fs.mkdir(dir, { recursive: true });
            }
            await fs.writeFile(this.storagePath, JSON.stringify(currentItems, null, 2), 'utf-8');
            const windows = electron_1.BrowserWindow.getAllWindows();
            for (const win of windows) {
                win.webContents.send('storage:changed', changes);
            }
            this.emitter.emit('changed', changes);
        }
        catch (e) {
            console.error('Error updating storage items:', e);
            throw e;
        }
    }
}
exports.StorageManager = StorageManager;
"""


def test_patch_storage_atomic_backup_and_enospc_survival(tmp_path):
    patched_js = _patch_storage(SAMPLE_STORAGE_JS)
    assert MARKER in patched_js
    mod_path = tmp_path / "storage.cjs"
    mod_path.write_text(patched_js, encoding="utf-8")

    # 1. Syntax check via node --check
    chk = subprocess.run(["node", "--check", str(mod_path)], capture_output=True, text=True)
    assert chk.returncode == 0, chk.stderr

    # 2. Runtime verification in Node: atomic write, .bak creation, ENOSPC non-truncation & in-memory retention, .bak recovery
    driver = tmp_path / "driver.cjs"
    storage_file = tmp_path / "app_storage.json"
    driver.write_text(
        f"""
const fs = require('fs');
const fsp = require('fs/promises');
const {{ StorageManager }} = require({json.dumps(str(mod_path))});

(async () => {{
  const storagePath = {json.dumps(str(storage_file))};
  const sm = new StorageManager(storagePath, new Map([['defaultKey', 'defVal']]));

  // Initial write + second write to generate .bak
  await sm.updateItems({{ alpha: '1', beta: '2', layout: 'intact' }});
  await sm.updateItems({{ gamma: '3' }});
  const onDiskBefore = JSON.parse(fs.readFileSync(storagePath, 'utf8'));
  if (onDiskBefore.alpha !== '1' || onDiskBefore.gamma !== '3') throw new Error('atomic write failed');
  if (!fs.existsSync(storagePath + '.bak')) throw new Error('.bak not created');

  // Simulate ENOSPC during fs.writeFile
  const origWriteFile = fsp.writeFile;
  fsp.writeFile = async (p, data, enc) => {{
    // Even if OS created an empty temp file before failing, primary file must NOT be touched!
    fs.writeFileSync(p, '');
    const err = new Error('ENOSPC: no space left on device, write');
    err.code = 'ENOSPC';
    throw err;
  }};
  let changedEmitted = false;
  sm.emitter.once('changed', () => {{ changedEmitted = true; }});
  // Must NOT throw on ENOSPC!
  await sm.updateItems({{ zoomLevel: '1.5' }});
  fsp.writeFile = origWriteFile;

  // Verify primary file on disk was NOT truncated and still has all 3 keys
  const onDiskAfterEnospc = JSON.parse(fs.readFileSync(storagePath, 'utf8'));
  if (onDiskAfterEnospc.alpha !== '1' || onDiskAfterEnospc.layout !== 'intact') {{
    throw new Error('app_storage.json was corrupted on ENOSPC!');
  }}
  // Verify temp file was cleaned up
  const leftovers = fs.readdirSync({json.dumps(str(tmp_path))}).filter(n => n.includes('.tmp.'));
  if (leftovers.length !== 0) throw new Error('temp files leaked: ' + leftovers.join(','));
  // Verify in-memory items still retained zoomLevel and emitted changed
  const memItems = await sm.getItems();
  if (memItems.zoomLevel !== '1.5' || !changedEmitted) {{
    throw new Error('in-memory fallback did not retain update during ENOSPC');
  }}

  // Simulate truncated 0-byte primary file on cold restart -> must recover from .bak
  fs.writeFileSync(storagePath, '');
  const smCold = new StorageManager(storagePath);
  const recovered = await smCold.getItems();
  if (recovered.alpha !== '1' || recovered.layout !== 'intact') {{
    throw new Error('.bak cold recovery failed: ' + JSON.stringify(recovered));
  }}
  console.log('STORAGE_ENOSPC_PASS');
}})().catch(err => {{ console.error(err); process.exit(1); }});
""",
        encoding="utf-8",
    )
    run = subprocess.run(["node", str(driver)], capture_output=True, text=True)
    assert run.returncode == 0, f"stdout={run.stdout}\nstderr={run.stderr}"
    assert "STORAGE_ENOSPC_PASS" in run.stdout


def test_patch_main_and_language_server_enospc_and_quota_saver(tmp_path):
    sample_main = """"use strict";
const electron_1 = require("electron");
electron_1.app.whenReady().then(() => {});
"""
    sample_ls = """"use strict";
function startLanguageServer(port, csrf, options = {}) {
    const logStream = fs.createWriteStream(getLsLogPath(), { flags: 'w' });
    const args = [
        '--api_server_url', 'https://generativelanguage.googleapis.com',
        '--cloud_code_endpoint', 'https://daily-cloudcode-pa.googleapis.com',
    ];
    let logStreamEnded = false;
    rl.on('line', (line) => {
        if (!logStreamEnded) {
            logStream.write(line + '\\n');
        }
    });
}
"""
    pm = _patch_main(sample_main, "http://127.0.0.1:6969")
    pls = _patch_language_server(sample_ls, "http://127.0.0.1:6969")

    assert "__bslEnospcGuardInstalled" in pm
    assert "offload_tool_followups" in pm
    assert "logStream.on('error'" in pls
    assert "try { logStream.write(line + '\\n'); } catch { logStreamEnded = true; }" in pls

    for idx, code in enumerate((pm, pls)):
        f = tmp_path / f"check_{idx}.cjs"
        f.write_text(code, encoding="utf-8")
        chk = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
        assert chk.returncode == 0, chk.stderr


@pytest.mark.skipif(
    not Path(os.environ.get("BSL_TEST_ORIGINAL_ASAR", "")).is_file(),
    reason="Branded base ASAR not present on machine",
)
def test_real_branded_asar_patch_roundtrip_syntax(tmp_path):
    orig = Path(os.environ["BSL_TEST_ORIGINAL_ASAR"])
    header, files = unpack_asar(orig.read_bytes())
    files["dist/main.js"] = _patch_main(files["dist/main.js"].decode("utf-8"), "http://127.0.0.1:6969").encode("utf-8")
    files["dist/languageServer.js"] = _patch_language_server(
        files["dist/languageServer.js"].decode("utf-8"), "http://127.0.0.1:6969"
    ).encode("utf-8")
    files["dist/storage.js"] = _patch_storage(files["dist/storage.js"].decode("utf-8")).encode("utf-8")

    repacked = pack_asar(header, files)
    _, unpacked_files = unpack_asar(repacked)
    for target in ("dist/main.js", "dist/languageServer.js", "dist/storage.js"):
        out_f = tmp_path / target.replace("/", "_")
        out_f.write_bytes(unpacked_files[target])
        chk = subprocess.run(["node", "--check", str(out_f)], capture_output=True, text=True)
        assert chk.returncode == 0, f"{target} failed syntax check: {chk.stderr}"
