"""Isolated combo persistence, cross-reference migration, and frontend transactional save contracts."""
import asyncio
import copy
import json
import pathlib
import subprocess

import pytest
from starlette.requests import Request


def _request(payload: dict) -> Request:
    raw = json.dumps(payload).encode("utf-8")
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": raw, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/config",
            "headers": [(b"content-type", b"application/json")],
            "query_string": b"",
        },
        receive,
    )


def _isolate_config_endpoint(monkeypatch, tmp_path, initial_cfg: dict) -> dict:
    store = {"cfg": copy.deepcopy(initial_cfg)}
    monkeypatch.setattr("app.main.cs_get_config", lambda: copy.deepcopy(store["cfg"]))

    def fake_replace(new_cfg):
        cleaned = copy.deepcopy(new_cfg)
        for flag in ("_deleted_connection", "_deleted_provider", "_deleted_model", "_deleted_models", "_renamed_combo"):
            cleaned.pop(flag, None)
        store["cfg"] = cleaned

    monkeypatch.setattr("app.main._replace_runtime_config", fake_replace)
    monkeypatch.setattr("app.main._CLAUDE_SETTINGS_PATH", tmp_path / "claude-settings.json")
    monkeypatch.setattr("app.main._QUOTA_SAVER_PERSISTENT_PATH", tmp_path / "quota-saver.json")
    monkeypatch.setattr("app.main._STUDIO_RUNTIME_STATE_PATH", tmp_path / "nonexistent-runtime-state.json")
    return store


def test_reconcile_rename_and_delete_are_scoped():
    from app.main import _reconcile_combo_alias_changes

    old = {"combos": [{"alias": "GPT-Sol", "chain": ["x"]}, {"alias": "Keep", "chain": ["GPT-Sol"]}]}
    new = {
        "combos": [{"alias": "GPT-Sol-v2", "chain": ["GPT-Sol"]}, {"alias": "Keep", "chain": ["GPT-Sol"]}],
        "antigravity_integration": {
            "mappings": {"slot": "GPT-Sol"},
            "custom_models": [{"target": "GPT-Sol", "display_name": "GPT-Sol", "placeholder_index": 12}],
        },
        "claude_code_integration": {
            "default_model": "GPT-Sol",
            "tier_slots": {"opus": "GPT-Sol"},
            "custom_models": [{"target": "GPT-Sol", "display_name": "GPT-Sol", "alias_id": "claude-bsl-1"}],
        },
    }
    new["_renamed_combo"] = {"from": "GPT-Sol", "to": "GPT-Sol-v2"}
    _reconcile_combo_alias_changes(old, new)
    assert new["antigravity_integration"]["mappings"]["slot"] == "GPT-Sol-v2"
    assert new["antigravity_integration"]["custom_models"][0]["placeholder_index"] == 12
    assert new["claude_code_integration"]["default_model"] == "GPT-Sol-v2"
    assert new["claude_code_integration"]["tier_slots"]["opus"] == "GPT-Sol-v2"
    assert new["combos"][0]["chain"] == ["GPT-Sol-v2"]


def test_spaces_are_valid_and_unknown_targets_remain_strict():
    from app.main import _is_known_antigravity_mapping_target

    cfg = {"combos": [{"alias": "GPT Sol Fast", "chain": ["x"]}], "providers": {}}
    assert _is_known_antigravity_mapping_target(cfg, "GPT Sol Fast")
    assert not _is_known_antigravity_mapping_target(cfg, "bogus-nonexistent")


def test_update_config_rename_migrates_references_and_second_save_succeeds(monkeypatch, tmp_path):
    from app.main import _ANTIGRAVITY_INTEGRATION_SLOT_SET, update_config

    valid_slot = sorted(_ANTIGRAVITY_INTEGRATION_SLOT_SET)[0]
    initial = {
        "providers": {
            "openai": {
                "enabled": True,
                "models": [{"id": "gpt-5.4", "enabled": True}],
                "connections": [],
            }
        },
        "combos": [
            {"alias": "GPT-Sol", "chain": ["openai/gpt-5.4"], "strategy": "fallback"},
            {"alias": "Coder-Chain", "chain": ["GPT-Sol", "openai/gpt-5.4"], "strategy": "fallback"},
        ],
        "antigravity_integration": {
            "enabled": True,
            "mappings": {valid_slot: "GPT-Sol"},
            "custom_models": [
                {"target": "GPT-Sol", "display_name": "GPT-Sol", "enabled": True, "placeholder_index": 412}
            ],
            "quota_saver": {
                "enabled": True,
                "fallback_models": [{"target": "GPT-Sol", "display_name": "GPT-Sol", "enabled": True}],
            },
        },
        "claude_code_integration": {
            "enabled": True,
            "replace_builtin": True,
            "default_model": "GPT-Sol",
            "tier_slots": {"opus": "GPT-Sol", "sonnet": "Coder-Chain", "haiku": ""},
            "custom_models": [
                {"target": "GPT-Sol", "display_name": "GPT-Sol", "alias_id": "claude-bsl-7", "enabled": True}
            ],
        },
    }
    store = _isolate_config_endpoint(monkeypatch, tmp_path, initial)

    # First save: UI renames GPT-Sol -> GPT-Sol-v2 while still holding old integration refs in payload
    first_payload = copy.deepcopy(initial)
    first_payload["combos"][0]["alias"] = "GPT-Sol-v2"
    first_payload["_renamed_combo"] = {"from": "GPT-Sol", "to": "GPT-Sol-v2"}

    res1 = asyncio.run(update_config(_request(first_payload)))
    assert res1.status_code == 200, res1.body.decode("utf-8")
    body1 = json.loads(res1.body)
    assert body1["status"] == "success"
    assert body1["antigravity_integration"]["mappings"][valid_slot] == "GPT-Sol-v2"
    assert body1["antigravity_integration"]["custom_models"][0]["target"] == "GPT-Sol-v2"
    assert body1["antigravity_integration"]["custom_models"][0]["placeholder_index"] == 412
    assert body1["claude_code_integration"]["default_model"] == "GPT-Sol-v2"
    assert body1["claude_code_integration"]["tier_slots"]["opus"] == "GPT-Sol-v2"
    assert body1["claude_code_integration"]["custom_models"][0]["target"] == "GPT-Sol-v2"
    assert body1["claude_code_integration"]["custom_models"][0]["alias_id"] == "claude-bsl-7"
    assert body1["combos"][1]["chain"][0] == "GPT-Sol-v2"

    # Second consecutive save: UI applies returned sections and updates strategy on Coder-Chain
    second_payload = copy.deepcopy(first_payload)
    second_payload.pop("_renamed_combo", None)
    second_payload["combos"] = body1["combos"]
    second_payload["antigravity_integration"] = body1["antigravity_integration"]
    second_payload["claude_code_integration"] = body1["claude_code_integration"]
    second_payload["combos"][1]["strategy"] = "round_robin"

    res2 = asyncio.run(update_config(_request(second_payload)))
    assert res2.status_code == 200, res2.body.decode("utf-8")
    assert store["cfg"]["combos"][1]["strategy"] == "round_robin"


def test_update_config_spaces_symbols_delete_cleanup_and_strict_bogus_400(monkeypatch, tmp_path):
    from app.main import update_config

    initial = {
        "providers": {"openai": {"enabled": True, "models": [{"id": "gpt-5.4", "enabled": True}], "connections": []}},
        "combos": [
            {"alias": "Team's GPT (Fast)", "chain": ["openai/gpt-5.4"], "strategy": "fallback"},
            {"alias": "Fallback-Combo", "chain": ["Team's GPT (Fast)", "openai/gpt-5.4"], "strategy": "fallback"},
        ],
        "antigravity_integration": {
            "enabled": True,
            "mappings": {},
            "custom_models": [{"target": "Team's GPT (Fast)", "display_name": "Team's GPT (Fast)", "enabled": True}],
        },
        "claude_code_integration": {
            "enabled": True,
            "replace_builtin": True,
            "default_model": "Team's GPT (Fast)",
            "tier_slots": {"opus": "Team's GPT (Fast)", "sonnet": "", "haiku": ""},
            "custom_models": [{"target": "Team's GPT (Fast)", "display_name": "Team's GPT (Fast)", "enabled": True}],
        },
    }
    store = _isolate_config_endpoint(monkeypatch, tmp_path, initial)

    # Round-trip combo with spaces & symbols succeeds
    ok_res = asyncio.run(update_config(_request(copy.deepcopy(initial))))
    assert ok_res.status_code == 200, ok_res.body.decode("utf-8")

    # Deleting referenced combo prunes custom_models and clears tier_slots/default_model cleanly
    del_payload = copy.deepcopy(store["cfg"])
    del_payload["combos"] = [del_payload["combos"][1]]
    del_res = asyncio.run(update_config(_request(del_payload)))
    assert del_res.status_code == 200, del_res.body.decode("utf-8")
    del_body = json.loads(del_res.body)
    assert del_body["antigravity_integration"]["custom_models"] == []
    assert del_body["claude_code_integration"]["custom_models"] == []
    assert del_body["claude_code_integration"]["default_model"] == ""
    assert del_body["claude_code_integration"]["tier_slots"]["opus"] == ""
    assert del_body["combos"][0]["chain"] == ["openai/gpt-5.4"]

    # D4 Negative Canary: brand-new bogus custom_models target MUST still be strictly rejected with 400
    bogus_payload = copy.deepcopy(store["cfg"])
    bogus_payload["antigravity_integration"]["custom_models"] = [
        {"target": "bogus-nonexistent-combo", "display_name": "Bogus", "enabled": True}
    ]
    bogus_res = asyncio.run(update_config(_request(bogus_payload)))
    assert bogus_res.status_code == 400
    assert "Unknown custom model target" in bogus_res.body.decode("utf-8")


def test_app_js_transactional_combo_saves_and_xss_escape_in_node_vm():
    app_js_path = pathlib.Path(__file__).parents[1] / "static" / "app.js"
    node_script = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

const src = fs.readFileSync(process.argv[1], 'utf8');

function extractSlice(startMarker, endMarker) {
  const s = src.indexOf(startMarker);
  const e = src.indexOf(endMarker, s);
  if (s < 0 || e < 0) throw new Error('Missing marker: ' + startMarker + ' -> ' + endMarker);
  return src.slice(s, e);
}

const saveBlock = extractSlice('let _autoSaveTimer = null;', 'function scheduleAutoSave()');
const escBlock = extractSlice('function agEsc(v){', '\nfunction ');
const comboTabBlock = extractSlice('function renderCombosTab() {', 'function showToast(message, isError)');

const elements = {
  'combo-modal': { style: { display: 'none' } },
  'combo-modal-idx': { value: '-1' },
  'combo-modal-title': { innerText: '' },
  'combo-modal-alias': { value: '' },
  'combo-modal-models-list': { innerHTML: '' },
  'main-content': { innerHTML: '' },
};

const toasts = [];
let fetchHandler = async () => ({ ok: true, status: 200, json: async () => ({ status: 'success' }) });
let clipboardText = null;

let renderCount = 0;
const sandbox = {
  window: {},
  document: {
    getElementById: (id) => elements[id] || null,
    body: { insertAdjacentHTML: () => {} },
  },
  navigator: {
    clipboard: { writeText: async (t) => { clipboardText = t; } },
  },
  confirm: () => true,
  fetch: (...args) => fetchHandler(...args),
  showToast: (msg, isErr) => { toasts.push({ msg, isErr: !!isErr }); },
  _setSaveIndicator: () => {},
  renderActiveTab: () => { renderCount += 1; },
  getThinkingSpec: () => null,
  isProviderSelectable: () => true,
  _bslSelectableModels: () => [],
  _isBlacksandProvider: () => false,
  SVGS: { blacksand: '' },
  globalConfig: {
    combos: [
      { alias: 'GPT-Sol', chain: ['openai/gpt-5.4'], strategy: 'fallback' },
      { alias: "Team's <Fast> \"A\"", chain: ['openai/gpt-5.4'], strategy: 'fallback' },
    ],
    providers: { openai: { name: 'OpenAI', models: [{ id: 'gpt-5.4', enabled: true }] } },
    antigravity_integration: { custom_models: [{ target: 'GPT-Sol' }] },
  },
  JSON, Promise, Error, Object, Array, String, Number, Math, RegExp, parseInt, console, setTimeout, clearTimeout,
};
sandbox.window = sandbox;

vm.createContext(sandbox);
vm.runInContext(escBlock + '\n' + saveBlock + '\n' + comboTabBlock, sandbox);

(async () => {
  // 1. Failed POST /api/config (400) keeps modal open, draft intact, globalConfig untouched, error toast only
  sandbox.openComboModal(0);
  elements['combo-modal-alias'].value = 'GPT-Sol-Renamed';
  fetchHandler = async () => ({
    ok: false,
    status: 400,
    statusText: 'Bad Request',
    json: async () => ({ error: 'Unknown custom model target' }),
  });
  toasts.length = 0;
  const okFail = await sandbox.saveComboModal();
  assert.strictEqual(okFail, false);
  assert.strictEqual(elements['combo-modal'].style.display, 'flex');
  assert.strictEqual(elements['combo-modal-alias'].value, 'GPT-Sol-Renamed');
  assert.strictEqual(sandbox.globalConfig.combos[0].alias, 'GPT-Sol');
  assert.strictEqual(toasts.length, 1);
  assert.strictEqual(toasts[0].isErr, true);
  assert(toasts[0].msg.includes('Unknown custom model target'));

  // 2. Slash in combo name rejected before fetch
  elements['combo-modal-alias'].value = 'bad/name';
  toasts.length = 0;
  await sandbox.saveComboModal();
  assert.strictEqual(toasts[0].isErr, true);
  assert(toasts[0].msg.includes('/'));

  // 3. Successful rename sends _renamed_combo, syncs returned integration sections, closes modal, clears one-shot flag
  elements['combo-modal-alias'].value = 'GPT Sol v2';
  let postedBody = null;
  fetchHandler = async (_url, opts) => {
    postedBody = JSON.parse(opts.body);
    return {
      ok: true,
      status: 200,
      json: async () => ({
        status: 'success',
        combos: postedBody.combos,
        antigravity_integration: { custom_models: [{ target: 'GPT Sol v2' }] },
        claude_code_integration: { tier_slots: { opus: 'GPT Sol v2' } },
      }),
    };
  };
  toasts.length = 0;
  const okSuccess = await sandbox.saveComboModal();
  assert.strictEqual(okSuccess, true);
  assert.deepStrictEqual(postedBody._renamed_combo, { from: 'GPT-Sol', to: 'GPT Sol v2' });
  assert.strictEqual(elements['combo-modal'].style.display, 'none');
  assert.strictEqual(sandbox.globalConfig.combos[0].alias, 'GPT Sol v2');
  assert.strictEqual(sandbox.globalConfig.antigravity_integration.custom_models[0].target, 'GPT Sol v2');
  assert.strictEqual(sandbox.globalConfig._renamed_combo, undefined);

  // 4b. Failed deleteCombo and updateComboStrategy keep globalConfig.combos intact & re-render on failure
  fetchHandler = async () => ({
    ok: false,
    status: 400,
    statusText: 'Bad Request',
    json: async () => ({ error: 'Referenced elsewhere' }),
  });
  const countBeforeDel = sandbox.globalConfig.combos.length;
  const delFail = await sandbox.deleteCombo(0);
  assert.strictEqual(delFail, false);
  assert.strictEqual(sandbox.globalConfig.combos.length, countBeforeDel);
  renderCount = 0;
  const stratFail = await sandbox.updateComboStrategy(0, 'round_robin');
  assert.strictEqual(stratFail, false);
  assert.strictEqual(sandbox.globalConfig.combos[0].strategy, 'fallback');
  assert.strictEqual(renderCount, 1);

  // 5. Queued concurrent saveConfig calls resolve strictly in order after each write completes
  const order = [];
  let releaseFirst;
  const firstGate = new Promise((r) => { releaseFirst = r; });
  let callNum = 0;
  fetchHandler = async (_url, opts) => {
    const n = ++callNum;
    if (n === 1) await firstGate;
    order.push('fetch-done-' + n);
    return { ok: true, status: 200, json: async () => ({ status: 'success', combos: JSON.parse(opts.body).combos }) };
  };
  const p1 = sandbox.saveConfig({ combos: [{ alias: 'A', chain: ['x'] }] }).then((v) => { order.push('p1-' + v); });
  const p2 = sandbox.saveConfig({ combos: [{ alias: 'B', chain: ['x'] }] }).then((v) => { order.push('p2-' + v); });
  await new Promise((r) => setTimeout(r, 15));
  assert.deepStrictEqual(order, []);
  releaseFirst();
  await Promise.all([p1, p2]);
  assert.deepStrictEqual(order, ['fetch-done-1', 'p1-true', 'fetch-done-2', 'p2-true']);
  assert.strictEqual(sandbox.globalConfig.combos[0].alias, 'B');

  // 5. XSS & quote escaping in renderCombosTab + index-based copyComboAlias
  sandbox.globalConfig.combos = [
    { alias: '<img src=x onerror=alert(1)> & "quote\'s"', chain: ['openai/gpt-5.4'], strategy: 'fallback' },
  ];
  const html = sandbox.renderCombosTab();
  assert(!html.includes('<img src=x'), 'Unescaped <img> tag leaked into renderCombosTab HTML');
  assert(html.includes('&lt;img src=x onerror=alert(1)&gt;'));
  assert(html.includes('onclick="copyComboAlias(0)"'));
  sandbox.copyComboAlias(0);
  await new Promise((r) => setTimeout(r, 5));
  assert.strictEqual(clipboardText, '<img src=x onerror=alert(1)> & "quote\'s"');
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
    proc = subprocess.run(
        ["node", "-e", node_script, str(app_js_path)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert proc.returncode == 0, f"Node VM combo test failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
