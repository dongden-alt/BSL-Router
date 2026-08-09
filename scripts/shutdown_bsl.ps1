$ErrorActionPreference = 'Stop'
# Find the PID listening on port 6969
$listener = Get-NetTCPConnection -LocalPort 6969 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $listener) {
    Write-Host "No process found on port 6969"
    exit 0
}

$pid = $listener.OwningProcess
Write-Host "Found listener PID: $pid"

# Walk up the parent chain to find the topmost python.exe ancestor
# (uvicorn --reload spawns a reloader parent that respawns workers)
$topPid = $pid
for ($i = 0; $i -lt 10; $i++) {
    try {
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$topPid" -ErrorAction SilentlyContinue
        if (-not $proc) { break }
        $ppid = $proc.ParentProcessId
        if ($ppid -eq 0) { break }
        $parent = Get-Process -Id $ppid -ErrorAction SilentlyContinue
        if (-not $parent) { break }
        if ($parent.ProcessName -eq 'python') {
            $topPid = $ppid
            Write-Host "Walked up to parent PID: $topPid"
        } else {
            Write-Host "Parent is non-python ($($parent.ProcessName)), stopping at PID: $topPid"
            break
        }
    } catch {
        break
    }
}

Write-Host "Killing process tree from PID: $topPid"
taskkill /T /F /PID $topPid
Write-Host "Done"
