from __future__ import annotations

import subprocess
import threading
import time

_log_lock = threading.Lock()


def log(message: str, prefix: str = "") -> None:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with _log_lock:
        print(f"{stamp} {prefix}{message}", flush=True)


def run(cmd: list[str], prefix: str = "", cwd: str | None = None) -> bool:
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except OSError as e:
        log(f"{cmd[0]}: {e}", prefix)
        return False
    with proc:
        assert proc.stdout
        for line in proc.stdout:
            log(line.rstrip("\n"), prefix)
        code = proc.wait()
    if code:
        log(f"{cmd[0]} exited with {code}", prefix)
    return code == 0
