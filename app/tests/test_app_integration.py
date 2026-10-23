"""App integration contracts, isolated from installed applications and upstreams."""
import asyncio
import copy
import hashlib
import json
from pathlib import Path

import httpx
import pytest
from starlette.requests import Request
from fastapi.responses import JSONResponse

import app.main as main
from app import antigravity_patcher as patcher


def config():
    cfg = {
        "server": {"port": 6969, "api_keys": ["test-key"]},
        "providers": {"fake": {"models": [{"id": "model"}]}},
        "combos": [{"alias": "coder-3", "chain": ["fake/model"]}],
        "antigravity_integration": {"enabled": True, "mappings": {}, "custom_models": [
            {"target": "coder-3", "display_name": "My Coder", "placeholder_index": 401},
            {"target": "fake/model", "enabled": False, "placeholder_index": 402},
        ]},
        "claude_code_integration": {"enabled": True, "default_model": "coder-3", "tier_slots": {"opus": "coder-3"},
                                    "custom_models": [{"target": "coder-3", "display_name": "My Coder"}]},
    }
    main._validate_antigravity_integration_config(cfg)
    main._validate_claude_code_integration_config(cfg)
    return cfg


def request(path, body=None, query=b"", headers=None, raw=None):
    payload = json.dumps(body or {}).encode() if raw is None else raw
    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}
    return Request({"type": "http", "method": "POST", "path": path, "query_string": query,
                    "scheme": "http", "server": ("127.0.0.1", 6969), "client": ("127.0.0.1", 1234),
                    "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]}, receive)


def test_config_shape_and_allocation():
    cfg = config()
    del cfg["antigravity_integration"]["custom_models"]
    main._validate_antigravity_integration_config(cfg)
    assert cfg["antigravity_integration"] == {"enabled": True, "mappings": {}}
    cfg["antigravity_integration"]["custom_models"] = [{"target": "coder-3", "placeholder_index": 417}, {"target": "fake/model", "placeholder_index": 417}, {"target": "coder-3"}]
    main._validate_antigravity_integration_config(cfg)
    models = cfg["antigravity_integration"]["custom_models"]
    assert len(models) == 2
    assert len({m["placeholder_index"] for m in models}) == 2
    assert all(m["placeholder_index"] not in main._AG_RESERVED_INDICES for m in models)
    assert models[1]["slug"] == "custom-fake-model"
    before = copy.deepcopy(models)
    main._validate_antigravity_integration_config(cfg)
    assert cfg["antigravity_integration"]["custom_models"] == before


@pytest.mark.parametrize("field,value", [("custom_models", {}), ("custom_models", [{"target": "missing"}]), ("custom_models", [{"target": "coder-3", "enabled": "yes"}])])
def test_invalid_custom_models(field, value):
    cfg = config()
    cfg["antigravity_integration"][field] = value
    with pytest.raises(ValueError):
        main._validate_antigravity_integration_config(cfg)


def test_load_config_validates_and_keeps_custom_models_clean(monkeypatch, tmp_path):
    """load_config() validates both integration blocks but NEVER auto-imports a
    machine-local ~/.gemini/antigravity/custom_models.json (2026-10-22: the
    public repo must ship a clean empty custom_models list)."""
    directory = tmp_path / ".gemini/antigravity"
    directory.mkdir(parents=True)
    (directory / "custom_models.json").write_text(json.dumps({"models": [
        {"name": "legacy", "externalModelName": "coder-3", "displayName": "Old", "enabled": True}]}))
    monkeypatch.setattr(main.os.path, "expanduser", lambda _: str(tmp_path))
    assert not hasattr(main, "_migrate_legacy_ag_custom_models"),         "the legacy auto-import helper must stay removed"
    cfg = config()
    for block in ("antigravity_integration", "claude_code_integration"):
        del cfg[block]["custom_models"]
    calls = []
    monkeypatch.setattr(main, "init_config", lambda _: None)
    monkeypatch.setattr(main, "cs_get_config", lambda: cfg)
    monkeypatch.setattr(main, "init_breaker", lambda _: None)
    for name in ("_validate_antigravity_integration_config", "_validate_claude_code_integration_config"):
        validate = getattr(main, name)
        def track(data, validate=validate, name=name):
            calls.append(name)
            return validate(data)
        monkeypatch.setattr(main, name, track)
    monkeypatch.setattr(main, "_replace_runtime_config", lambda data: calls.append(("persist", copy.deepcopy(data))))
    monkeypatch.setattr(main, "replace_config", lambda data: calls.append(("replace", copy.deepcopy(data))))
    main.load_config()
    assert calls == ["_validate_antigravity_integration_config", "_validate_claude_code_integration_config",
                     ("replace", cfg)]
    # Both blocks were validated, and neither one gained auto-imported entries
    # from the machine-local legacy files planted above.
    assert not cfg["antigravity_integration"].get("custom_models"),         "legacy ~/.gemini/antigravity/custom_models.json must never be imported"
    assert not cfg["claude_code_integration"].get("custom_models")


def test_get_available_models_is_public(monkeypatch):
    assert "/GetAvailableModels" in main._PUBLIC_PATHS
    assert main._is_public_path("/GetAvailableModels")
    monkeypatch.setattr(main, "_is_admin_auth_enabled", lambda: True)
    calls = []
    async def forward(req):
        calls.append(req.url.path)
        return JSONResponse({"ok": True})
    result = asyncio.run(main.admin_auth_middleware(request("/GetAvailableModels"), forward))
    assert result.status_code == 200
    assert calls == ["/GetAvailableModels"]


@pytest.mark.parametrize("key,container", [("models", {}), ("models", []), ("availableModels", {}), ("available_models", [])])
def test_catalog_containers(key, container):
    original = {key: container, "agentModelSorts": [{"groups": [{"modelIds": ["native"]}]}]}
    before = copy.deepcopy(original)
    models = config()["antigravity_integration"]["custom_models"]
    result = main._merge_antigravity_custom_models_catalog(original, models)
    assert original == before
    entry = result[key][0] if isinstance(container, list) else result[key]["custom-coder-3"]
    assert entry["displayName"] == "My Coder"
    assert entry["model"] == "MODEL_PLACEHOLDER_M401"
    assert entry["supportsImages"] is True
    assert "custom-fake-model" not in str(result)
    assert result["agentModelSorts"][0]["groups"][0]["modelIds"] == ["native", "custom-coder-3"]
    assert main._merge_antigravity_custom_models_catalog(result, models) == result


def test_catalog_route_without_credentials_and_delegation(monkeypatch):
    cfg = config()
    monkeypatch.setattr(main, "cs_get_config", lambda: cfg)
    result = asyncio.run(main.antigravity_fetch_available_models(request("/v1internal:fetchAvailableModels")))
    assert json.loads(result.body)["models"]["custom-coder-3"]["model"] == "MODEL_PLACEHOLDER_M401"
    cfg["antigravity_integration"]["custom_models"] = []
    calls = []
    async def forward(req, operation):
        calls.append(operation)
        return JSONResponse({"native": True})
    monkeypatch.setattr(main, "_forward_antigravity_ccpa_control", forward)
    result = asyncio.run(main.antigravity_fetch_available_models(request("/v1internal:fetchAvailableModels")))
    assert calls == ["fetchAvailableModels"]
    assert json.loads(result.body) == {"native": True}


def frame(body, flags=0):
    return bytes([flags]) + len(body).to_bytes(4, "big") + body


def test_grpc_injection_preserves_sample_fields_and_trailer():
    entry = main._proto_string(1, "models/native") + main._proto_string(2, "Native") + main._proto_varint(24) + main._proto_varint(1)
    body = main._proto_varint(7 << 3 | 2) + main._proto_varint(len(entry)) + entry
    trailer = frame(b"grpc-status: 0\r\n", 128)
    original = frame(body) + trailer
    models = config()["antigravity_integration"]["custom_models"]
    result = main._inject_custom_models_into_grpc_web_proto(original, models)
    assert result.endswith(trailer)
    size = int.from_bytes(result[1:5], "big")
    fields = main._proto_fields(result[5:5 + size])
    assert [f[0] for f in fields] == [7, 7]
    custom = main._proto_fields(fields[1][2])
    assert custom[0][2] == b"models/MODEL_PLACEHOLDER_M401"
    assert custom[1][2] == b"My Coder"
    assert custom[2][2] == 1
    assert main._inject_custom_models_into_grpc_web_proto(result, models) == result
    assert main._inject_custom_models_into_grpc_web_proto(b"bad", models) == b"bad"
    assert main._inject_custom_models_into_grpc_web_proto(frame(b"compressed", 1), models) == frame(b"compressed", 1)


@pytest.mark.parametrize("url", ["https://evil.invalid/LanguageServerService/GetAvailableModels", "file:///LanguageServerService/GetAvailableModels", "http://localhost/other", "http://localhost@evil.invalid/LanguageServerService/GetAvailableModels"])
def test_grpc_proxy_rejects_remote_targets(url):
    from urllib.parse import urlencode
    result = asyncio.run(main.antigravity_get_available_models(request("/GetAvailableModels", query=urlencode({"ls": url}).encode())))
    assert result.status_code == 400


@pytest.mark.parametrize("selector", ["MODEL_PLACEHOLDER_M401", "models/MODEL_PLACEHOLDER_M401", "models/custom/custom-coder-3", "custom-coder-3", "My Coder", "coder-3", 1401, "1401"])
def test_custom_selectors(selector):
    assert main._resolve_antigravity_custom_model(config(), selector) == "coder-3"
    assert main._resolve_antigravity_custom_model(config(), "MODEL_PLACEHOLDER_M402") is None


@pytest.mark.parametrize("location,selector", [("model", "MODEL_PLACEHOLDER_M401"), ("modelId", 1401), ("model_id", "custom-coder-3"), ("request.model", "MODEL_PLACEHOLDER_M401"), ("request.modelId", "My Coder")])
def test_generate_custom_combo_resolution(monkeypatch, location, selector):
    cfg = config()
    monkeypatch.setattr(main, "cs_get_config", lambda: cfg)
    captured = {}
    async def dispatch(body, **kwargs):
        captured.update(body=body, kwargs=kwargs)
        return JSONResponse({"ok": True})
    monkeypatch.setattr(main, "_process_chat_completion", dispatch)
    body = {"request": {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]}}
    if location.startswith("request."):
        body["request"][location.split(".")[1]] = selector
    else:
        body[location] = selector
    result = asyncio.run(main.antigravity_generate(request("/v1internal:generateContent", body)))
    assert result.status_code == 200
    assert captured["body"]["model"] == "coder-3"
    assert captured["kwargs"]["client_wants_gemini"] is True


def make_asar():
    files = {
        "dist/main.js": b"const {app} = require('electron'); app.whenReady().then(() => {});",
        "dist/languageServer.js": b"function startLanguageServer(options) { const args = ['--api_server_url', 'https://api.example', '--cloud_code_endpoint', 'https://cloud.example']; return args; }",
        "other.txt": b"untouched",
    }
    header = {"files": {"dist": {"files": {"main.js": {"size": 0, "offset": "0"}, "languageServer.js": {"size": 0, "offset": "0"}}}, "other.txt": {"size": 0, "offset": "0"}}}
    return patcher.pack_asar(header, files)


def test_asar_and_ide_round_trip(tmp_path):
    ide = tmp_path / "settings.json"
    ide.write_text('{\n // keep comment\n "editor.fontSize": 14,\n}\n')
    asar = tmp_path / "resources/app.asar"
    asar.parent.mkdir()
    original = make_asar()
    asar.write_bytes(original)
    result = patcher.patch_antigravity(7777, ide_paths=[ide], asar_path=asar)
    assert result["desktop_status"] == "patched"
    assert '"jetski.cloudCodeUrl": "http://127.0.0.1:7777"' in ide.read_text()
    assert "// keep comment" in ide.read_text()
    header, files = patcher.unpack_asar(asar.read_bytes())
    assert files["other.txt"] == b"untouched"
    for name in ("main.js", "languageServer.js"):
        entry = header["files"]["dist"]["files"][name]
        data = files["dist/" + name]
        assert entry["integrity"]["hash"] == hashlib.sha256(data).hexdigest()
        assert entry["integrity"]["blocks"] == [hashlib.sha256(data).hexdigest()]
        assert patcher.MARKER.encode() in data
        assert b"7777" in data
    assert b"--inference_api_server_url" in files["dist/languageServer.js"]
    assert patcher.integration_status([ide], asar)["desktop_status"] == "patched"
    patcher.patch_antigravity(8888, ide_paths=[ide], asar_path=asar)
    assert b"7777" not in patcher.unpack_asar(asar.read_bytes())[1]["dist/main.js"]
    patcher.restore_antigravity(ide_paths=[ide], asar_path=asar)
    assert asar.read_bytes() == original
    assert "jetski.cloudCodeUrl" not in ide.read_text()
    assert "editor.fontSize" in ide.read_text()
    assert "// keep comment" in ide.read_text()
    assert ide.with_name("settings.json.bsl.bak").exists()


def test_legacy_asar_recovery(tmp_path):
    asar = tmp_path / "app.asar"
    asar.write_bytes(make_asar())
    legacy = tmp_path / ".antigravity-model-patch"
    backup = legacy / "backups/one/app.asar"
    backup.parent.mkdir(parents=True)
    backup.write_bytes(asar.read_bytes())
    (legacy / "state.json").write_text('{}')
    asar.write_bytes(b"old patched archive")
    patcher.patch_antigravity(ide_paths=[], asar_path=asar)
    assert (tmp_path / ".bsl-router-patch/original.app.asar").read_bytes() == backup.read_bytes()
    patcher.restore_antigravity(ide_paths=[], asar_path=asar)
    assert asar.read_bytes() == backup.read_bytes()


@pytest.mark.parametrize("name", ["dist/main.js", "dist/languageServer.js"])
@pytest.mark.parametrize("marker", [patcher.MARKER.encode(), b"/* __ag_model_patch__ */"])
def test_original_backup_rejects_patched_archive(tmp_path, name, marker):
    asar = tmp_path / "app.asar"
    header, files = patcher.unpack_asar(make_asar())
    files[name] += marker
    patched = patcher.pack_asar(header, files)
    asar.write_bytes(patched)
    with pytest.raises(ValueError, match="No clean vendor ASAR backup available"):
        patcher._original_backup(asar)
    assert not (tmp_path / ".bsl-router-patch/original.app.asar").exists()
    assert asar.read_bytes() == patched


def test_original_backup_without_legacy_state_uses_shallow_glob(tmp_path, monkeypatch):
    asar = tmp_path / "app.asar"
    asar.write_bytes(b"old patched archive")
    legacy = tmp_path / ".antigravity-model-patch"
    backup = legacy / "backups/v-2.25.0/app.asar"
    backup.parent.mkdir(parents=True)
    original = make_asar()
    backup.write_bytes(original)
    assert not (legacy / "state.json").exists()
    glob = Path.glob
    patterns = []
    def shallow_glob(path, pattern):
        patterns.append(pattern)
        assert pattern == "*/app.asar"
        return glob(path, pattern)
    monkeypatch.setattr(Path, "glob", shallow_glob)
    recovered = patcher._original_backup(asar)
    assert patterns == ["*/app.asar"]
    assert recovered == tmp_path / ".bsl-router-patch/original.app.asar"
    assert recovered.read_bytes() == original
    assert asar.read_bytes() == b"old patched archive"


def test_staged_recovery(tmp_path, monkeypatch):
    asar = tmp_path / "app.asar"
    asar.write_bytes(make_asar())
    replace = patcher.os.replace
    def locked(source, target):
        if Path(target) == asar:
            raise PermissionError("running")
        return replace(source, target)
    monkeypatch.setattr(patcher.os, "replace", locked)
    result = patcher.patch_antigravity(ide_paths=[], asar_path=asar)
    assert result["desktop_status"] == "staged_restart_required"
    assert patcher.integration_status([], asar)["desktop_status"] == "staged_restart_required"
    monkeypatch.setattr(patcher.os, "replace", replace)
    assert patcher.apply_staged_asar_if_present(asar)
    assert patcher.integration_status([], asar)["desktop_status"] == "patched"


def test_staged_recovery_returns_false_while_asar_is_locked(tmp_path, monkeypatch):
    asar = tmp_path / "app.asar"
    staged = tmp_path / "app.asar.bsl-staged"
    staged.write_bytes(make_asar())

    def locked(source, target):
        raise PermissionError("running")

    monkeypatch.setattr(patcher.os, "replace", locked)
    assert patcher.apply_staged_asar_if_present(asar) is False
    assert staged.exists()


def test_claude_sync_preserves_settings_and_cleans_managed_keys(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    original = {"permissions": {"allow": ["Read"]}, "hooks": {}, "mcpServers": {"local": {}}, "env": {"MY_ENV": "keep"}}
    path.write_text(json.dumps(original))
    monkeypatch.setattr(main, "_CLAUDE_SETTINGS_PATH", str(path))
    cfg = config()
    main._sync_claude_code_settings(cfg)
    result = json.loads(path.read_text())
    for key in ("permissions", "hooks", "mcpServers"):
        assert result[key] == original[key]
    assert result["env"]["MY_ENV"] == "keep"
    assert result["env"]["ANTHROPIC_AUTH_TOKEN"] == "test-key"
    assert result["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:6969"
    assert result["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "claude-bsl-tier-opus"
    assert result["model"] == "claude-bsl-1"
    assert result["modelPicker"]["replaceBuiltInOptions"] is True
    assert result["modelPicker"]["options"][0]["model"] == "claude-bsl-1"
    assert result["modelPicker"]["options"][0]["label"] == "My Coder"
    cfg["claude_code_integration"]["enabled"] = False
    main._sync_claude_code_settings(cfg)
    assert json.loads(path.read_text()) == original


def test_claude_safe_aliases_and_tier_fallback():
    cfg = config()
    cc = cfg["claude_code_integration"]
    cc["custom_models"][0]["alias_id"] = "gpt-unsafe"
    cc["tier_slots"]["haiku"] = "fake/model"
    main._validate_claude_code_integration_config(cfg)
    assert cc is not cfg["claude_code_integration"]
    assert cfg["claude_code_integration"]["custom_models"][0]["alias_id"] == "claude-bsl-1"
    assert main._resolve_claude_code_alias(cfg, "claude-bsl-tier-haiku") == "fake/model"


def test_grpc_proxy_forwards_and_injects(monkeypatch):
    from urllib.parse import urlencode
    cfg = config()
    monkeypatch.setattr(main, "cs_get_config", lambda: cfg)
    calls = []
    class Client:
        def __init__(self, **kwargs):
            assert kwargs["verify"] is False
            assert kwargs["timeout"] == 30.0
            assert kwargs["follow_redirects"] is False
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return httpx.Response(200, content=frame(b""))
    monkeypatch.setattr(main.httpx, "AsyncClient", Client)
    url = "http://127.0.0.1:1234/LanguageServerService/GetAvailableModels"
    result = asyncio.run(main.antigravity_get_available_models(request("/GetAvailableModels", query=urlencode({"ls": url}).encode(), raw=b"wire-request")))
    assert result.status_code == 200
    assert b"models/MODEL_PLACEHOLDER_M401" in result.body
    assert calls[0][0] == url
    assert calls[0][1]["content"] == b"wire-request"
    assert calls[0][1]["headers"]["Content-Type"] == "application/grpc-web+proto"


def test_catalog_upstream_merge_and_failure(monkeypatch):
    cfg = config()
    monkeypatch.setattr(main, "cs_get_config", lambda: cfg)
    built = []
    class Client:
        def build_request(self, method, url, **kwargs):
            built.append(kwargs)
            return httpx.Request(method, url, **kwargs)
        async def send(self, req):
            return httpx.Response(200, json={"models": {"native": {"displayName": "Native"}}})
    monkeypatch.setattr(main, "_get_antigravity_egress_client", lambda: Client())
    req = request("/v1internal:fetchAvailableModels", headers={"Authorization": "Bearer native", "accept-encoding": "gzip"})
    result = asyncio.run(main.antigravity_fetch_available_models(req))
    assert set(json.loads(result.body)["models"]) == {"native", "custom-coder-3"}
    assert not any(k.lower() == "accept-encoding" for k in built[0]["headers"])
    class FailedClient(Client):
        async def send(self, req):
            raise httpx.ConnectError("offline")
    monkeypatch.setattr(main, "_get_antigravity_egress_client", lambda: FailedClient())
    result = asyncio.run(main.antigravity_fetch_available_models(req))
    assert set(json.loads(result.body)["models"]) == {"custom-coder-3"}


def test_jsonc_existing_endpoint_restore_and_invalid_preservation(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"one": 1, /*comment*/ "jetski.cloudCodeUrl": "https://old", "two": 2,}')
    patcher.patch_ide_settings(path, 7777)
    assert "/*comment*/" in path.read_text()
    assert path.read_text().count("jetski.cloudCodeUrl") == 1
    patcher.patch_ide_settings(path, restore=True)
    text = path.read_text()
    assert json.loads(__import__('re').sub(r',\s*}', '}', patcher._jsonc_mask(text))) == {"one": 1, "two": 2}
    path.write_text('{broken json')
    with pytest.raises(ValueError):
        patcher.patch_ide_settings(path)
    assert path.read_text() == '{broken json'


def test_frontend_shared_picker_and_controls():
    source = (Path(__file__).parents[1] / "static/app.js").read_text(encoding="utf-8")
    assert "function(context = 'combo')" in source
    assert "openAddModelModal('antigravity')" in source
    assert "openAddModelModal('claude_code')" in source
    assert '<details class="app-integration-details"><summary>Native model slot mappings' in source
    assert "app-integration-model-row" in source
    assert "<span>App Integration</span>" in source
    assert "[CC]" not in source
    assert "Claude Code (Desktop / CLI)" in source
    assert "appIntegrationModels('antigravity')" in source
    assert "appIntegrationModels('claude_code')" in source
    assert "Hide built-in Claude models" in source
    assert "const AG_SLOTS=" in source


def test_identity_allocation_does_not_steal_later_existing_ids():
    cfg = config()
    cc = cfg["claude_code_integration"]
    cc["custom_models"] = [{"target": "fake/model"}, {"target": "coder-3", "alias_id": "claude-bsl-1"}]
    main._validate_claude_code_integration_config(cfg)
    assert [m["alias_id"] for m in cfg["claude_code_integration"]["custom_models"]] == ["claude-bsl-2", "claude-bsl-1"]
    cfg["antigravity_integration"]["custom_models"] = [{"target": "fake/model"}, {"target": "coder-3", "placeholder_index": 401}]
    main._validate_antigravity_integration_config(cfg)
    assert cfg["antigravity_integration"]["custom_models"][1]["placeholder_index"] == 401


def test_asar_multiblock_integrity_and_namespaced_app():
    data = b"x" * (patcher.BLOCK_SIZE + 17)
    header = {"files": {"big": {"size": 0, "offset": "0"}}}
    packed = patcher.pack_asar(header, {"big": data})
    decoded, files = patcher.unpack_asar(packed)
    integrity = decoded["files"]["big"]["integrity"]
    assert files["big"] == data
    assert integrity["blocks"] == [hashlib.sha256(data[:patcher.BLOCK_SIZE]).hexdigest(), hashlib.sha256(data[patcher.BLOCK_SIZE:]).hexdigest()]
    source = "electron.app.whenReady().then(() => {});"
    result = patcher._patch_main(source, "http://127.0.0.1:7777")
    assert result.count("electron.app.whenReady()") == 2
    assert "electron." + patcher.MARKER not in result


@pytest.mark.parametrize("source", ["electron_1.app\n    .whenReady().then(() => {});", "app.whenReady().then(() => {});"])
def test_patch_main_ready_hook_and_language_server_scope(source):
    result = patcher._patch_main(source, "http://127.0.0.1:7777")
    assert result.endswith(source)
    assert result.index(patcher.MARKER) < result.index(source)
    assert "webRequest.onBeforeRequest((details, callback) => { try {" in result
    assert "const lsPort = require('./languageServer').getLsPort();" in result
    assert "['127.0.0.1', 'localhost'].includes(u.hostname) && (!lsPort || Number(u.port) === Number(lsPort))" in result
    assert "LanguageServerService/SetCloudCodeURL')) return callback({cancel:true})" in result
    assert 'redirectURL:"http://127.0.0.1:7777/GetAvailableModels?ls=" + encodeURIComponent(details.url)' in result
    assert "} } catch {} callback({});" in result
    assert "onBeforeRequest({urls:" not in result
    if source.startswith("electron_1"):
        assert "electron_1.app.whenReady().then(" in result


def test_api_status_sync_and_patch(monkeypatch, tmp_path):
    cfg = config()
    monkeypatch.setattr(main, "cs_get_config", lambda: cfg)
    monkeypatch.setattr(main, "_CLAUDE_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(patcher, "integration_status", lambda: {"desktop_status": "original", "ide": []})
    status = json.loads(asyncio.run(main.app_integration_status()).body)
    assert status["antigravity"]["enabled"] is True
    assert status["claude_code"]["models"] == 2
    result = asyncio.run(main.app_integration_claude_code_sync())
    assert result.status_code == 200
    assert json.loads((tmp_path / "settings.json").read_text())["model"] == "claude-bsl-1"
    calls = []
    def patch(**kwargs):
        calls.append(kwargs)
        return {"ok": True}
    monkeypatch.setattr(patcher, "patch_antigravity", patch)
    result = asyncio.run(main.app_integration_antigravity_patch(request("/api/app-integration/antigravity/patch", {"action": "restore"})))
    assert result.status_code == 200
    assert calls == [{"port": 6969, "restore": True}]
    result = asyncio.run(main.app_integration_antigravity_patch(request("/api/app-integration/antigravity/patch", {"action": "invalid"})))
    assert result.status_code == 400
    routes = {route.path for route in main.app.routes}
    assert {"/api/app-integration/status", "/api/app-integration/antigravity/patch", "/api/app-integration/claude-code/sync", "/GetAvailableModels", "/v1internal:fetchAvailableModels", "/v1internal/fetchAvailableModels"} <= routes
    paths = [route.path for route in main.app.routes]
    assert paths.index("/v1internal:fetchAvailableModels") < paths.index("/v1internal:{operation}")


def test_claude_catalog_and_inference(monkeypatch):
    cfg = config()
    monkeypatch.setattr(main, "cs_get_config", lambda: cfg)
    result = asyncio.run(main.list_models())
    models = json.loads(result.body)["data"]
    custom = next(m for m in models if m["id"] == "claude-bsl-1")
    assert custom["display_name"] == "My Coder"
    assert custom["owned_by"] == "anthropic"
    gw_result = asyncio.run(main.list_models(request("/v1/models", query=b"limit=1000", headers={"anthropic-version": "2023-06-01"})))
    gw_models = json.loads(gw_result.body)["data"]
    assert [m["id"] for m in gw_models] == ["claude-bsl-1", "claude-bsl-tier-opus"]
    captured = []
    async def dispatch(body, **kwargs):
        captured.append((body, kwargs))
        return JSONResponse({"ok": True})
    monkeypatch.setattr(main, "_process_chat_completion", dispatch)
    asyncio.run(main.anthropic_messages(request("/v1/messages", {"model": "claude-bsl-1", "max_tokens": 32, "messages": [{"role": "user", "content": "hello"}]})))
    assert captured[-1][0]["model"] == "coder-3"
    assert captured[-1][1]["client_wants_anthropic"] is True
    asyncio.run(main.chat_completions(request("/v1/chat/completions", {"model": "claude-bsl-1", "messages": [{"role": "user", "content": "hello"}]})))
    assert captured[-1][0]["model"] == "coder-3"
    cfg["claude_code_integration"]["enabled"] = False
    assert main._resolve_claude_code_alias(cfg, "claude-bsl-1") is None
    assert not any(m["id"].startswith("claude-bsl-") for m in json.loads(asyncio.run(main.list_models()).body)["data"])
