"""Tests for the Unified 4-Plane Modular Switchboard (Antigravity / Blacksand Code + Claude Code)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.ls_components import (
    DEFAULT_ANTIGRAVITY_LS_COMPONENTS,
    LS_COMPONENTS_PRESETS,
    apply_ls_components_to_anthropic_body,
    apply_ls_components_to_gemini_inner,
    apply_ls_components_to_raw_v1internal,
    detect_ls_components_environment,
    filter_list_experiments_bytes,
    requires_experiment_filtering,
    requires_wire_filtering,
    sync_antigravity_hooks_file,
    sync_claude_code_hooks_file,
    validate_ls_components_config,
)


def test_fast_path_zero_overhead_when_all_on_or_target_disabled():
    cfg = validate_ls_components_config(None)
    assert cfg == DEFAULT_ANTIGRAVITY_LS_COMPONENTS
    assert requires_wire_filtering(cfg, "antigravity") is False
    assert requires_wire_filtering(cfg, "claude_code") is False
    assert requires_experiment_filtering(cfg) is False

    raw = b'{"model":"gemini-4-argon","request":{"systemInstruction":{"parts":[{"text":"<guidelines>strict</guidelines>"}]},"contents":[{"role":"user","parts":[{"text":"hello"}]}]}}'
    assert apply_ls_components_to_raw_v1internal(raw, cfg) is raw
    exp = b'{"flags":[{"name":"enable-plugins","boolValue":true}]}'
    assert filter_list_experiments_bytes(exp, cfg) is exp

    # Target-specific bypass: Lean Coder with apply_antigravity=False bypasses Antigravity completely
    lean_cc_only = validate_ls_components_config({"preset": "lean_coder", "apply_antigravity": False, "apply_claude_code": True})
    assert requires_wire_filtering(lean_cc_only, "antigravity") is False
    assert requires_experiment_filtering(lean_cc_only) is False
    assert requires_wire_filtering(lean_cc_only, "claude_code") is True
    assert apply_ls_components_to_raw_v1internal(raw, lean_cc_only) is raw

    # And with apply_claude_code=False, Anthropic body is untouched
    lean_ag_only = validate_ls_components_config({"preset": "lean_coder", "apply_antigravity": True, "apply_claude_code": False})
    assert requires_wire_filtering(lean_ag_only, "claude_code") is False
    anth = {"system": "<guidelines>block</guidelines>", "tools": [{"name": "AskUserQuestion"}]}
    assert apply_ls_components_to_anthropic_body(anth, lean_ag_only) is False
    assert len(anth["tools"]) == 1


def test_plane3_strips_only_unchecked_xml_sections():
    cfg = validate_ls_components_config({"prompt_guidelines": False, "prompt_skills_catalog": False})
    assert requires_wire_filtering(cfg, "antigravity") is True

    inner = {
        "systemInstruction": {
            "parts": [
                {
                    "text": (
                        "<identity>BSL</identity>\n"
                        "<guidelines>Refuse CAPTCHA</guidelines>\n"
                        "<user_rules>Keep git safe</user_rules>\n"
                        "<skills>43 skills...</skills>\n"
                        "<mcp_servers>code-atlas</mcp_servers>"
                    )
                }
            ]
        },
        "contents": [{"role": "user", "parts": [{"text": "fix bug"}]}],
    }
    changed = apply_ls_components_to_gemini_inner(inner, cfg)
    assert changed is True
    out = inner["systemInstruction"]["parts"][0]["text"]
    assert "<guidelines>" not in out
    assert "<skills>" not in out
    assert "<identity>BSL</identity>" in out
    assert "<user_rules>Keep git safe</user_rules>" in out
    assert "<mcp_servers>code-atlas</mcp_servers>" in out


def test_plane1_strips_pre_invocation_metadata_from_user_turns_only():
    cfg = validate_ls_components_config({"hook_pre_invocation": False})
    inner = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": (
                            "[BSL-ROUTING-METADATA]max_cli_calls=3[/BSL-ROUTING-METADATA]\n"
                            "<bsl_Mode_Payload>plan</bsl_Mode_Payload>\n"
                            "<bsl_Completion_Contract mode=\"strict\" />\n"
                            "Actual user request"
                        )
                    }
                ],
            },
            {
                "role": "model",
                "parts": [{"text": "[BSL-ROUTING-METADATA]quoted in model reply[/BSL-ROUTING-METADATA]"}],
            },
        ]
    }
    assert apply_ls_components_to_gemini_inner(inner, cfg) is True
    assert inner["contents"][0]["parts"][0]["text"].strip() == "Actual user request"
    assert "[BSL-ROUTING-METADATA]" in inner["contents"][1]["parts"][0]["text"]


def test_plane4_filters_disabled_tools_and_cleans_empty_tools_key():
    cfg = validate_ls_components_config({
        "tool_ask_question": False,
        "tool_mcp_bridge": False,
        "tool_generate_image": False,
    })
    inner = {
        "tools": [
            {
                "functionDeclarations": [
                    {"name": "read_file"},
                    {"name": "ask_question"},
                    {"name": "call_mcp_tool"},
                    {"name": "mcp_code_atlas_search"},
                    {"name": "generate_image"},
                    {"name": "search_web"},
                ]
            }
        ],
        "toolConfig": {"functionCallingConfig": {"mode": "AUTO"}},
    }
    assert apply_ls_components_to_gemini_inner(inner, cfg) is True
    kept_names = [fd["name"] for fd in inner["tools"][0]["functionDeclarations"]]
    assert kept_names == ["read_file", "search_web"]
    assert "toolConfig" in inner

    cfg_all_off = validate_ls_components_config({
        "tool_core_execution": False,
        "tool_web_search": False,
    })
    assert apply_ls_components_to_gemini_inner(inner, cfg_all_off) is True
    assert "tools" not in inner
    assert "toolConfig" not in inner


def test_claude_code_anthropic_body_filtering():
    cfg = validate_ls_components_config({"preset": "raw_uncensored"})
    body = {
        "model": "claude-opus-4-8",
        "system": [
            {"type": "text", "text": "Base coder prompt.\n<guidelines>Refuse X</guidelines>\n<skills>catalog</skills>"},
            {"type": "text", "text": "<system-reminder>Contents of CLAUDE.md project instructions</system-reminder>Keep this."},
        ],
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "<system-reminder>UserPromptSubmit hook output: [BSL-ROUTING-METADATA]x[/BSL-ROUTING-METADATA]</system-reminder>Fix login.py"},
                    {"type": "text", "text": "<system-reminder>Unrelated file edit warning</system-reminder>"},
                ],
            }
        ],
        "tools": [
            {"name": "Read"},
            {"name": "Edit"},
            {"name": "Bash"},
            {"name": "AskUserQuestion"},
            {"name": "Agent"},
            {"name": "mcp__code_atlas__search"},
            {"name": "WebSearch"},
            {"name": "generate_image"},
        ],
        "tool_choice": {"type": "auto"},
    }
    assert apply_ls_components_to_anthropic_body(body, cfg) is True
    assert "<guidelines>" not in body["system"][0]["text"]
    assert "<skills>" not in body["system"][0]["text"]
    assert "Base coder prompt." in body["system"][0]["text"]
    assert "CLAUDE.md" not in body["system"][1]["text"]
    assert "Keep this." in body["system"][1]["text"]
    assert "UserPromptSubmit hook" not in body["messages"][0]["content"][0]["text"]
    assert "Fix login.py" in body["messages"][0]["content"][0]["text"]
    # Unrelated reminder preserved
    assert "Unrelated file edit warning" in body["messages"][0]["content"][1]["text"]
    kept_tools = [t["name"] for t in body["tools"]]
    assert kept_tools == ["Read", "Edit", "Bash", "WebSearch"]


def test_plane2_list_experiments_wire_filtering_preserves_other_flags():
    cfg = validate_ls_components_config({"preset": "lean_coder"})
    assert requires_experiment_filtering(cfg) is True

    raw = json.dumps({
        "flags": [
            {"name": "enable-customization-kind-skill", "boolValue": True},
            {"name": "enable-customization-kind-rule", "boolValue": True},
            {"name": "unrelated-google-flag", "boolValue": True},
        ]
    }).encode("utf-8")
    patched = json.loads(filter_list_experiments_bytes(raw, cfg).decode("utf-8"))
    by_name = {f["name"]: f for f in patched["flags"]}
    assert by_name["enable-customization-kind-skill"]["boolValue"] is False
    assert by_name["enable-customization-kind-rule"]["boolValue"] is True
    assert by_name["unrelated-google-flag"]["boolValue"] is True


def test_plane1_lossless_hooks_json_stash_and_restore(tmp_path: Path):
    hooks_file = tmp_path / "hooks.json"
    original = {
        "bsl-studio-core-sync": {
            "enabled": True,
            "SessionStart": [{"type": "command", "command": "node start.js"}],
            "PreInvocation": [{"type": "command", "command": "node pre.js"}],
            "Stop": [{"type": "command", "command": "node stop.js"}],
        }
    }
    hooks_file.write_text(json.dumps(original, indent=2), encoding="utf-8")

    lean = validate_ls_components_config({"preset": "lean_coder"})
    res1 = sync_antigravity_hooks_file(lean, hooks_paths=[hooks_file])
    assert str(hooks_file) in res1["updated"]
    after_off = json.loads(hooks_file.read_text(encoding="utf-8"))
    assert "SessionStart" in after_off["bsl-studio-core-sync"]
    assert "PreInvocation" not in after_off["bsl-studio-core-sync"]
    assert "Stop" not in after_off["bsl-studio-core-sync"]

    # Idempotent no-op
    res2 = sync_antigravity_hooks_file(lean, hooks_paths=[hooks_file])
    assert res2["updated"] == []

    # Unchecking apply_antigravity restores exact original hooks cleanly
    res3 = sync_antigravity_hooks_file({**lean, "apply_antigravity": False}, hooks_paths=[hooks_file])
    assert str(hooks_file) in res3["updated"]
    assert json.loads(hooks_file.read_text(encoding="utf-8")) == original
    assert not (tmp_path / "hooks.json.bsl-stash.json").exists()


def test_claude_code_lossless_settings_stash_and_restore(tmp_path: Path):
    settings_file = tmp_path / "settings.json"
    original = {
        "skillOverrides": {"bsl-memory-bridge": "on", "bsl-intent-router": "on"},
        "hooks": {
            "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "node up.js"}]}],
            "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "node pre.js"}]}],
            "Stop": [{"hooks": [{"type": "command", "command": "node stop.js"}]}],
        },
        "statusLine": {"type": "command", "command": "node status.js"},
    }
    settings_file.write_text(json.dumps(original, indent=2), encoding="utf-8")

    lean = validate_ls_components_config({"preset": "lean_coder"})
    res1 = sync_claude_code_hooks_file(lean, settings_path=settings_file)
    assert str(settings_file) in res1["updated"]
    after_lean = json.loads(settings_file.read_text(encoding="utf-8"))
    assert "UserPromptSubmit" not in after_lean["hooks"]
    assert "Stop" not in after_lean["hooks"]
    assert "PreToolUse" in after_lean["hooks"]
    assert after_lean["skillOverrides"] == {"bsl-memory-bridge": "off", "bsl-intent-router": "off"}
    assert "statusLine" in after_lean

    # Unchecking apply_claude_code restores all stashed hooks + skillOverrides losslessly
    res2 = sync_claude_code_hooks_file({**lean, "apply_claude_code": False}, settings_path=settings_file)
    assert str(settings_file) in res2["updated"]
    assert json.loads(settings_file.read_text(encoding="utf-8")) == original
    assert not (tmp_path / "settings.json.bsl-stash.json").exists()


def test_api_endpoint_preserves_target_checkboxes_and_ui_present(monkeypatch):
    import app.main as main_mod

    saved_cfgs = []
    fake_cfg = copy.deepcopy(main_mod.cs_get_config())
    monkeypatch.setattr(main_mod, "cs_get_config", lambda: fake_cfg)
    monkeypatch.setattr(main_mod, "_replace_runtime_config", lambda c: saved_cfgs.append(copy.deepcopy(c)))

    client = TestClient(main_mod.app)
    # Turn off Claude Code target first
    r0 = client.post("/api/app-integration/antigravity/ls-components", json={"apply_claude_code": False})
    assert r0.status_code == 200
    assert r0.json()["ls_components"]["apply_claude_code"] is False

    # Activate Lean Coder preset -> target checkbox apply_claude_code=False MUST be preserved!
    r = client.post("/api/app-integration/antigravity/ls-components", json={"preset": "lean_coder"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["ls_components"]["apply_claude_code"] is False
    assert body["ls_components"]["apply_antigravity"] is True
    assert body["ls_components"]["prompt_guidelines"] is False
    assert body["ls_components"]["tool_ask_question"] is False

    validated = main_mod._validate_antigravity_integration_config(copy.deepcopy(fake_cfg))
    assert validated["antigravity_integration"]["ls_components"]["apply_claude_code"] is False

    # Verify unified collapsible box + target checkboxes + 4 plane subtabs exist in app.js
    app_js = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(encoding="utf-8")
    assert "Unified 4-Plane Modular Switchboard" in app_js
    assert 'data-ls-key="apply_antigravity"' in app_js
    assert 'data-ls-key="apply_claude_code"' in app_js
    assert "Plane 1 · Hooks" in app_js
    assert "Plane 2 · Discovery" in app_js
    assert "Plane 3 · Prompt XML" in app_js
    assert "Plane 4 · Tools" in app_js
    assert "is-inapplicable" in app_js


def test_detect_environment_vanilla_antigravity_and_claude_code_blur(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("BSL_BLACKSAND_STUDIO_ASAR", raising=False)
    monkeypatch.delenv("BSL_BLACKSAND_CODE_ASAR", raising=False)

    # 1. Vanilla GitHub user with original Antigravity only (no hooks.json, no Blacksand Code, no Claude Code)
    fake_home = tmp_path / "home"
    (fake_home / ".gemini" / "antigravity").mkdir(parents=True)
    fake_local = tmp_path / "local"
    fake_roaming = tmp_path / "roaming"

    env_vanilla = detect_ls_components_environment(
        DEFAULT_ANTIGRAVITY_LS_COMPONENTS,
        home_dir=fake_home,
        appdata_dir=fake_roaming,
        localappdata_dir=fake_local,
    )
    assert env_vanilla["apps"]["antigravity_installed"] is True
    assert env_vanilla["apps"]["blacksand_code_installed"] is False
    assert env_vanilla["apps"]["claude_code_installed"] is False
    assert env_vanilla["apps"]["has_bsl_hooks"] is False
    assert env_vanilla["apps"]["antigravity_label"] == "Antigravity App / IDE"
    # Plane 1 hooks are blurred as "No hook configured" when user has no hooks.json
    assert env_vanilla["applicability"]["hook_stop"]["applicable"] is False
    assert env_vanilla["applicability"]["hook_stop"]["reason"] == "No hook configured"
    # Planes 2/3/4 Antigravity items are applicable
    assert env_vanilla["applicability"]["prompt_guidelines"]["applicable"] is True
    assert env_vanilla["applicability"]["tool_ask_question"]["applicable"] is True

    # 2. Claude Code installed, and user selects Claude Code Only -> Antigravity-only items blur
    cc_dir = fake_home / ".claude"
    cc_dir.mkdir(parents=True)
    (cc_dir / "settings.json").write_text(
        json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo stop"}]}]}}),
        encoding="utf-8",
    )
    env_cc_only = detect_ls_components_environment(
        {**DEFAULT_ANTIGRAVITY_LS_COMPONENTS, "apply_antigravity": False, "apply_claude_code": True},
        home_dir=fake_home,
        appdata_dir=fake_roaming,
        localappdata_dir=fake_local,
    )
    assert env_cc_only["apps"]["claude_code_installed"] is True
    assert env_cc_only["applicability"]["hook_stop"]["applicable"] is True
    assert env_cc_only["applicability"]["hook_pre_tool_use"]["applicable"] is False
    assert env_cc_only["applicability"]["custom_plugins"]["applicable"] is False
    assert "only" in env_cc_only["applicability"]["custom_plugins"]["reason"].lower()
    assert env_cc_only["applicability"]["prompt_guidelines"]["applicable"] is False
    assert env_cc_only["applicability"]["tool_generate_image"]["applicable"] is False
    assert env_cc_only["applicability"]["prompt_user_rules"]["applicable"] is True
    assert env_cc_only["applicability"]["prompt_skills_catalog"]["applicable"] is True
    assert env_cc_only["applicability"]["custom_skills"]["applicable"] is True
    assert env_cc_only["applicability"]["tool_core_execution"]["applicable"] is True

    # 3. Blacksand Code installed + paused hook in .bsl-stash.json stays applicable (never falsely blurred)
    bsl_dir = fake_home / ".gemini" / "blacksand-studio"
    bsl_dir.mkdir(parents=True)
    (bsl_dir / "hooks.json").write_text(json.dumps({"hooks": {}}), encoding="utf-8")
    (bsl_dir / "hooks.json.bsl-stash.json").write_text(
        json.dumps({"hooks": {"Stop": [{"command": "node bsl-studio-stop.js"}]}}),
        encoding="utf-8",
    )
    env_bsl = detect_ls_components_environment(
        {**DEFAULT_ANTIGRAVITY_LS_COMPONENTS, "apply_antigravity": True, "apply_claude_code": False},
        home_dir=fake_home,
        appdata_dir=fake_roaming,
        localappdata_dir=fake_local,
    )
    assert env_bsl["apps"]["blacksand_code_installed"] is True
    assert env_bsl["apps"]["has_bsl_hooks"] is True
    assert env_bsl["apps"]["antigravity_label"] == "Antigravity / Blacksand Code"
    assert env_bsl["applicability"]["hook_stop"]["applicable"] is True


def test_adversarial_crucible_claude_code_fastpath_and_shared_custom_rules(tmp_path: Path, monkeypatch):
    """Adversarial Crucible regressions:
    1. Disabling Antigravity-only XML/tool switches (`prompt_guidelines`, `prompt_mcp_servers`, `tool_generate_image`)
       MUST keep `requires_wire_filtering(cfg, 'claude_code')` False (0.0ms fast-path preserved for Claude Code).
    2. `custom_rules` is shared across Antigravity AND Claude Code (controls `CLAUDE.md` reminder stripping
       and stays applicable in Claude-Code-only mode).
    3. `detect_ls_components_environment` discovers `~/.gemini/config/hooks.json` and `blacksand-code-lite`,
       and does NOT falsely mark `hook_pre_invocation` applicable on vanilla Claude Code without hooks.
    """
    monkeypatch.delenv("BSL_BLACKSAND_STUDIO_ASAR", raising=False)
    monkeypatch.delenv("BSL_BLACKSAND_CODE_ASAR", raising=False)

    ag_only_disabled = validate_ls_components_config({
        "prompt_guidelines": False,
        "prompt_mcp_servers": False,
        "prompt_subagents_section": False,
        "prompt_artifacts_section": False,
        "prompt_slash_and_style": False,
        "tool_generate_image": False,
    })
    assert requires_wire_filtering(ag_only_disabled, "antigravity") is True
    assert requires_wire_filtering(ag_only_disabled, "claude_code") is False

    # Turning off custom_rules alone activates Claude Code wire filtering & strips CLAUDE.md reminder
    cc_rules_off = validate_ls_components_config({"custom_rules": False})
    assert requires_wire_filtering(cc_rules_off, "claude_code") is True
    cc_body = {
        "system": "<system-reminder>Contents of CLAUDE.md project instructions</system-reminder>Hello",
        "messages": [{"role": "user", "content": "hi"}],
    }
    assert apply_ls_components_to_anthropic_body(cc_body, cc_rules_off) is True
    assert "CLAUDE.md" not in cc_body["system"]
    assert "Hello" in cc_body["system"]

    # Vanilla Claude Code without UserPromptSubmit or BSL hooks blurs hook_pre_invocation,
    # while custom_rules stays applicable!
    fake_home = tmp_path / "home2"
    (fake_home / ".claude").mkdir(parents=True)
    (fake_home / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    env_cc_vanilla = detect_ls_components_environment(
        {**DEFAULT_ANTIGRAVITY_LS_COMPONENTS, "apply_antigravity": False, "apply_claude_code": True},
        home_dir=fake_home,
        appdata_dir=tmp_path / "r",
        localappdata_dir=tmp_path / "l",
    )
    assert env_cc_vanilla["applicability"]["custom_rules"]["applicable"] is True
    assert env_cc_vanilla["applicability"]["hook_pre_invocation"]["applicable"] is False

    # Blacksand Code Lite + ~/.gemini/config/hooks.json discovery
    (fake_home / ".gemini" / "blacksand-code-lite").mkdir(parents=True)
    (fake_home / ".gemini" / "config").mkdir(parents=True)
    (fake_home / ".gemini" / "config" / "hooks.json").write_text(
        json.dumps({"PreInvocation": [{"command": "node pre_invocation.js"}]}),
        encoding="utf-8",
    )
    env_lite = detect_ls_components_environment(
        DEFAULT_ANTIGRAVITY_LS_COMPONENTS,
        home_dir=fake_home,
        appdata_dir=tmp_path / "r",
        localappdata_dir=tmp_path / "l",
    )
    assert env_lite["apps"]["blacksand_code_installed"] is True
    assert env_lite["applicability"]["hook_pre_invocation"]["applicable"] is True

