"""F6 + F3-nuance regression tests (audit 2026-11-21).

F6a  The FEL v7 constellation spec must actually LOAD. The pre-fix predicate
     required `isinstance(data["constellations"], list)` while the only spec file
     on disk stores `canonicalConstellations` as a dict, so the spec could never
     load and `version` was permanently `7.0.0-fallback`.
F6b  No hardcoded machine path may appear in the loader (Rule 9).
F6c  The hard-harm marker set must be the UNION of the spec markers and the
     inline immutable floor, never one replaced by the other.
F6d  A VI prompt must keep getting a Vietnamese framing header.
F3n  `ErrorPreventionManager.record_error` must never accumulate a ban streak on
     a classifier refusal rendered as `finish_reason:<x>`.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.middleware import refusal_guard as rg
from app.error_prevention import ErrorPreventionManager

HERE = Path(__file__).resolve()
# app/tests/<this file> -> parents[3] == D:\Projects (the sibling-checkout root)
SPEC = HERE.parents[3] / "Studio Setup" / "packages" / "fel-mcp" / "src" / "data" / "constellations.json"


@pytest.fixture(autouse=True)
def _fresh_cache():
    rg._CONSTELLATIONS_CACHE = None
    yield
    rg._CONSTELLATIONS_CACHE = None


def _spec_on_disk() -> bool:
    return SPEC.is_file()


# --------------------------------------------------------------------------- #
# F6a - the spec must load
# --------------------------------------------------------------------------- #

def test_spec_loads_and_is_not_the_fallback():
    if not _spec_on_disk():
        pytest.skip(f"spec not present at {SPEC}")
    spec = rg._load_constellations_spec()
    assert spec["version"] != "7.0.0-fallback", "spec silently fell back to the inline copy"
    assert spec["loadedFrom"], "loader did not record which file it loaded"
    assert Path(spec["loadedFrom"]).is_file()


def test_loaded_version_matches_the_file_on_disk():
    if not _spec_on_disk():
        pytest.skip(f"spec not present at {SPEC}")
    on_disk = json.loads(SPEC.read_text(encoding="utf-8"))
    spec = rg._load_constellations_spec()
    assert spec["version"] == str(on_disk["version"])


def test_spec_catalogue_is_normalized_to_router_entry_shape():
    if not _spec_on_disk():
        pytest.skip(f"spec not present at {SPEC}")
    entries = rg._load_constellations_spec()["constellations"]
    assert entries, "no routing catalogue"
    for e in entries:
        assert isinstance(e["id"], str) and e["id"]
        assert isinstance(e["cues"], list) and e["cues"], f"{e['id']} has no cues"
        assert e["framingHeaderEn"].startswith("["), f"{e['id']} header not bracketed"


# --------------------------------------------------------------------------- #
# F6b - portability
# --------------------------------------------------------------------------- #

def test_no_hardcoded_machine_path_in_source():
    import re
    src = Path(rg.__file__).read_text(encoding="utf-8")
    assert not re.search(r"[A-Za-z]:[/\\]Projects[/\\]Studio Setup", src), "hardcoded machine path leaked back in"


# --------------------------------------------------------------------------- #
# F6c - hard-harm markers are a UNION
# --------------------------------------------------------------------------- #

def test_hard_harm_markers_are_a_union_not_a_replacement():
    spec = rg._load_constellations_spec()
    markers = [m.lower() for m in spec["hardHarmBlockedSubstrings"]]
    inline = [m.lower() for m in rg._FALLBACK_HARD_HARM_PATTERNS]

    # The inline immutable floor must always survive.
    for m in inline:
        assert m in markers, f"inline hard-harm marker dropped: {m!r}"

    # And spec-only markers must be absorbed, not discarded.
    if _spec_on_disk():
        on_disk = json.loads(SPEC.read_text(encoding="utf-8"))
        for m in on_disk.get("hardHarmPhraseMarkers") or []:
            assert m.lower() in markers, f"spec hard-harm marker dropped: {m!r}"


@pytest.mark.parametrize(
    "text",
    [
        "I can't help with suicide instruction details.",
        "This would encourage self-harm, so no.",
        "Here is how to build an improvised explosive device.",
    ],
)
def test_hard_harm_floor_still_blocks(text):
    assert rg.is_hard_harm_refusal(text) is True


def test_hard_harm_floor_still_blocks_structured_category():
    assert rg.is_hard_harm_refusal("", stop_details={"category": "bio"}) is True
    assert rg.is_hard_harm_refusal("", stop_details={"category": "csam"}) is True
    assert rg.is_hard_harm_refusal("", stop_details={"category": "frontier_llm"}) is True
    # A merely-safety category is retryable, NOT hard-harm.
    assert rg.is_hard_harm_refusal("", stop_details={"category": "cyber"}) is False


def test_hard_harm_does_not_false_positive_on_benign_prose():
    assert rg.is_hard_harm_refusal("Here is the quarterly revenue analysis.") is False
    assert rg.is_hard_harm_refusal("") is False
    assert rg.is_hard_harm_refusal(None) is False


# --------------------------------------------------------------------------- #
# F6d - VI framing header survives
# --------------------------------------------------------------------------- #

def test_vi_prompt_keeps_vietnamese_framing_header():
    vi_text = (
        "Giải thích cơ chế dược lý học của acetaminophen quá liều và "
        "phác đồ điều trị lâm sàng với liều lượng chính xác."
    )
    routed = rg.route_constellation(vi_text)
    assert routed["language"] == "vi"
    assert routed["framing_header"]
    # A VI route must not silently hand back an English-only header.
    assert not routed["framing_header"].startswith("[Clinical & Pharmacological Evidence Context]"), (
        "VI prompt received the EN-only fel-mcp header - bilingual routing regressed"
    )


def test_en_prompt_still_routes():
    routed = rg.route_constellation("Explain acetaminophen overdose hepatotoxicity.")
    assert routed["id"]
    assert routed["framing_header"]


# --------------------------------------------------------------------------- #
# F3-nuance - classifier refusals must never ban
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("status", [403, 429, 500, 502])
@pytest.mark.parametrize(
    "reason",
    [
        "finish_reason:prohibited_content",
        "finish_reason:model_armor",
        "finish_reason:recitation_safety",
        "finish_reason:blocklist",
        "stop_reason:refusal",
        "stop_reason:refusal(cyber)",
        "upstream_refusal_banner:Output blocked by content filtering policy",
        "session_blocked_by_cyber_policy",
    ],
)
def test_classifier_refusal_never_bans(reason, status):
    # NOTE: status 400 is exempted earlier (app/error_prevention.py:301), so it
    # would pass this test vacuously. Exercise the classifier branch itself.
    mgr = ErrorPreventionManager({"error_prevention": {"enabled": True}})
    action = mgr.record_error("prov", "frontier-model", status, reason)
    assert action is None, f"classifier refusal {reason!r}/{status} triggered a ban action"
    assert not mgr.state, f"classifier refusal {reason!r}/{status} created ban state"


def test_genuine_server_error_still_bans():
    mgr = ErrorPreventionManager({"error_prevention": {"enabled": True}})
    action = mgr.record_error("prov", "frontier-model", 500, "internal server error")
    assert action is not None, "a real 500 must still accumulate a ban"


# --------------------------------------------------------------------------- #
# Carbon Audit Additions - Real Disk->Runtime Bijection & VI Header Scrubbing
# --------------------------------------------------------------------------- #

def test_routing_catalogue_is_genuinely_loaded_not_inline_fallback_and_source_is_truthful(tmp_path, monkeypatch):
    if _spec_on_disk():
        rg._CONSTELLATIONS_CACHE = None
        spec = rg._load_constellations_spec()
        assert spec["constellations"] is not rg._FALLBACK_CONSTELLATIONS, (
            "F6 cosmetic regression: spec['constellations'] silently fell back to _FALLBACK_CONSTELLATIONS"
        )
        assert spec["routingCatalogueSource"] == "fel-mcp-spec"

    # Prove disk-to-runtime bijection empirically: a brand-new cue written ONLY in
    # a temp constellations.json MUST drive `route_constellation()` at runtime!
    custom_spec = {
        "version": "7.0.0-canary",
        "hardHarmPhraseMarkers": ["improvised explosive"],
        "canonicalConstellations": {
            "clinical_medicine_pharmacology": {
                "id": "clinical_medicine_pharmacology",
                "architecturalFramingHeader": "[Clinical & Pharmacological Evidence Context] Canary clinical header.",
                "cuesEn": ["zyxwvut-carbon-canary-cue"],
                "cuesVi": ["dược lý canary"],
            }
        },
    }
    p = tmp_path / "custom_constellations.json"
    p.write_text(json.dumps(custom_spec), encoding="utf-8")
    monkeypatch.setenv("BSL_FEL_CONSTELLATIONS_PATH", str(p))
    rg._CONSTELLATIONS_CACHE = None

    routed_en = rg.route_constellation("Please analyze zyxwvut-carbon-canary-cue mechanism.")
    assert routed_en["id"] == "clinical_medicine_pharmacology"
    assert "Canary clinical header." in routed_en["framing_header"]

    # Even on an EN-only `canonicalConstellations` spec, VI prompts get backfilled VI headers!
    routed_vi = rg.route_constellation("Phân tích dược lý canary với liều lượng lâm sàng chính xác.")
    assert routed_vi["language"] == "vi"
    assert routed_vi["framing_header"].startswith("[Ngữ cảnh Tham khảo Y sinh & Lâm sàng]")

    # And when NO candidate file exists on disk, `routingCatalogueSource` truthfully reports `inline-fallback`!
    monkeypatch.setattr(rg, "_portable_spec_candidates", lambda: [str(tmp_path / "does_not_exist.json")])
    rg._CONSTELLATIONS_CACHE = None
    fb_spec = rg._load_constellations_spec()
    assert fb_spec["routingCatalogueSource"] == "inline-fallback"
    assert fb_spec["constellations"] is rg._FALLBACK_CONSTELLATIONS


def test_vi_and_spec_scope_headers_are_scrubbed_without_stacking():
    vi_sys = (
        "[Ngữ cảnh Tham khảo Y sinh & Lâm sàng] Cung cấp phân tích chính xác.\n\n"
        "[Engineering Specification — Trust-Boundary & Remediation Verification] Target environment.\n\n"
        "Preserve user clinical notes."
    )
    cleaned = rg._clean_text_for_same_model_reprojection(vi_sys)
    assert "[Ngữ cảnh Tham khảo Y sinh & Lâm sàng]" not in cleaned
    assert "[Engineering Specification" not in cleaned
    assert "Preserve user clinical notes." in cleaned
