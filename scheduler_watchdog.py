"""
Single-instance, resilient watchdog for the linkedin-leads scheduler.

Runs under pythonw.exe (no console window). Spawns the REAL worker
(python.exe scheduler_worker.py, CREATE_NO_WINDOW) and restarts it 60s after
any unexpected exit. Exactly one worker instance is guaranteed: if a
scheduler_worker.py process is already alive anywhere, the watchdog exits
quietly. Anti-thrash: after 3 consecutive quick exits the restart delay backs
off (60s -> 5min) to avoid hammering (each restart also sends a Telegram
status notice from scheduler_worker.main()).

This only launches/manages the worker; it does NOT touch any logic.
"""
import os
import subprocess
import sys
import time

ROOT = r"C:\Users\surya\Projects\linkedin-leads"
WORKER = os.path.join(ROOT, "scheduler_worker.py")
PID_FILE = r"C:\Users\surya\AppData\Local\Temp\opencode\scheduler_watchdog.pid"
LOG_FILE = r"C:\Users\surya\AppData\Local\Temp\opencode\scheduler_watchdog.log"
RESTART_DELAY = 60
BACKOFF_DELAY = 300
QUICK_EXIT_SECONDS = 30
QUICK_EXIT_LIMIT = 3


def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def resolve_python():
    base = os.path.dirname(sys.executable or "")
    cand = os.path.join(base, "python.exe")
    if os.path.isfile(cand):
        return cand
    return sys.executable  # fallback (if launched with python.exe directly)


def worker_pids():
    """Any running process whose command line carries scheduler_worker.py."""
    names = ("python.exe", "pythonw.exe")
    filt = "Name='" + "' or Name='".join(names) + "'"
    cmd = ("(Get-CimInstance Win32_Process -Filter \"%s\").CommandLine" % filt)
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
            capture_output=True, text=True, timeout=60).stdout or ""
    except Exception as e:
        log("pidscan error: %s" % e)
        return []
    return [l.strip() for l in out.splitlines() if "scheduler_worker.py" in l]


def main():
    log("watchdog starting (pid=%d)" % os.getpid())
    try:
        with open(PID_FILE, "w") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass

    if worker_pids():
        log("scheduler_worker.py already running elsewhere; watchdog exits (no duplicate)")
        return

    pyexe = resolve_python()
    quick_exits = 0
    delay = RESTART_DELAY
    while True:
        try:
            log("launching worker: %s %s (hidden)" % (pyexe, WORKER))
            out = open(LOG_FILE, "a", encoding="utf-8")
            proc = subprocess.Popen(
                [pyexe, WORKER],
                cwd=ROOT,
                creationflags=0x08000000,  # CREATE_NO_WINDOW
                stdout=out, stderr=subprocess.STDOUT,
            )
            t0 = time.time()
            rc = proc.wait()
            alive = time.time() - t0
            if alive < QUICK_EXIT_SECONDS:
                quick_exits += 1
            else:
                quick_exits = 0
            log("worker exited rc=%s after %.0fs (quick_exit_streak=%d); next restart +%ds"
                % (rc, alive, quick_exits, delay))
        except Exception as e:
            quick_exits += 1
            log("launch error: %s; next retry +%ds" % (e, delay))

        time.sleep(delay)
        if worker_pids():
            log("worker instance detected elsewhere; watchdog exits to keep single instance")
            return
        if quick_exits >= QUICK_EXIT_LIMIT:
            delay = BACKOFF_DELAY
            log("backoff to %ds (worker crashing repeatedly)" % delay)


if __name__ == "__main__":
    main()