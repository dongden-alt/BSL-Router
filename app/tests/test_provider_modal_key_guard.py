import re
from pathlib import Path

APP_JS = Path(__file__).resolve().parents[1] / "static" / "app.js"


def _provider_save_block() -> str:
    """Extract the body of window.saveProviderModal up to closeProviderModal()."""
    source = APP_JS.read_text(encoding="utf-8")
    start = source.index("window.saveProviderModal = () => {")
    end = source.index("    closeProviderModal();", start)
    return source[start:end]


def test_fix_comment_and_append_branch_present() -> None:
    """The fix comment marker and the append branch ('Connection ' +) must both be present,
    and the discoverability placeholder must exist in openProviderModal."""
    source = APP_JS.read_text(encoding="utf-8")
    assert "Fix for silent key loss 2026-08-25" in source
    block = _provider_save_block()
    assert "'Connection ' + (" in block or "'Connection ' +" in block
    # Discoverability placeholder in the edit branch of openProviderModal
    assert "Leave blank to keep existing key" in source
    assert "a different key adds a new connection" in source


def test_no_unconditional_key_clobber() -> None:
    """The old unguarded `firstConn.api_key = key;` must only appear inside the
    new-provider branch (guarded by existingConns.length === 0). The different-key
    case must append via connections.push()."""
    block = _provider_save_block()
    # The append branch must exist for the different-key case.
    assert "connections.push(" in block
    # Every occurrence of the clobber assignment must be guarded by the
    # new-provider branch — i.e. a length/emptiness guard must appear before it.
    assignment = "firstConn.api_key = key;"
    guard = "existingConns.length === 0"
    # There must be at least one guarded assignment.
    assert assignment in block, "expected the assignment to remain in the new-provider branch"
    assert guard in block, "expected a new-provider branch guard"
    # Verify ordering: the guard appears before the assignment (new-provider branch
    # comes first), proving the assignment is inside the guarded branch, not
    # unconditional at the top level of the block.
    assert block.index(guard) < block.index(assignment), (
        "firstConn.api_key = key must be inside the new-provider branch, not unconditional"
    )
    # Sanity: there is no SECOND unguarded assignment after the push branch.
    first = block.index(assignment)
    rest = block[first + len(assignment):]
    assert assignment not in rest, (
        "found a second (unguarded) firstConn.api_key = key assignment after the guard"
    )


def test_placeholder_wiring_in_edit_branch() -> None:
    """openProviderModal edit branch must set keyInput.placeholder containing
    'keep existing key' when editing a provider with existing connections."""
    source = APP_JS.read_text(encoding="utf-8")
    # Locate the edit branch of openProviderModal (the block guarded by
    # `if (editingProviderId && globalConfig.providers ...)`).
    start = source.index("if (editingProviderId && globalConfig.providers")
    end = source.index("    if (titleEl) {", start)  # the new-provider branch follows
    edit_branch = source[start:end]
    assert "keyInput.placeholder" in edit_branch
    assert "keep existing key" in edit_branch
