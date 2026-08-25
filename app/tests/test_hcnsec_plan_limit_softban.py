"""Regression tests for hcnsec 'Action plan limited' 400 classification.

Proves that a deterministic account-health reject from hcnsec
('user is not allowed to access ... Action plan limited') is classified as
`auth` and triggers an immediate softban, instead of being silently dropped
as a generic 400 client-payload error (the pre-2026-08-25 behavior that left
the dead leaf re-selected as the coder-2 primary on every request).

The fix lives in app/error_prevention.py: three phrases were added to both
ERROR_TYPES['auth'] and the _is_billing_error marker tuple so the 400 is NOT
short-circuited by the client-payload guard at record_error L285.

Pure unit tests — no live sockets, no config.yaml writes (ephemeral softban
route never touches disk).
"""

from app.error_prevention import ErrorPreventionManager


def _enabled_config():
    return {
        "error_prevention": {"enabled": True, "consecutive_threshold": 3},
        # minimal providers shape so manually_enable_model-style swaps don't blow up
        "providers": {},
    }


HCNSEC_PLAN_LIMIT_MSG = (
    "Request was rejected due to reason: user is not allowed to access, "
    "reason: CustomerId ... Action plan limited ."
)


class TestHcnsecPlanLimitClassification:
    """Plan-limit rejects must be auth, not unknown/generic-400."""

    def test_classifies_as_auth(self):
        mgr = ErrorPreventionManager(_enabled_config())
        assert mgr.classify_error(400, HCNSEC_PLAN_LIMIT_MSG) == "auth"

    def test_classifies_as_auth_short_phrase(self):
        mgr = ErrorPreventionManager(_enabled_config())
        # The 'plan limited' substring alone (without the full sentence) still hits.
        assert mgr.classify_error(400, "Action plan limited for this account") == "auth"

    def test_classifies_as_auth_not_allowed(self):
        mgr = ErrorPreventionManager(_enabled_config())
        assert (
            mgr.classify_error(400, "user is not allowed to access this model")
            == "auth"
        )

    def test_no_longer_silent_on_400(self):
        """Pre-fix: record_error returned None for this 400 (no ban)."""
        mgr = ErrorPreventionManager(_enabled_config())
        action = mgr.record_error(
            "hcnsec", "kat-coder-pro-v2.5", 400, HCNSEC_PLAN_LIMIT_MSG
        )
        assert action is not None
        assert action["action"] == "softban"
        assert action["error_type"] == "auth"
        assert action["ephemeral"] is True
        assert action["duration_seconds"] > 0

    def test_true_client_payload_400_still_ignored(self):
        """A real client-payload 400 (no auth markers) must still be ignored."""
        mgr = ErrorPreventionManager(_enabled_config())
        action = mgr.record_error(
            "someprov", "somemodel", 400, "Unsupported parameter: messages"
        )
        assert action is None

    def test_plan_limit_ban_surfaces_in_combo_prefilter(self):
        """After the softban, check_ban must report the leaf as banned so the
        combo pre-filter (resolve_combo) and advance_combo_retry skip it."""
        cfg = _enabled_config()
        mgr = ErrorPreventionManager(cfg)
        mgr.record_error("hcnsec", "kat-coder-pro-v2.5", 400, HCNSEC_PLAN_LIMIT_MSG)
        from app.error_prevention import check_ban

        banned, ban_state, remaining = check_ban(cfg, "hcnsec", "kat-coder-pro-v2.5")
        assert banned is True
        assert ban_state == "softban"
        assert remaining is not None and remaining > 0
