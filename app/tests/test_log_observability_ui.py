"""Static regression coverage for Logs-view scroll preservation."""

from pathlib import Path


APP_JS = Path(__file__).resolve().parents[1] / "static" / "app.js"


def test_logs_capture_at_bottom_before_patching_and_restore_manual_scroll():
    source = APP_JS.read_text(encoding="utf-8")

    capture = source.index("const consoleScrollBeforePatch = captureLogScrollState")
    patch = source.index("existingConsole.innerHTML = consoleLogLines")
    restore = source.index("restoreLogScroll(container.querySelector('#console-log-box'), consoleScrollBeforePatch)")

    assert capture < patch < restore
    assert "atBottom: Boolean(box) && box.scrollHeight - box.scrollTop - box.clientHeight < 60" in source
    assert "if (!beforePatch.existed || beforePatch.atBottom)" in source
    assert "box.scrollTop = box.scrollHeight;" in source
    assert "box.scrollTop = Math.min(beforePatch.scrollTop" in source
