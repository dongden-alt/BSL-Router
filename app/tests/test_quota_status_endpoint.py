"""Per-key quota probe: unit normalization + /api/quota/status contract.

Research 2026-08-24 (source-verified one-api/new-api):
  subscription hard_limit_usd = (RemainQuota+UsedQuota)/QuotaPerUnit (500k=$1)
  usage total_usage = same units. Gateways with DisplayInCurrencyEnabled return
  already-USD limits and OpenAI-style cents usage. _normalize_billing must
  resolve the variant from hard_limit magnitude alone (self-consistent per
  gateway since both endpoints read the same DB row).
"""
import pytest
from fastapi.testclient import TestClient


def _normalize(hard, used):
    from app.main import _normalize_billing
    return _normalize_billing(hard, used)


class TestNormalizeBillingUnits:
    def test_raw_quota_variant_vietapi_shape(self):
        # Live-probed: vietapi-o hard=48,000,000 (raw quota), usage=0
        r = _normalize(48_000_000, 0.0)
        assert r == {"hard_limit_usd": 96.0, "used_usd": 0.0, "remaining_usd": 96.0, "remaining_pct": 100.0}

    def test_raw_quota_variant_vsllm_shape(self):
        # Live-probed: vsllm-a hard=100,000,000, usage=53,176.2564 - $199.89 left
        r = _normalize(100_000_000, 53176.2564)
        assert r["hard_limit_usd"] == 200.0
        assert r["used_usd"] == 0.11
        assert r["remaining_usd"] == 199.89
        assert r["remaining_pct"] == 99.9

    def test_usd_variant_openai_shape(self):
        # DisplayInCurrency gateway: hard=$9.99, usage in cents (241 = $2.41)
        r = _normalize(9.99, 241.0)
        assert r["hard_limit_usd"] == 9.99
        assert r["used_usd"] == 2.41
        assert r["remaining_usd"] == pytest.approx(7.58, abs=0.01)

    def test_boundary_ten_thousand_is_usd(self):
        # >10,000 is the raw-quota trigger; exactly 10,000 stays USD (cents usage)
        r = _normalize(10_000.0, 100_000.0)
        assert r["hard_limit_usd"] == 10_000.0
        assert r["used_usd"] == 1_000.0

    def test_drained_clamps_to_zero(self):
        r = _normalize(100_000_000, 100_000_000)
        assert r["remaining_usd"] == 0.0
        assert r["remaining_pct"] == 0.0

    def test_overuse_clamps_remaining(self):
        # usage beyond limit must not go negative
        r = _normalize(50_000_000, 60_000_000)
        assert r["remaining_usd"] == 0.0


class TestQuotaStatusEndpoint:
    def test_endpoint_returns_shape_and_fails_open(self, monkeypatch):
        from app.main import app, _QUOTA_CACHE
        _QUOTA_CACHE["ts"] = 0.0
        _QUOTA_CACHE["providers"] = {}

        async def _fake_probe(base, key):
            return {"hard_limit_usd": 96.0, "used_usd": 0.0, "remaining_usd": 96.0, "remaining_pct": 100.0}

        monkeypatch.setattr("app.main._probe_oneapi_billing", _fake_probe)
        client = TestClient(app)
        resp = client.get("/api/quota/status")
        assert resp.status_code == 200
        body = resp.json()
        assert "providers" in body and isinstance(body["providers"], dict)

    def test_provider_filter_param(self, monkeypatch):
        from app.main import app, _QUOTA_CACHE
        _QUOTA_CACHE["ts"] = 0.0
        _QUOTA_CACHE["providers"] = {}

        async def _fake_probe(base, key):
            return None

        monkeypatch.setattr("app.main._probe_oneapi_billing", _fake_probe)
        client = TestClient(app)
        resp = client.get("/api/quota/status?provider=vietapi-o")
        assert resp.status_code == 200
        assert "vietapi-o" in resp.json()["providers"]
