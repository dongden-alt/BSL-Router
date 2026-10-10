"""Static regression tests: the Authenticity tab Run Test button must paint the running UI.

Bug: renderAuthenticityTab() early-returned while a test was in flight, so the
'Testing...' button state, the Cancel button and the step indicator never
appeared (and a mid-run tab re-entry stayed blank). These tests pin the static
structure of app.js only - no server, no network, no config.
"""
from pathlib import Path

APP_JS = Path(__file__).resolve().parents[2] / "app" / "static" / "app.js"


def _load():
    src = APP_JS.read_text(encoding="utf-8")
    fn = src.index("function renderAuthenticityTab() {")
    tpl = src.index("container.innerHTML =", fn)
    end = src.index("// Lazily load data boxes once.", fn)
    state = src.index("const _authState = {")
    return src, fn, tpl, end, state


def test_render_authenticity_tab_has_no_running_early_return():
    src, fn, tpl, _end, _state = _load()
    head = src[fn:tpl]
    assert "st.running" not in head, (
        "renderAuthenticityTab() must paint even while a test runs, but its "
        "function head still references st.running (the early-return guard is "
        "back):\n" + head
    )


def test_running_ui_is_rendered_from_state():
    src, _fn, tpl, end, _state = _load()
    body = src[tpl:end]
    for needle in (
        'id="auth-run-btn"',
        'id="auth-cancel-btn"',
        'id="auth-step-indicator"',
        'id="auth-step-label"',
        "st.running ? 'Testing",
    ):
        assert needle in body, f"running-state UI missing from the template: {needle!r}"


def test_step_label_survives_rerender():
    src, _fn, _tpl, _end, state = _load()
    literal = src[state:src.index("};", state)]
    assert "stepLabel:" in literal, "_authState must declare a persistent stepLabel field"
    assert "_authState.stepLabel = s.label" in src, (
        "the step poller must persist the live label into _authState.stepLabel"
    )
    assert "st.stepLabel || " in src, (
        "the template must restore the stored step label across re-renders"
    )
