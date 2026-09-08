"""Pure static-assert tests for the FEL Tools-tab UI (FEL-2b).

The admin UI is a static HTML + vanilla-JS app (no component framework, no
node-based test harness in this repo), so these tests assert the shipped
artifacts directly:

  1. index.html hosts <template id="fel-block-template"> containing all
     required control IDs and response-surface legend headers.
  2. app.js wires renderToolsTab() to inject ${felBlockHTML()}.
  3. felBlockHTML serializes defaults via setAttribute/textContent (the
     innerHTML-serialization trap), never .value/.checked assignment.
  4. Handlers exist and persist only the six keys FEL owns, never
     replacing tools.fel wholesale.
"""

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")

REQUIRED_IDS = [
    "fel-master",
    "fel-recovery",
    "fel-clarity",
    "fel-ctx",
    "fel-clientref",
    "fel-scope",
    "fel-block-template",
]

LEGEND_HEADERS = [
    "x-bsl-fel",
    "x-bsl-clarified",
    "x-bsl-recovered",
    "x-bsl-refusal",
]


def _template_src() -> str:
    m = re.search(
        r"<template[^>]*id=[\"']fel-block-template[\"'][^>]*>(.*?)</template>",
        HTML,
        re.S,
    )
    assert m, "fel-block-template <template> not found in index.html"
    return m.group(1)


def test_template_contains_all_control_ids():
    tpl = _template_src()
    for cid in REQUIRED_IDS:
        if cid == "fel-block-template":
            continue
        assert f'id="{cid}"' in tpl, f"missing control id in template: {cid}"


def test_template_contains_legend_headers():
    tpl = _template_src()
    for hdr in LEGEND_HEADERS:
        assert hdr in tpl, f"missing response-surface legend row: {hdr}"


def test_template_handlers_wired():
    tpl = _template_src()
    assert 'onchange="felOnMaster(this)"' in tpl
    assert "felOnToggle(this, 'refusal_recovery.enabled')" in tpl
    assert "felOnToggle(this, 'clarity.enabled')" in tpl
    assert "felOnField(this, 'engagement.context')" in tpl
    assert "felOnField(this, 'engagement.client_ref')" in tpl
    assert "felOnField(this, 'engagement.scope')" in tpl


def test_renderToolsTab_injects_fel_block():
    assert "${felBlockHTML()}" in JS
    # The injection must sit inside renderToolsTab's returned template.
    start = JS.index("function renderToolsTab()")
    end = JS.index("${felBlockHTML()}")
    tail = JS.index("function felBlockHTML")
    assert start < end < tail, "felBlockHTML injected outside renderToolsTab"


def test_felblockhtml_uses_attribute_serialization():
    start = JS.index("function felBlockHTML")
    end = JS.index("function felOnMaster")
    body = JS[start:end]
    for cid in ["fel-master", "fel-recovery", "fel-clarity"]:
        assert f"setAttribute('checked'" in body, (
            "checkbox defaults must use setAttribute('checked') to survive innerHTML"
        )
    assert "setAttribute('value'" in body, (
        "text-input defaults must use setAttribute('value') to survive innerHTML"
    )
    assert ".textContent = " in body, (
        "textarea default must be set as textContent (child text is the default value)"
    )
    # The classic trap: property assignment before serialization is lost.
    assert not re.search(r"\.value\s*=", body), (
        "felBlockHTML must not use .value= (lost on innerHTML serialization)"
    )


def test_fel_handlers_persist_only_owned_keys():
    start = JS.index("function _felConfig")
    end = JS.index("// --- PHASE 3 Observability JS")
    body = JS[start:end]
    for fn in ["_felConfig", "felBlockHTML", "_felEnsure", "felOnMaster", "felOnToggle", "felOnField"]:
        assert f"function {fn}" in body, f"missing handler: {fn}"
    # Never replace tools.fel wholesale — only create-if-absent / per-key writes.
    assert "globalConfig.tools.fel = {}" in body, (
        "tools.fel must only be created when absent, preserving foreign sub-keys"
    )
    assert not re.search(r"tools\.fel\s*=\s*(?!{})\S", body), (
        "must not assign a fresh object over an existing tools.fel"
    )


def test_felbody_disabled_state():
    body_tpl = _template_src()
    assert 'id="fel-body"' in body_tpl, "fel-body wrapper missing from template"
    start = JS.index("function felBlockHTML")
    end = JS.index("function felOnMaster")
    js_body = JS[start:end]
    assert "toggleAttribute('disabled'" in js_body
    assert "0.5" in js_body, "dimmed opacity when master off"


# ── FEL-3: per-family toggles (additive) ───────────────────────────────────

FAMILY_IDS = ["fel-fam-cn", "fel-fam-gpt", "fel-fam-claude", "fel-fam-gemini"]
FAMILIES = ["cn", "gpt", "claude", "gemini"]


def test_template_contains_family_toggles():
    tpl = _template_src()
    for fid in FAMILY_IDS:
        assert f'id="{fid}"' in tpl, f"missing per-family toggle in template: {fid}"
    for fam in FAMILIES:
        assert f"felOnFamily(this, '{fam}')" in tpl, (
            f"missing felOnFamily handler wiring for family: {fam}"
        )


def test_felconfig_defaults_all_families_on():
    start = JS.index("function _felConfig")
    end = JS.index("function felBlockHTML")
    body = JS[start:end]
    assert "families:" in body, "_felConfig must expose a families default"
    for fam in FAMILIES:
        assert f"{fam}: true" in body, f"family {fam} must default ON"


def test_felblockhtml_stamps_family_toggles():
    start = JS.index("function felBlockHTML")
    end = JS.index("function _felEnsure")
    body = JS[start:end]
    for fid in FAMILY_IDS:
        assert f"'{fid}'" in body or f'"{fid}"' in body or f"fel-fam-" in body, (
            f"felBlockHTML must stamp family toggle default: {fid}"
        )
    assert "setAttribute('checked'" in body, (
        "family toggle defaults must use setAttribute (innerHTML serialization rule)"
    )


def test_felonfamily_persists_only_families_key():
    start = JS.index("function _felConfig")
    end = JS.index("// --- PHASE 3 Observability JS")
    body = JS[start:end]
    assert "function felOnFamily" in body, "missing felOnFamily handler"
    # Persists the families sub-key (create-if-absent), never tools.fel itself.
    assert "if (!fel.families) fel.families = {};" in body
    assert "fel.families[family] = cb.checked;" in body
    assert "scheduleAutoSave();" in body.split("function felOnFamily")[1]


def test_family_toggles_in_disabled_selectors():
    start = JS.index("function _felConfig")
    end = JS.index("// --- PHASE 3 Observability JS")
    body = JS[start:end]
    # Both the render-time dimming and the master-toggle handler must include
    # the family toggle ids in their disabled/dimmed selectors.
    assert body.count("fel-fam-cn") >= 2, (
        "family ids must appear in both felBlockHTML and felOnMaster dimming selectors"
    )
    for fid in FAMILY_IDS:
        assert fid in body


# ── FEL-5: research modes (additive) ───────────────────────────────────────

RESEARCH_IDS = ["fel-research-content", "fel-research-coding"]


def test_template_contains_research_toggles():
    tpl = _template_src()
    for rid in RESEARCH_IDS:
        assert f'id="{rid}"' in tpl, f"missing research toggle in template: {rid}"
    assert "felOnResearch(this, 'content')" in tpl, "content lane handler unwired"
    assert "felOnResearch(this, 'coding')" in tpl, "coding lane handler unwired"


def test_felconfig_research_defaults_off():
    start = JS.index("function _felConfig")
    end = JS.index("function felBlockHTML")
    body = JS[start:end]
    assert "research:" in body, "_felConfig must expose a research default"
    assert "content: false" in body, "research content must default OFF"
    assert "coding: false" in body, "research coding must default OFF"


def test_felblockhtml_stamps_research_toggles():
    start = JS.index("function felBlockHTML")
    end = JS.index("function _felEnsure")
    body = JS[start:end]
    for rid in RESEARCH_IDS:
        assert f"'#{rid}'" in body or f'"#{rid}"' in body or (
            f"'{rid}'" in body or f'"{rid}"' in body
        ), (
            f"felBlockHTML must stamp research toggle default: {rid}"
        )
    assert "setAttribute('checked'" in body, (
        "research toggle defaults must use setAttribute (innerHTML serialization rule)"
    )


def test_felonresearch_persists_only_research_key():
    start = JS.index("function _felConfig")
    end = JS.index("// --- PHASE 3 Observability JS")
    body = JS[start:end]
    assert "function felOnResearch" in body, "missing felOnResearch handler"
    # Persists the research sub-key (create-if-absent), never tools.fel itself.
    assert "if (!fel.research) fel.research = {};" in body
    assert "fel.research[lane] = cb.checked;" in body
    assert "scheduleAutoSave();" in body.split("function felOnResearch")[1]


def test_research_toggles_in_disabled_selectors():
    start = JS.index("function _felConfig")
    end = JS.index("// --- PHASE 3 Observability JS")
    body = JS[start:end]
    # Stamp site + both disabled/dimmed selectors (render-time and master
    # toggle) must carry the research ids.
    assert body.count("fel-research-content") >= 3, (
        "research ids must appear in stamping and both disabled selectors"
    )
    for rid in RESEARCH_IDS:
        assert rid in body
