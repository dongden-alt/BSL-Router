"""UI contract tests for the Codex JSON import action."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent.parent.parent
SOURCE = (ROOT / "app/static/app.js").read_text(encoding="utf-8")


def _import_body():
    body = SOURCE[SOURCE.index("function openCodexJsonImport()") :]
    return body[: body.index("async function startCodexLoopback()")]


def test_import_action_is_codex_only_and_shares_oauth_action_row():
    assert SOURCE.count("codex-import-json-btn") == 1
    assert "providerId === 'codex' || providerId === 'openai_codex'" in SOURCE
    assert '${isCodexImportProvider(activeProviderId) ?' in SOURCE
    assert 'onclick="openOAuthTokenModal()"' in SOURCE and 'onclick="openCodexJsonImport()"' in SOURCE


def test_modal_controls_and_accessibility():
    body = _import_body()
    for fragment in ('type="file" accept="application/json,.json"', '<textarea id="codex-import-text"',
                     'for="codex-import-file"', 'for="codex-import-text"',
                     'role="status" aria-live="polite"', 'id="codex-import-cancel"'):
        assert fragment in body


def test_busy_guard_precedes_async_file_read_and_allows_cancel():
    body = _import_body()
    assert body.index("if (busy || cancelled) return;") < body.index("await file.text()")
    assert "busy = true" in body and "submitBtn.disabled = true" in body
    assert "cancelBtn.disabled = true" not in body
    assert "requestController?.abort()" in body


def test_close_cleanup_and_scoped_escape_listener():
    body = _import_body()
    assert "onClose: () =>" in body
    assert "clearCredentialInputs();" in body
    assert "textArea = null;" in body and "fileInput = null;" in body
    modal = SOURCE[SOURCE.index("function showOAuthModal") : SOURCE.index("function setOAuthModalMessage")]
    assert "event.key === 'Escape'" in modal
    assert "document.removeEventListener('keydown', onEscape)" in modal


def test_file_paste_ambiguity_and_utf8_size_are_enforced():
    body = _import_body()
    assert "if (file && pasted)" in body
    assert "not both" in body
    assert "new TextEncoder().encode(rawPayload).byteLength" in body
    assert "rawPayload.length > CODEX_IMPORT_MAX_BYTES" not in body


def test_raw_json_is_posted_without_duplicate_key_collapse():
    body = _import_body()
    assert "parsedPayload = JSON.parse(rawPayload)" in body
    assert "body: rawPayload" in body
    assert "JSON.stringify(parsedPayload)" not in body
    assert "rawPayload = null" in body and "parsedPayload = null" in body


def test_backend_post_late_response_suppression_and_retry():
    body = _import_body()
    assert "fetch('/api/oauth/codex/import-json'" in body
    assert "signal: requestController.signal" in body
    assert body.count("if (cancelled)") >= 2
    assert "busy = false" in body and "submitBtn.disabled = false" in body
    assert "await completeOAuthConnection('codex', result.connection, modal);" in body


def test_browser_oauth_flow_preserved():
    assert "window.openOAuthTokenModal = () =>" in SOURCE
    assert "startFixedPortLoopbackFlow('codex')" in SOURCE
    assert "startCodexLoopback();" in SOURCE


def test_asset_revision_bumped():
    html = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
    revision = re.search(r'/admin/app\.js\?v=([^\"]+)', html)
    assert revision and revision.group(1) == "20261022-codex-import-json-3"
