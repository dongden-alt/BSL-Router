"""Offline metadata contracts; no inference, URLs, or referenced files opened."""
import base64
import copy
import hashlib
import json
import struct
import time
import zlib

import pytest

from app.middleware import image_delivery_trace as trace

RAW = b"bounded-image-bytes"
DATA = base64.b64encode(RAW).decode()
URI = "data:image/png;base64," + DATA
SECRET = "private-prompt-token-path-and-tool-name"


@pytest.fixture
def control(tmp_path, monkeypatch):
    monkeypatch.setattr(trace, "CONTROL", tmp_path / "control.json")
    monkeypatch.setattr(trace, "OUTPUT", tmp_path / "metadata.json")
    trace._records.clear()
    monkeypatch.setattr(trace, "_window", None)
    trace._context.set(None)

    def enable(**changes):
        now = time.time()
        cfg = {"enabled": True, "issued_at": now - 1, "expires_at": now + 60,
               "max_records": 32}
        cfg.update(changes)
        trace.CONTROL.write_text(json.dumps(cfg))
    return enable


def records():
    return json.loads(trace.OUTPUT.read_text())["records"]


def nested_image(data=DATA, mime="image/png"):
    return {"role": "tool", "fr": {"parts": [{"response": {"result": {
        "inlineData": {"mimeType": mime, "data": data}}}}]}}


def test_disabled_default(control):
    trace.trace("ingress", {"secret": SECRET}, begin=True)
    assert not trace.OUTPUT.exists()


@pytest.mark.parametrize("changes", [
    {"enabled": False}, {"expires_at": 0}, {"expires_at": lambda: time.time() + 901},
    {"issued_at": lambda: time.time() + 10}, {"max_records": 33}, {"max_records": True},
    {"expires_at": float("nan")}, {"expires_at": float("inf")},
    {"arm_next_media": 1}, {"watch_sha256": "g" * 64},
    {"watch_sha256": "a" * 63}, {"watch_sha256": True},
    *({"expected_dimensions": value} for value in (
        None, [], [720], [720, 540, 1], "720x540", {"width": 720, "height": 540},
        [True, 540], [720, False], [720.0, 540], [0, 540], [720, -1],
        [0x80000000, 540], [720, 0x80000000])),
])
def test_invalid_control(control, changes):
    resolved = {k: (v() if callable(v) else v) for k, v in changes.items()}
    control(**resolved)
    trace.trace("ingress", {}, begin=True)
    assert not trace.OUTPUT.exists()


def test_expiration_deletion_and_record_cap(control, monkeypatch):
    control(max_records=3)
    for _ in range(40):
        trace.trace("ingress", {}, begin=True)
    assert len(records()) == 3
    assert len({r["correlation"] for r in records()}) == 3
    monkeypatch.setattr(trace, "_deadline", time.monotonic() - 1)
    before = trace.OUTPUT.read_bytes()
    trace.trace("upstream", {})
    assert trace.OUTPUT.read_bytes() == before
    assert not trace._records
    trace.CONTROL.unlink()
    trace.trace("ingress", {}, begin=True)
    assert trace._context.get() is None


@pytest.mark.parametrize("role", ["user", "tool"])
@pytest.mark.parametrize("wire", ["gemini", "openai", "anthropic", "responses"])
def test_four_formats_user_and_tool(wire, role):
    image = {"type": "image_url", "image_url": {"url": URI}}
    if wire == "gemini":
        part = {"inlineData": {"mimeType": "image/png", "data": DATA}}
        if role == "tool":
            part = {"functionResponse": {"name": SECRET, "response": {"result": [part]}}}
        payload = {"request": {"contents": [{"role": "user", "parts": [part]}]}}
    elif wire == "openai":
        payload = {"messages": [{"role": role, "content": [image]}]}
    elif wire == "anthropic":
        part = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": DATA}}
        if role == "tool":
            part = {"type": "tool_result", "content": [part]}
        payload = {"messages": [{"role": "user", "content": [part]}]}
    else:
        part = {"type": "input_image", "image_url": URI}
        if role == "tool":
            part = {"type": "function_call_output", "output": json.dumps([part])}
        payload = {"input": [{"role": "user", "content": [part]}]}
    before = copy.deepcopy(payload)
    report = trace.summarize(payload)
    assert report["media_count"] + report["serialized_media_count"] == 1
    item = next(p for p in report["parts"] if p.get("status") == "decoded")
    assert item["decoded_bytes"] == len(RAW)
    assert item["sha256"] == hashlib.sha256(RAW).hexdigest()
    assert item["mime"] == "image/png"
    assert SECRET not in json.dumps(report)
    assert DATA not in json.dumps(report)
    assert payload == before


def test_absence_and_arbitrary_strings_redacted(control):
    control()
    payload = {SECRET: {"type": SECRET, "mimeType": SECRET, "data": SECRET},
               "messages": [{"content": SECRET}], "url": "file:///" + SECRET}
    trace.trace("ingress", payload, begin=True)
    trace.trace("upstream", {"messages": []})
    output = trace.OUTPUT.read_text()
    assert SECRET not in output and "file:///" not in output
    assert records()[1]["media_count"] == 0
    assert records()[0]["correlation"] == records()[1]["correlation"]
    assert records()[0]["reference_count"] == 1


@pytest.mark.parametrize("mime,data,status", [
    ("text/private", DATA, "invalid_mime"),
    ("image/png", "%%%", "invalid_base64"),
    ("image/png", None, "invalid_data"),
    ("image/png", "A" * (3 * trace.MAX_IMAGE_BYTES), "oversize"),
], ids=["mime", "base64", "missing", "oversize"])
def test_malformed_or_oversize(mime, data, status):
    report = trace.summarize({"inlineData": {"mimeType": mime, "data": data}})
    assert any(p.get("status") == status for p in report["parts"])
    assert not any("sha256" in p for p in report["parts"])


def test_traversal_depth_nodes_cycles_and_strings():
    root = current = {}
    for _ in range(100):
        current["result"] = {}
        current = current["result"]
    assert trace.summarize(root)["truncated"]
    report = trace.summarize([{} for _ in range(trace.MAX_NODES * 2)])
    assert report["truncated"] and report["nodes"] <= trace.MAX_NODES
    root = []
    root.append(root)
    assert trace.summarize(root)["nodes"] == 2
    assert trace.summarize("x" * (trace.MAX_BYTES + 1))["truncated"]
    assert trace.summarize("[broken")["media_count"] == 0


def test_parts_bounded():
    report = trace.summarize([{"type": "text"} for _ in range(300)])
    assert len(report["parts"]) == trace.MAX_PARTS
    assert report["truncated"]


def test_control_oversize_and_bad_json(control):
    for text in ("[", "x" * 1025, "[]", '{"enabled":true}'):
        trace.CONTROL.write_text(text)
        trace.trace("ingress", {}, begin=True)
        assert not trace.OUTPUT.exists()


def test_failure_is_silent_and_does_not_mutate(control, monkeypatch):
    control()
    monkeypatch.setattr(trace, "OUTPUT", trace.OUTPUT / "missing" / "out.json")
    payload = {"messages": []}
    trace.trace("ingress", payload, begin=True)
    assert payload == {"messages": []}


def test_gemini_converter_preserves_correlation_and_exposes_serialization(control):
    from app.compat.adapters.gemini import gemini_request_to_openai
    control()
    payload = {"contents": [{"role": "user", "parts": [{"functionResponse": {
        "name": "test_tool", "response": {"result": [{"inlineData": {
            "mimeType": "image/png", "data": DATA}}]}}}]}]}
    before = copy.deepcopy(payload)
    trace.trace("ingress", payload, begin=True)
    converted = gemini_request_to_openai(payload, "offline-test")
    result = records()
    assert [r["phase"] for r in result] == ["ingress", "adapter_before", "adapter_after"]
    assert len({r["correlation"] for r in result}) == 1
    assert result[1]["media_count"] == 1
    assert result[2]["media_count"] == 0
    assert result[2]["serialized_media_count"] == 1
    assert payload == before
    assert isinstance(converted["messages"][0]["content"], str)


def test_pydantic_canonical_view():
    from app.normalizer import UniversalNormalizer
    request = UniversalNormalizer.normalize_to_openai({"model": "offline-test", "messages": [
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": URI}}]}]})
    before = request.model_dump()
    assert trace.summarize(request)["media_count"] == 1
    assert request.model_dump() == before


def test_serialized_json_preparse_structure_limit():
    assert trace.summarize('[' * 100 + '0' + ']' * 100)["truncated"]
    assert trace.summarize('[' + ','.join('0' for _ in range(10000)) + ']')["truncated"]


def test_hard_record_ceiling(control):
    control()
    for _ in range(40):
        trace.trace("ingress", {}, begin=True)
    assert len(records()) == 32


def test_long_history_latest_media_and_schema_exclusion():
    payload = {"request": {"contents": [
        {"role": "user", "parts": [{"type": "text", "content": SECRET} for _ in range(8)]}
        for _ in range(600)] + [nested_image()],
        "tools": [{"schema": {SECRET: [SECRET] * 10000}}],
        "systemInstruction": {"parts": [nested_image()]}}}
    report = trace.summarize(payload)
    assert report["truncated"] and report["nodes"] <= trace.MAX_NODES
    latest = report["latest"]
    assert latest["complete"] and latest["tool_result"] and latest["media_count"] == 1
    assert not latest["no_media_in_inspected_subtree"]
    assert any(p.get("sha256") == hashlib.sha256(RAW).hexdigest() for p in report["parts"])
    assert report["schema_counts"]["fr"] == 1
    assert SECRET not in json.dumps(report) and "schema" not in report["schema_counts"]


def test_role_budget_cannot_starve_media():
    payload = [{"inlineData": {"mimeType": "image/png", "data": DATA}}] + [
        {"role": "user", "type": "text"} for _ in range(300)]
    report = trace.summarize(payload)
    assert len([p for p in report["parts"] if p["kind"] in ("role", "text")]) == trace.MAX_PARTS
    assert any("sha256" in p for p in report["parts"])
    assert len(report["parts"]) <= trace.MAX_PARTS + trace.MAX_MEDIA_PARTS


def test_latest_absence_is_local_and_budgeted():
    payload = {"messages": [nested_image()] + [{"role": "user", "content": "text"}] * 3000}
    report = trace.summarize(payload)
    assert report["truncated"] and report["latest"]["complete"]
    assert report["latest"]["no_media_in_inspected_subtree"]
    payload["messages"][-1] = {"role": "tool", "content": [{}] * 1000}
    latest = trace.summarize(payload)["latest"]
    assert latest["truncated"] and not latest["no_media_in_inspected_subtree"]
    assert latest["nodes"] <= trace.LATEST_NODES
    assert not trace.summarize({})["latest"]["no_media_in_inspected_subtree"]


def test_arm_capture_freezes_only_selected_correlation(control):
    control(arm_next_media=True)
    trace.trace("ingress", {"messages": [{"role": "user", "content": "text"}]}, begin=True)
    assert not trace.OUTPUT.exists()
    trace.trace("ingress", {"messages": [nested_image()]}, begin=True)
    selected = trace._context.get()
    doc = json.loads(trace.OUTPUT.read_text())
    assert doc["capture"]["selected_at"] >= trace._window[0]
    assert doc["capture"]["selected_correlation"] == selected
    trace.trace("ingress", {"messages": [nested_image()]}, begin=True)
    assert len(records()) == 1
    trace._context.set(selected)
    for phase in ("adapter_before", "adapter_after", "canonical", "vision_before", "vision_after", "upstream"):
        trace.trace(phase, {"messages": []})
    doc = json.loads(trace.OUTPUT.read_text())
    assert doc["capture"]["frozen"] and len(doc["records"]) == 7
    assert all(r["correlation"] == selected for r in records())
    assert [r["sequence"] for r in records()] == sorted(r["sequence"] for r in records())
    assert len(doc["groups"]) == 1 and len(doc["groups"][0]["phases"]) == 7
    frozen = trace.OUTPUT.read_bytes()
    for _ in range(40):
        trace.trace("ingress", {"messages": [nested_image()]}, begin=True)
    assert trace.OUTPUT.read_bytes() == frozen


@pytest.mark.parametrize("selectors", [
    {"watch_sha256": hashlib.sha256(RAW).hexdigest()},
    {"expected_dimensions": [720, 540]},
    {"watch_sha256": "b15a7676fdeeda28f63ea700f33ff6691e2ea50351b20081b19c4dde48f3622d",
     "expected_dimensions": [720, 540]},
])
@pytest.mark.parametrize("latest", [
    {"role": "tool", "content": "unrelated result"},
    nested_image(base64.b64encode(b"unrelated-image").decode()),
    {"role": "user", "content": [{"type": "image_url", "image_url": "https://invalid.test/image"}]},
])
def test_arm_selectors_reject_history_and_unrelated_latest(control, selectors, latest):
    control(arm_next_media=True, **selectors)
    ihdr = b"IHDR" + struct.pack(">IIBBBBB", 720, 540, 8, 2, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + ihdr + struct.pack(">I", zlib.crc32(ihdr))
    payload = {"messages": [nested_image(), nested_image(base64.b64encode(png).decode()), latest]}
    trace.trace("ingress", payload, begin=True)
    trace.trace("upstream", payload)
    assert trace._selected is None and not trace._frozen
    assert not trace._records and not trace.OUTPUT.exists()


@pytest.mark.parametrize("selectors", [
    {"watch_sha256": hashlib.sha256(RAW).hexdigest()},
    {"expected_dimensions": [720, 540]},
    {"watch_sha256": hashlib.sha256(RAW).hexdigest(), "expected_dimensions": [720, 540]},
])
def test_arm_selectors_wait_for_latest_subtree(control, selectors):
    control(arm_next_media=True, **selectors)
    trace.trace("ingress", nested_image(), begin=True)
    assert trace._selected is None and not trace.OUTPUT.exists()


def test_arm_rejects_valid_but_mismatched_latest_dimensions(control):
    control(arm_next_media=True,
            watch_sha256="b15a7676fdeeda28f63ea700f33ff6691e2ea50351b20081b19c4dde48f3622d",
            expected_dimensions=[720, 540])
    ihdr = b"IHDR" + struct.pack(">IIBBBBB", 640, 480, 8, 2, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + ihdr + struct.pack(">I", zlib.crc32(ihdr))
    trace.trace("ingress", {"messages": [nested_image(base64.b64encode(png).decode())]}, begin=True)
    assert trace._selected is None and not trace.OUTPUT.exists()


@pytest.mark.parametrize("serialized", [False, True])
def test_arm_transformed_jpeg_matches_dimensions_and_freezes(control, serialized):
    control(arm_next_media=True,
            watch_sha256="b15a7676fdeeda28f63ea700f33ff6691e2ea50351b20081b19c4dde48f3622d",
            expected_dimensions=[720, 540])
    sof = struct.pack(">BHHB", 8, 540, 720, 3) + b"\x01\x11\x00\x02\x11\x00\x03\x11\x00"
    jpeg = b"\xff\xd8\xff\xc0" + struct.pack(">H", len(sof) + 2) + sof
    latest = nested_image(base64.b64encode(jpeg).decode(), "image/jpeg")
    if serialized:
        latest = {"role": "tool", "content": json.dumps(latest)}
    payload = {"messages": [latest]}
    trace.trace("ingress", payload, begin=True)
    selected = trace._context.get()
    assert trace._selected == selected
    doc = json.loads(trace.OUTPUT.read_text())
    assert not doc["capture"]["frozen"] and not doc["groups"][0]["watch_match"]
    item = next(p for p in records()[0]["latest"]["parts"] if p.get("status") == "decoded")
    assert (item["width"], item["height"], item["header_format"]) == (720, 540, "jpeg")
    trace.trace("ingress", payload, begin=True)
    trace.trace("upstream", payload)
    assert not trace._frozen and len(records()) == 1
    trace._context.set(selected)
    for phase in ("adapter_before", "adapter_after", "canonical", "vision_before", "vision_after", "upstream"):
        trace.trace(phase, {"messages": []})
    doc = json.loads(trace.OUTPUT.read_text())
    assert doc["capture"]["frozen"] and len(doc["records"]) == 7
    assert {r["correlation"] for r in doc["records"]} == {selected}
    frozen = trace.OUTPUT.read_bytes()
    trace.trace("ingress", payload, begin=True)
    assert trace.OUTPUT.read_bytes() == frozen


@pytest.mark.parametrize("dimensions", [None, [720, 540]])
def test_arm_known_sha_selects_even_without_matching_dimensions(control, dimensions):
    selectors = {"watch_sha256": hashlib.sha256(RAW).hexdigest().upper()}
    if dimensions is not None:
        selectors["expected_dimensions"] = dimensions
    control(arm_next_media=True, **selectors)
    trace.trace("ingress", {"messages": [nested_image()]}, begin=True)
    assert trace._selected == trace._context.get()
    assert json.loads(trace.OUTPUT.read_text())["groups"][0]["watch_match"]
    trace.trace("upstream", {})
    assert trace._frozen


def test_dimensions_change_resets_same_control_window(control):
    control(arm_next_media=True, expected_dimensions=[720, 540],
            watch_sha256=hashlib.sha256(RAW).hexdigest())
    trace.trace("ingress", {"messages": [nested_image()]}, begin=True)
    trace.trace("upstream", {})
    assert trace._frozen
    cfg = json.loads(trace.CONTROL.read_text())
    cfg["expected_dimensions"] = [640, 480]
    trace.CONTROL.write_text(json.dumps(cfg))
    trace.trace("ingress", {"messages": [{"role": "tool", "content": "unrelated"}]}, begin=True)
    assert trace._window[-1] == (640, 480)
    assert trace._selected is None and not trace._frozen and not trace._records


def test_arm_rejects_request_started_before_new_window(control):
    control()
    trace.trace("ingress", {}, begin=True)
    control(arm_next_media=True)
    trace.trace("canonical", {"messages": [nested_image()]})
    assert trace._selected is None
    trace.trace("ingress", {"messages": [nested_image()]}, begin=True)
    assert trace._selected is not None


def test_upstream_write_failure_does_not_freeze_before_snapshot(control, monkeypatch):
    control(arm_next_media=True)
    trace.trace("ingress", {"messages": [nested_image()]}, begin=True)
    original = trace.os.replace

    def fail(*args):
        raise OSError("offline")

    monkeypatch.setattr(trace.os, "replace", fail)
    trace.trace("upstream", {})
    assert not trace._frozen
    monkeypatch.setattr(trace.os, "replace", original)
    trace.trace("upstream", {})
    assert trace._frozen and json.loads(trace.OUTPUT.read_text())["capture"]["frozen"]


def test_arm_fallback_latest_tool_result(control):
    control(arm_next_media=True)
    trace.trace("ingress", {"messages": [{"role": "tool", "content": "text"}]}, begin=True)
    assert len(records()) == 1
    trace.trace("upstream", {})
    assert json.loads(trace.OUTPUT.read_text())["capture"]["frozen"]


def test_watch_pins_group_and_keeps_other_images(control):
    control(watch_sha256=hashlib.sha256(RAW).hexdigest().upper())
    trace.trace("ingress", {"messages": [nested_image()]}, begin=True)
    selected = trace._context.get()
    trace.trace("canonical", {})
    for _ in range(50):
        trace.trace("ingress", {}, begin=True)
    assert {r["phase"] for r in records() if r["correlation"] == selected} == {"ingress", "canonical"}
    other = base64.b64encode(b"different-image").decode()
    trace.trace("ingress", {"messages": [nested_image(other)]}, begin=True)
    assert any(p.get("sha256") == hashlib.sha256(b"different-image").hexdigest()
               for r in records() for p in r["parts"])
    assert len(records()) <= 32


def test_recent_media_group_survives_empty_requests(control):
    control()
    trace.trace("ingress", {"messages": [nested_image()]}, begin=True)
    selected = trace._context.get()
    trace.trace("adapter_after", {})
    for _ in range(50):
        trace.trace("ingress", {}, begin=True)
    assert len([r for r in records() if r["correlation"] == selected]) == 2


def test_dimensions_validated_png_jpeg_and_bounded_headers():
    ihdr = b"IHDR" + struct.pack(">IIBBBBB", 720, 540, 8, 2, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + ihdr + struct.pack(">I", zlib.crc32(ihdr))
    sof = struct.pack(">BHHB", 8, 540, 720, 3) + b"\x01\x11\x00\x02\x11\x00\x03\x11\x00"
    jpeg = b"\xff\xd8\xff\xc0" + struct.pack(">H", len(sof) + 2) + sof
    for raw, fmt, mime in ((png, "png", "image/png"), (jpeg, "jpeg", "image/jpeg")):
        report = trace.summarize({"messages": [nested_image(base64.b64encode(raw).decode(), mime)]})
        item = next(p for p in report["parts"] if p.get("status") == "decoded")
        assert (item["width"], item["height"], item["header_format"]) == (720, 540, fmt)
    assert trace._dimensions(png[:-1]) == {}
    assert trace._dimensions(png[:-1] + bytes([png[-1] ^ 1])) == {}
    assert trace._dimensions(jpeg[:12]) == {}
    assert trace._dimensions(b"\xff\xd8\xff\xe1\xff\xff" + b"x" * 65533 + jpeg[2:]) == {}


def test_atomic_concurrent_reads_and_writes(control):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    control()
    trace.trace("ingress", {}, begin=True)
    stop = threading.Event()
    errors, reads = [], []

    def reader():
        while not stop.is_set():
            try:
                doc = json.loads(trace.OUTPUT.read_text())
                assert 1 <= len(doc["records"]) <= 32
                reads.append(doc["records"][-1]["sequence"])
            except PermissionError:
                # Windows may deny a concurrent open during atomic replacement.
                # A successful open must always contain a complete document.
                continue
            except Exception as error:
                errors.append(type(error).__name__)

    worker = threading.Thread(target=reader)
    worker.start()
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: trace.trace("ingress", {}, begin=True), range(60)))
    finally:
        stop.set()
        worker.join(timeout=5)
    assert reads and not errors
    assert not list(trace.OUTPUT.parent.glob(trace.OUTPUT.name + ".*.tmp"))
    assert len(records()) == 32


def test_atomic_replace_failure_cleans_temporary(control, monkeypatch):
    control()
    trace.trace("ingress", {}, begin=True)
    previous = trace.OUTPUT.read_bytes()

    def fail(*args):
        raise OSError("offline replace failure")

    monkeypatch.setattr(trace.os, "replace", fail)
    trace.trace("upstream", {})
    assert trace.OUTPUT.read_bytes() == previous
    assert not list(trace.OUTPUT.parent.glob(trace.OUTPUT.name + ".*.tmp"))


def test_frozen_capture_expiry_and_disabled_clear_state(control, monkeypatch):
    control(arm_next_media=True)
    trace.trace("ingress", {"messages": [nested_image()]}, begin=True)
    trace.trace("upstream", {})
    previous = trace.OUTPUT.read_bytes()
    monkeypatch.setattr(trace, "_deadline", time.monotonic() - 1)
    trace.trace("ingress", {}, begin=True)
    assert not trace._records and not trace._pinned and trace._selected is None
    assert trace.OUTPUT.read_bytes() == previous
    trace.CONTROL.unlink()
    trace.trace("ingress", {}, begin=True)
    assert trace._context.get() is None
