"""Real process lifecycle harness on an ephemeral non-production TCP port.

The harness drives the same PowerShell lifecycle launcher as production with a
throwaway port, a disposable listener, CA trust skipped, and logs confined to
.brain/scratch. It never touches port 443.
"""
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app import main


PROJECT_ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = PROJECT_ROOT / "scripts" / "bslrouter.ps1"
SCRATCH = PROJECT_ROOT / ".brain" / "scratch" / "mitm-process-lifecycle"

# This harness drives the REAL PowerShell launcher, which starts a real
# mitmdump process from the repository virtualenv. A fresh clone has no .venv
# yet, so the launcher exits non-zero and the failure looks like a product bug
# rather than a missing prerequisite. Detect that up front and skip instead.
_MITMDUMP = PROJECT_ROOT / ".venv" / "Scripts" / (
    "mitmdump.exe" if os.name == "nt" else "mitmdump"
)
_MISSING_MITMDUMP = not _MITMDUMP.exists()
LISTENER_CODE = """
import socket
import sys
import time
port = int(sys.argv[1])
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind(('127.0.0.1', port))
sock.listen()
print('ready', flush=True)
while True:
    time.sleep(1)
"""


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    return None


def _start_foreign_listener(port):
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", LISTENER_CODE, str(port)],
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout.readline().strip() == "ready"
    return process


def _run_lifecycle(action, port, evict_foreign=False):
    """Invoke the launcher directly.

    ``evict_foreign`` models the CONSENT GATE. The Start Integration button
    passes it (so a foreign owner may be evicted); the background watchdog does
    not, so it can never silently steal the port from another proxy.
    """
    command = [
        "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(LAUNCHER),
        action, "-Mitm", "-Background", "-MitmPortOverride", str(port),
        "-SkipCaTrust", "-LogDirectory", str(SCRATCH),
    ]
    if evict_foreign:
        command.append("-EvictForeign")
    # mitmdump inherits Windows handles from its short-lived PowerShell launcher.
    # Capturing those handles makes subprocess.communicate wait for the long-lived
    # child after PowerShell has exited, so use DEVNULL and bound only the launcher.
    process = subprocess.Popen(
        command,
        cwd=PROJECT_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        returncode = process.wait(timeout=35)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
        raise
    return subprocess.CompletedProcess(command, returncode)


def _kill_port_listeners(port):
    for owner in main._listener_owners(port):
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(owner["pid"])],
            capture_output=True,
            text=True,
            timeout=10,
        )


@pytest.mark.skipif(os.name != "nt", reason="requires Windows PowerShell and taskkill")
@pytest.mark.skipif(
    _MISSING_MITMDUMP,
    reason=(
        "mitmdump not found in .venv/Scripts - install dependencies "
        "(pip install -r requirements.txt) to run the real-process MITM harness"
    ),
)
@pytest.mark.slow
@pytest.mark.timeout(60)  # Real subprocess lifecycle with 35s internal wait
def test_nonproduction_listener_tree_is_replaced_and_verified_exclusive():
    port = _free_port()
    foreign = None
    SCRATCH.mkdir(parents=True, exist_ok=True)
    try:
        foreign = _start_foreign_listener(port)
        foreign_owners = _wait_for(lambda: main._listener_owners(port))
        assert foreign_owners is not None
        assert len(foreign_owners) == 1
        assert foreign_owners[0]["is_bsl_mitm"] is False

        started = _run_lifecycle("start", port, evict_foreign=True)
        assert started.returncode == 0, started.stderr or started.stdout

        runtime = _wait_for(lambda: (
            status if (status := main._mitm_runtime_status(port))["server"] and not status["conflict"] else None
        ))
        assert runtime is not None
        assert runtime["port"] == port
        assert len(runtime["owners"]) == 1
        assert runtime["owners"][0]["is_bsl_mitm"] is True

        stopped = _run_lifecycle("stop", port)
        assert stopped.returncode == 0, stopped.stderr or stopped.stdout
        assert _wait_for(lambda: not main._listener_owners(port)) is True
    finally:
        _kill_port_listeners(port)
        if foreign and foreign.poll() is None:
            foreign.kill()
            foreign.wait(timeout=5)
        shutil.rmtree(SCRATCH, ignore_errors=True)


@pytest.mark.skipif(
    _MISSING_MITMDUMP,
    reason=(
        "mitmdump not found in .venv/Scripts - install dependencies "
        "(pip install -r requirements.txt) to run the real-process MITM harness"
    ),
)
@pytest.mark.slow
@pytest.mark.timeout(60)  # Real subprocess lifecycle with internal waits
def test_start_without_consent_refuses_and_leaves_foreign_listener_alive():
    """The watchdog's path must NEVER take the port from another process.

    Regression guard for the silent-steal bug: after a sleep/wake cycle 9Router
    would reclaim :443 and BSL's watchdog would force-kill it within 5 seconds,
    with no user action. Only the Start Integration button (-EvictForeign) may
    evict a foreign owner.

    This asserts on the real OS process, not a mock, because the bug was in the
    launcher's kill path.
    """
    port = _free_port()
    foreign = None
    SCRATCH.mkdir(parents=True, exist_ok=True)
    try:
        foreign = _start_foreign_listener(port)
        foreign_owners = _wait_for(lambda: main._listener_owners(port))
        assert foreign_owners is not None
        assert foreign_owners[0]["is_bsl_mitm"] is False
        # Track the OS-observed listener PIDs, not foreign.pid: the helper spawns
        # the actual listener as a child, so the wrapper pid differs.
        listener_pids_before = sorted(o["pid"] for o in foreign_owners)

        # No -EvictForeign: the launcher must refuse rather than kill.
        result = _run_lifecycle("start", port)
        assert result.returncode == 1, "launcher must refuse without consent"

        # The foreign listener must still be alive and still own the port.
        assert foreign.poll() is None, "foreign process was killed without consent"
        owners_after = main._listener_owners(port)
        assert sorted(o["pid"] for o in owners_after) == listener_pids_before
        assert owners_after[0]["is_bsl_mitm"] is False
    finally:
        _kill_port_listeners(port)
        if foreign and foreign.poll() is None:
            foreign.kill()
            foreign.wait(timeout=5)
        shutil.rmtree(SCRATCH, ignore_errors=True)
