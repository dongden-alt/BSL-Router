"""Agent route resolution tests.

Proves resolve_agent_route behavior:
  - granular key with own cell  -> own cell
  - granular key without cell   -> parent cell (planner/auditor only)
  - underscore keys whose parent is NOT a real category -> None (no false parenting)
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.middleware.bsl_router_utils import resolve_agent_route


def test_granular_key_resolves_to_own_cell():
    routes = {
        "planner_architect": {"primary": "model-a"},
        "planner": {"primary": "model-b"},
    }
    assert resolve_agent_route(routes, "planner_architect") == {"primary": "model-a"}


def test_granular_key_falls_back_to_parent():
    routes = {"planner": {"primary": "model-b"}}
    assert resolve_agent_route(routes, "planner_architect") == {"primary": "model-b"}


def test_no_false_parenting_for_coder_families():
    routes = {"fast": {"primary": "model-z"}}
    assert resolve_agent_route(routes, "fast_coder") is None
    assert resolve_agent_route({"power": {}}, "power_coder") is None
    assert resolve_agent_route({"ultra": {}}, "ultra_coder") is None
    assert resolve_agent_route({"frontend": {}}, "frontend_coder") is None


def test_auditor_family_falls_back_to_parent():
    routes = {"auditor": {"primary": "model-c"}}
    assert resolve_agent_route(routes, "auditor_reviewer") == {"primary": "model-c"}
    assert resolve_agent_route(routes, "auditor_reviewer_member") == {"primary": "model-c"}


def test_empty_and_missing_keys():
    assert resolve_agent_route({}, "planner_architect") is None
    assert resolve_agent_route({"planner": {}}, "") is None
    assert resolve_agent_route({"planner": {}}, None) is None
    # Granular key that does not exist and has no parent -> None
    assert resolve_agent_route({"planner": {}}, "scout_extra") is None
