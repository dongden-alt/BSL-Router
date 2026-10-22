"""Unit tests for scripts/run_full_depth.py — NO NETWORK, no live router.

Covers the spec-mandated invariants:
  * needle hit logic searches content + reasoning_content (reasoning-only and
    content-only both count as hits);
  * PLAN constant shape (family/tier/pinned_model present everywhere; GPT has
    no double source; Kimi/GLM carry gold+double);
  * ceiling escalation stop logic (hit to 128K, miss at 256K -> ceiling=128K,
    no 512K/1M call made);
  * provider-pinned model field on the wire (never a bare model id);
  * retries=1 / serial-per-provider / timeout=180 / cooldown invariants.

Fake needle callers extract the 8-char code from the prompt via regex (the
runner generates a random code per doc, so precomputed doc comparison would
never match) and tier hits by prompt LENGTH — the ladder docs are
deterministic in size (n_chars + insertion), so length identifies the rung.
"""
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

rfd = __import__("scripts.run_full_depth", fromlist=["run_full_depth"])

NEEDLE_CODE_RE = re.compile(r"The secret code is ([A-Z2-9]{8})\.")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def flat_result(content="", reasoning_content="", ok=True, error="", ptok=None, ctok=None):
    """Caller-result shape (what needle_hit / the ladder consume)."""
    return {"ok": ok, "content": content, "reasoning_content": reasoning_content,
            "usage": ({} if ptok is None and ctok is None
                      else {"prompt_tokens": ptok, "completion_tokens": ctok}),
            "error": error, "attempts": 1}


class FakeUrllibResponse:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


def fake_urlopen(resp_payload):
    """urlopen double returning a canned OpenAI-shaped chat response."""
    calls = []

    def _urlopen(req, timeout=None):
        calls.append({"req": req,
                      "body": json.loads(req.data.decode("utf-8")),
                      "timeout": timeout})
        return FakeUrllibResponse(resp_payload)

    return _urlopen, calls


def make_api_response(content="", reasoning_content="", ptok=42, ctok=7, model="x/y"):
    """Raw router response shape (what urlopen returns)."""
    return {"choices": [{"finish_reason": "stop",
                         "message": {"content": content,
                                     "reasoning_content": reasoning_content}}],
            "usage": {"prompt_tokens": ptok, "completion_tokens": ctok},
            "model": model}


class WithTmpOut(unittest.TestCase):
    """tmp output path shared by the orchestration/persistence tests."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="full-depth-test-")
        self.out_path = os.path.join(self.tmpdir, "full_depth_unified.json")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 1. reasoning_content-aware needle hit logic
# ---------------------------------------------------------------------------
class TestNeedleHit(unittest.TestCase):
    CODE = "ABCD1234"

    def test_content_only_needle_counts_as_hit(self):
        r = flat_result(content="The secret code is ABCD1234.")
        self.assertTrue(rfd.needle_hit(r, self.CODE))

    def test_reasoning_only_needle_counts_as_hit(self):
        r = flat_result(content="", reasoning_content="...secret... ABCD1234 ...verify")
        self.assertTrue(rfd.needle_hit(r, self.CODE))

    def test_needle_across_content_and_reasoning_boundary(self):
        # code split across the two fields still matches on the concatenation
        r = flat_result(content="...prefix AB", reasoning_content="CD1234 suffix...")
        self.assertTrue(rfd.needle_hit(r, self.CODE))

    def test_needle_absent_is_not_a_hit(self):
        r = flat_result(content="I could not find any code.", reasoning_content="thinking...")
        self.assertFalse(rfd.needle_hit(r, self.CODE))

    def test_empty_or_missing_fields_are_safe(self):
        self.assertFalse(rfd.needle_hit({}, self.CODE))
        self.assertFalse(rfd.needle_hit(None, self.CODE))
        self.assertFalse(rfd.needle_hit(flat_result(), ""))
        r = flat_result(content=None, reasoning_content=None)
        self.assertFalse(rfd.needle_hit(r, self.CODE))

    def test_failed_call_is_not_a_hit_even_with_leftover_content(self):
        r = flat_result(content="", reasoning_content="", ok=False, error="HTTP 500")
        self.assertFalse(rfd.needle_hit(r, self.CODE))

    def test_ladder_records_reasoning_only_hits(self):
        """End-to-end within run_needle_ladder: a rung answered ONLY inside
        reasoning_content (16K) still lifts the ceiling to 16K."""
        seen_rungs = []

        def call(model, prompt, max_tokens=600):
            if "What is the secret code?" not in prompt:
                return flat_result(content="cal", ptok=5)
            body = prompt.rsplit("\n\nWhat is the secret code?", 1)[0]
            m = NEEDLE_CODE_RE.search(body)
            seen_rungs.append(len(body))
            if m and len(body) < 100000:
                # needle answered ONLY in reasoning_content (16K rung)
                return flat_result(reasoning_content=f"let me think... {m.group(1)}", ctok=3)
            return flat_result(content="no idea", ctok=3)

        res = rfd.run_needle_ladder(call, "prov/model-x")
        self.assertEqual(res["ceiling"], "16K")
        self.assertTrue(res["needles"]["16K"]["hit"])
        self.assertFalse(res["needles"]["64K"]["hit"])
        # miss at 256K (ok, no hit) is the >=256K stop point — nothing beyond
        self.assertIn("256K", res["needles"])
        self.assertNotIn("512K", res["needles"])
        self.assertNotIn("1M", res["needles"])
        # exactly four rungs attempted: 16K, 64K, 128K, 256K — each distinctly
        # sized, in ladder order (doc length < 128K rung size marks the 16K hit)
        self.assertEqual(len(seen_rungs), 4)
        self.assertLess(seen_rungs[0], 100000)
        self.assertGreater(seen_rungs[1], 200000)
        self.assertGreater(seen_rungs[2], 400000)
        self.assertGreater(seen_rungs[3], 900000)


# ---------------------------------------------------------------------------
# 2. PLAN constant shape
# ---------------------------------------------------------------------------
class TestPlanShape(unittest.TestCase):
    KNOWN_PROVIDERS = {"codex", "mintrouter", "antigravity", "aibox", "chat2api"}

    def test_every_entry_has_required_fields(self):
        for i, e in enumerate(rfd.PLAN):
            for field in ("family", "tier", "pinned_model"):
                self.assertIn(field, e, f"PLAN[{i}] missing {field}")
            self.assertIn(e["family"], ("gpt", "gemini", "kimi", "glm"),
                          f"PLAN[{i}] unexpected family {e['family']}")
            # provider-pinned: provider/model with a non-empty provider prefix
            pinned = e["pinned_model"]
            self.assertTrue("/" in pinned, f"PLAN[{i}] not provider-pinned: {pinned}")
            provider = pinned.split("/", 1)[0]
            self.assertIn(provider, self.KNOWN_PROVIDERS,
                          f"PLAN[{i}] unknown provider prefix: {pinned}")

    def test_gpt_has_no_double_tier(self):
        tiers = {e["tier"] for e in rfd.PLAN if e["family"] == "gpt"}
        self.assertNotIn("double", tiers)
        self.assertNotIn("depth", tiers)

    def test_gpt_gold_is_codex_only(self):
        golds = [e for e in rfd.PLAN if e["family"] == "gpt" and e["tier"] == "gold"]
        self.assertEqual(len(golds), 5)
        for e in golds:
            self.assertTrue(e["pinned_model"].startswith("codex/"),
                            f"GPT gold must be codex/*, got {e['pinned_model']}")
        self.assertEqual([e["pinned_model"] for e in golds],
                         ["codex/gpt-5.6-sol", "codex/gpt-5.6-terra", "codex/gpt-5.6-luna",
                          "codex/gpt-5.5", "codex/gpt-6-astra"])

    def test_gpt_edge_fallback_is_conditional_and_mintrouter(self):
        fb = [e for e in rfd.PLAN if e.get("tier") == "edge-fallback"]
        self.assertEqual(len(fb), 1)
        self.assertEqual(fb[0]["pinned_model"], "mintrouter/gpt-6.1-sol")
        self.assertTrue(fb[0].get("conditional"))

    def test_kimi_has_gold_double_depth(self):
        by_tier = {e["tier"]: e["pinned_model"] for e in rfd.PLAN if e["family"] == "kimi"}
        self.assertEqual(by_tier.get("gold"), "aibox/kimi/kimi-k3")
        self.assertEqual(by_tier.get("double"), "aibox/kimi-k3")
        self.assertEqual(by_tier.get("depth"), "chat2api/workbuddy-web/kimi-k3")

    def test_glm_has_gold_double_depth(self):
        by_tier = {e["tier"]: e["pinned_model"] for e in rfd.PLAN if e["family"] == "glm"}
        self.assertEqual(by_tier.get("gold"), "aibox/ZHIPU/GLM-5.3")
        self.assertEqual(by_tier.get("double"), "mintrouter/glm-5.3")
        self.assertEqual(by_tier.get("depth"), "chat2api/zai-web1/glm-5.3-flash-max")

    def test_glm_gold_uses_plain_5_3_not_prime(self):
        golds = [e["pinned_model"] for e in rfd.PLAN
                 if e["family"] == "glm" and e["tier"] == "gold"]
        self.assertEqual(golds, ["aibox/ZHIPU/GLM-5.3"])

    def test_gemini_reference_models(self):
        refs = [e["pinned_model"] for e in rfd.PLAN if e["family"] == "gemini"]
        self.assertEqual(refs, ["antigravity/gemini-4-argon-high",
                                "antigravity/gemini-4-argon-medium",
                                "antigravity/gemini-4-argon-low",
                                "antigravity/gemini-2.5-pro"])
        for r in refs:
            self.assertTrue(r.startswith("antigravity/"))

    def test_plan_covers_exactly_four_families(self):
        self.assertEqual({e["family"] for e in rfd.PLAN}, {"gpt", "gemini", "kimi", "glm"})

    def test_canonical_prompt_constants_untouched(self):
        # verbatim reuse requirement — pin the exact canonical strings
        self.assertEqual(
            rfd.CAL,
            ("Transform the following text exactly as instructed. Text: "
             "机器学习模型 transformer 架构中的 attention mechanism 通过 query-key-value 三元组计算权重。"
             "def softmax(x): return [math.exp(i)/sum(map(math.exp,x)) for i in x] "
             "The quick brown fox jumps over 13 lazy dogs in 2025年. Repeat it once verbatim, then translate to French."))
        self.assertEqual(
            rfd.CAP["manacher"],
            "Implement Manacher's algorithm for longest palindromic substring in Python. Code only.")
        self.assertEqual(
            rfd.CAP["math_multistep"],
            "Compute (37*417) + (2048/16) - (13^3). Show steps, end with 'ANSWER: <n>'.")
        self.assertEqual(
            rfd.CAP["logic_puzzle"],
            "Three boxes: apples, oranges, mixed. All labels wrong. You pick one fruit from the box labeled 'mixed' and it's an apple. Relabel correctly. Explain briefly.")

    def test_ladder_rungs_and_sizes(self):
        self.assertEqual([t for t, _c in rfd.LADDER],
                         ["16K", "64K", "128K", "256K", "512K", "1M"])
        self.assertEqual([c for _t, c in rfd.LADDER],
                         [16000 * 4, 64000 * 4, 128000 * 4, 256000 * 4,
                          512000 * 4, 1000000 * 4])


# ---------------------------------------------------------------------------
# 3. Ceiling-escalation stop logic (the spec scenario)
# ---------------------------------------------------------------------------
class TestCeilingStop(unittest.TestCase):
    def test_hit_until_128k_miss_at_256k_stops_no_512k_or_1m(self):
        seen_sizes = []

        def call(model, prompt, max_tokens=600):
            if "What is the secret code?" not in prompt:
                return flat_result(content="cal", ptok=5)
            body = prompt.rsplit("\n\nWhat is the secret code?", 1)[0]
            seen_sizes.append(len(body))
            m = NEEDLE_CODE_RE.search(body)
            if m and len(body) <= 128000 * 4 + 1000:
                # 16K / 64K / 128K hit in content
                return flat_result(content=m.group(1), ctok=8)
            return flat_result(content="I don't see a code", ctok=8)

        res = rfd.run_needle_ladder(call, "prov/model-y")
        self.assertEqual(res["ceiling"], "128K")
        # exactly four rungs attempted: 16K, 64K, 128K, 256K (each distinctly
        # sized and in ladder order) — and nothing at 512K/1M
        self.assertEqual(len(seen_sizes), 4)
        self.assertLess(seen_sizes[0], 100000)
        self.assertGreater(seen_sizes[1], 200000)
        self.assertGreater(seen_sizes[2], 400000)
        self.assertGreater(seen_sizes[3], 900000)
        self.assertNotIn("512K", res["needles"])
        self.assertNotIn("1M", res["needles"])
        self.assertFalse(res["quota_blocked"])

    def test_miss_below_256k_does_not_stop_ladder(self):
        seen_sizes = []

        def call(model, prompt, max_tokens=600):
            if "What is the secret code?" not in prompt:
                return flat_result(content="cal", ptok=5)
            body = prompt.rsplit("\n\nWhat is the secret code?", 1)[0]
            seen_sizes.append(len(body))
            m = NEEDLE_CODE_RE.search(body)
            if m and len(body) <= 256000 * 4 + 1000:
                return flat_result(content=m.group(1), ctok=8)
            return flat_result(content="miss", ctok=8)

        res = rfd.run_needle_ladder(call, "prov/model-z")
        self.assertEqual(res["ceiling"], "256K")
        # miss at 64K does not stop; the miss at 512K (>=256K) does
        self.assertEqual(len(seen_sizes), 5)  # up to and including 512K
        self.assertNotIn("1M", res["needles"])

    def test_error_stops_ladder_without_counting_as_miss(self):
        def call(model, prompt, max_tokens=600):
            if "What is the secret code?" not in prompt:
                return flat_result(content="cal", ptok=5)
            body = prompt.rsplit("\n\nWhat is the secret code?", 1)[0]
            m = NEEDLE_CODE_RE.search(body)
            if m and len(body) <= 16000 * 4 + 1000:
                return flat_result(content=m.group(1), ctok=8)
            return flat_result(ok=False, error="HTTP 403")

        res = rfd.run_needle_ladder(call, "prov/model-e")
        self.assertEqual(res["ceiling"], "16K")
        self.assertTrue(res["needles"]["16K"]["hit"])
        self.assertFalse(res["needles"]["64K"]["ok"])
        self.assertTrue(res["quota_blocked"])
        self.assertNotIn("512K", res["needles"])
        self.assertNotIn("1M", res["needles"])


# ---------------------------------------------------------------------------
# 4. Wire invariants — provider-pinned model, retries=1, serial, timeout
# ---------------------------------------------------------------------------
class TestWireInvariants(WithTmpOut):
    def test_wire_body_is_provider_pinned_and_fixed_params(self):
        payload = make_api_response(content="ok", ptok=10)
        urlopen, calls = fake_urlopen(payload)
        with mock.patch.object(rfd.urllib.request, "urlopen", urlopen):
            caller = rfd.make_caller(router_url="http://x/v1/chat/completions",
                                     api_key="k", cooldown=0, timeout=180)
            caller("prov/model-a", "hi")
        self.assertEqual(len(calls), 1)
        b = calls[0]["body"]
        self.assertEqual(b["model"], "prov/model-a")  # pinned, never bare
        self.assertIn("/", b["model"])
        self.assertEqual(b["stream"], False)
        self.assertEqual(b["temperature"], 0.0)
        self.assertEqual(b["max_tokens"], 600)
        self.assertEqual(b["messages"], [{"role": "user", "content": "hi"}])
        self.assertEqual(calls[0]["req"].headers.get("Authorization"), "Bearer k")

    def test_retries_is_one_so_http_error_makes_single_attempt(self):
        urlopen, calls = fake_urlopen({})
        calls.clear()

        def _u(req, timeout=None):
            calls.append(1)
            raise rfd.urllib.error.HTTPError("http://x", 500, "boom", hdrs=None, fp=None)

        with mock.patch.object(rfd.urllib.request, "urlopen", _u):
            caller = rfd.make_caller(router_url="http://x", api_key="k",
                                     cooldown=0, timeout=180)
            r = caller("prov/model-b", "hi")
        self.assertEqual(len(calls), 1)  # no storm
        self.assertFalse(r["ok"])
        self.assertEqual(r["attempts"], 1)
        self.assertEqual(r["error"], "HTTP 500")

    def test_caller_invariant_defaults(self):
        self.assertEqual(rfd.RETRIES, 1)
        self.assertEqual(rfd.TIMEOUT, 180)
        self.assertEqual(rfd.COOLDOWN, 1.5)
        self.assertEqual(rfd.ROUTER_URL, "http://127.0.0.1:6969/v1/chat/completions")
        self.assertEqual(rfd.MAX_TOKENS, {"cal": 600, "cap": 800, "needle": 50, "identity": 100})

    def test_caller_passes_timeout_to_urlopen(self):
        urlopen, calls = fake_urlopen(make_api_response())
        with mock.patch.object(rfd.urllib.request, "urlopen", urlopen):
            caller = rfd.make_caller(router_url="http://x", api_key="k",
                                     cooldown=0, timeout=180)
            caller("prov/model-c", "hi")
        self.assertEqual(calls[0]["timeout"], 180)

    def test_caller_reads_reasoning_content_off_the_wire(self):
        payload = make_api_response(content="a", reasoning_content="b", ptok=3, ctok=2)
        urlopen, _calls = fake_urlopen(payload)
        with mock.patch.object(rfd.urllib.request, "urlopen", urlopen):
            caller = rfd.make_caller(router_url="http://x", api_key="k",
                                     cooldown=0, timeout=180)
            r = caller("prov/model-d", "hi")
        self.assertTrue(r["ok"])
        self.assertEqual(r["content"], "a")
        self.assertEqual(r["reasoning_content"], "b")


# ---------------------------------------------------------------------------
# 5. Orchestration: run() with a fake caller (no network, tmp output)
# ---------------------------------------------------------------------------
class RecordingCaller:
    """Fake caller: cal/cap/identity answer normally; needles hit only in
    the 16K rung (content). Records every call so ordering can be asserted."""

    def __init__(self, fail_models=(), needle_miss_models=()):
        self.fail_models = set(fail_models)
        self.needle_miss_models = set(needle_miss_models)
        self.log = []

    def __call__(self, model, prompt, max_tokens=600):
        self.log.append((model, prompt[:40], max_tokens))
        if model in self.fail_models:
            return flat_result(ok=False, error="HTTP 500")
        if "What is the secret code?" in prompt:
            body = prompt.rsplit("\n\nWhat is the secret code?", 1)[0]
            m = NEEDLE_CODE_RE.search(body)
            if model in self.needle_miss_models or not m or len(body) > 16000 * 4 + 1000:
                return flat_result(content="no code", ctok=3)
            return flat_result(content=m.group(1), ctok=8)
        return flat_result(content="answer", ptok=77, ctok=9)


class TestRunOrchestration(WithTmpOut):
    def test_run_single_family_kimi_writes_incremental_json(self):
        caller = RecordingCaller()
        with mock.patch("builtins.print"):
            results = rfd.run("kimi", call=caller, out_path=self.out_path)
        self.assertEqual(sorted(k for k in results if k != "_consensus"),
                         ["aibox/kimi-k3", "aibox/kimi/kimi-k3",
                          "chat2api/workbuddy-web/kimi-k3"])
        # persisted file matches returned results
        on_disk = json.load(open(self.out_path, encoding="utf-8"))
        self.assertEqual(on_disk["aibox/kimi/kimi-k3"]["ceiling"], "16K")
        self.assertEqual(on_disk["aibox/kimi/kimi-k3"]["tokenizer"]["ptok"], 77)
        self.assertEqual(on_disk["aibox/kimi-k3"]["ceiling"], "16K")
        # consensus block present for kimi
        self.assertIn("kimi", on_disk["_consensus"])
        ck = on_disk["_consensus"]["kimi"]
        self.assertEqual(ck["canonical_model"], "kimi-k3")
        self.assertIn("gold", ck["sources"])
        self.assertIn("double", ck["sources"])
        self.assertIn("depth", ck["sources"])
        self.assertEqual(ck["degraded"], [])
        # every call used a pinned model
        for model, _prefix, _mt in caller.log:
            self.assertIn("/", model)

    def test_run_glm_flags_degraded_when_depth_below_gold(self):
        caller = RecordingCaller(needle_miss_models=("chat2api/zai-web1/glm-5.3-flash-max",))
        with mock.patch("builtins.print"):
            results = rfd.run("glm", call=caller, out_path=self.out_path)
        ck = results["_consensus"]["glm"]
        self.assertEqual(ck["sources"]["gold"]["ceiling"], "16K")
        self.assertEqual(ck["sources"]["depth"]["ceiling"], "none")
        self.assertEqual(ck["sources"]["gold"]["capability_passed"], 3)
        self.assertIn("depth", ck["degraded"])
        self.assertNotIn("double", ck["degraded"])

    def test_run_gpt_skips_conditional_fallback_when_gold_clean(self):
        caller = RecordingCaller()
        with mock.patch("builtins.print"):
            results = rfd.run("gpt", call=caller, out_path=self.out_path)
        self.assertNotIn("mintrouter/gpt-6.1-sol", results)
        self.assertIn("codex/gpt-5.6-sol", results)
        self.assertEqual(len([k for k in results if k != "_consensus"]), 5)

    def test_run_gpt_uses_fallback_when_gold_lane_errors(self):
        caller = RecordingCaller(fail_models=tuple(
            e["pinned_model"] for e in rfd.PLAN
            if e["family"] == "gpt" and e["tier"] == "gold"))
        with mock.patch("builtins.print"):
            results = rfd.run("gpt", call=caller, out_path=self.out_path)
        self.assertIn("mintrouter/gpt-6.1-sol", results)
        self.assertEqual(results["codex/gpt-5.5"]["fatal"], "HTTP 500")
        # fallback lane completed normally
        self.assertEqual(results["mintrouter/gpt-6.1-sol"]["ceiling"], "16K")

    def test_run_gemini_all_reference_models(self):
        caller = RecordingCaller()
        with mock.patch("builtins.print"):
            results = rfd.run("gemini", call=caller, out_path=self.out_path)
        self.assertEqual(len([k for k in results if k != "_consensus"]), 4)
        for k in results:
            if k != "_consensus":
                self.assertTrue(k.startswith("antigravity/"))
        self.assertNotIn("_consensus", results)  # gemini is not a consensus family

    def test_run_serial_within_provider_and_pinned_models_only(self):
        caller = RecordingCaller()
        with mock.patch("builtins.print"):
            rfd.run("kimi", call=caller, out_path=self.out_path)
        providers = [m.split("/", 1)[0] for m, _p, _mt in caller.log]
        # each provider's calls form one contiguous block (serial per provider)
        boundaries = sum(1 for a, b in zip(providers, providers[1:]) if a != b)
        self.assertEqual(boundaries, len(set(providers)) - 1)

    def test_unknown_family_exits_with_error(self):
        with self.assertRaises(SystemExit):
            with mock.patch("builtins.print"):
                rfd.run("nonexistent", call=lambda *a, **k: flat_result(),
                        out_path=self.out_path)


# ---------------------------------------------------------------------------
# 6. Persistence behavior
# ---------------------------------------------------------------------------
class TestPersistence(WithTmpOut):
    def test_persist_is_atomic_and_creates_dirs(self):
        nested = os.path.join(self.tmpdir, "a", "b", "out.json")
        rfd.persist({"x": 1}, nested)
        self.assertEqual(json.load(open(nested, encoding="utf-8")), {"x": 1})
        self.assertFalse(os.path.exists(nested + ".tmp"))

    def test_resolve_out_path_env_overrides(self):
        with mock.patch.dict(os.environ, {"FULL_DEPTH_OUT": "Z:\\somewhere\\res.json"}):
            self.assertEqual(rfd.resolve_out_path(), "Z:\\somewhere\\res.json")
        with mock.patch.dict(os.environ, {"FULL_DEPTH_OUT": "Z:\\dir"}):
            self.assertEqual(rfd.resolve_out_path(),
                             os.path.join("Z:\\dir", "full_depth_unified.json"))
        os.environ.pop("FULL_DEPTH_OUT", None)
        self.assertEqual(rfd.resolve_out_path(),
                         os.path.join(rfd.DEFAULT_OUT_DIR, "full_depth_unified.json"))

    def test_crash_safe_incremental_after_every_model(self):
        caller = RecordingCaller()
        persisted_snapshots = []
        real_persist = rfd.persist

        def spy(results, out_path):
            persisted_snapshots.append({k: v for k, v in results.items()})
            real_persist(results, out_path)

        with mock.patch.object(rfd, "persist", spy), mock.patch("builtins.print"):
            rfd.run("kimi", call=caller, out_path=self.out_path)
        # one write per model (sizes 1, 2, 3) + a final consensus write
        sizes = [len({k for k in s if k != "_consensus"}) for s in persisted_snapshots]
        self.assertEqual(sizes, [1, 2, 3, 3])
        self.assertIn("_consensus", persisted_snapshots[-1])


# ---------------------------------------------------------------------------
# 7. CLI
# ---------------------------------------------------------------------------
class TestCli(unittest.TestCase):
    def test_parser_default_family_is_all(self):
        args = rfd.build_parser().parse_args([])
        self.assertEqual(args.family, "all")

    def test_parser_accepts_each_family(self):
        for fam in ("gpt", "gemini", "kimi", "glm", "all"):
            self.assertEqual(rfd.build_parser().parse_args([fam]).family, fam)

    def test_parser_rejects_unknown_family(self):
        with self.assertRaises(SystemExit):
            rfd.build_parser().parse_args(["not-a-family"])


if __name__ == "__main__":
    unittest.main()
