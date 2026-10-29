# -*- coding: utf-8 -*-
"""run_detached: run a long command fully detached, with its status in a file.

Built for AI coding agents such as Claude Code, whose background shell commands stop at a
time limit (by default 30 minutes, at most 2 hours) or when the agent's session/process tree
goes away. Renders, long test series and builds outlive that limit and get cut off mid-way,
with no exit code.

How it works:
  * DOUBLE LAUNCH: `--launch` starts a short-lived starter process, which starts the watcher
    DETACHED and exits at once. The watcher is orphaned, so even a tree kill of the launcher
    (`taskkill /T` on Windows, a process-group kill on POSIX) does not reach it.
  * NO WINDOW (Windows): the watcher has no console (DETACHED_PROCESS) and the command runs
    with CREATE_NO_WINDOW, so no console window pops up.
  * The watcher runs the command, sends stdout+stderr to log.txt (PYTHONUNBUFFERED=1) and
    writes status.json: launching -> running -> finished (rc 0) | failed (rc != 0, or the
    watcher vanished without writing an rc).

    python run_detached.py --launch build -- make all
    python run_detached.py --status build
    python run_detached.py --wait build --timeout 270
    python run_detached.py --list

Exit codes of --status / --wait: 0 finished (command rc 0) . 3 failed . 4 still running
(the timeout passed) . 2 unknown name / usage error.

State lives in $RUN_DETACHED_HOME, or by default in %LOCALAPPDATA%\\run-detached (Windows)
or $XDG_CACHE_HOME/run-detached (~/.cache/run-detached) elsewhere.
"""
import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import time

__version__ = "0.1.1"

if sys.stdout is not None:  # the detached watcher has no stdout
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

RC_FINISHED, RC_USAGE, RC_FAILED, RC_RUNNING = 0, 2, 3, 4
_BREAKAWAY = 0x01000000  # CREATE_BREAKAWAY_FROM_JOB: leave the parent's job object, if allowed

# Negative control for the tests only: run the command as a NORMAL child of the launcher (what a
# plain background shell does). The survival test must fail with this set. Not a usage mode.
_NO_DETACH_ENV = "RUN_DETACHED_NO_DETACH"

# How long --launch waits for the watcher to record its pid before reporting a failure. It was a
# fixed 10 s: on a starved machine (a below-normal priority process can take well over 10 s just to
# start Python) the watcher recorded its pid at 19 s, --launch reported "failed" (rc 3) and the
# command then ran to completion with rc 0. Override with RUN_DETACHED_CONFIRM_S.
CONFIRM_S = float(os.environ.get("RUN_DETACHED_CONFIRM_S") or 120)

# Tests only: delay the watcher before it records its pid, to simulate a slow start.
_WATCHER_DELAY_ENV = "RUN_DETACHED_WATCHER_DELAY_S"


def base_dir() -> pathlib.Path:
    env = os.environ.get("RUN_DETACHED_HOME")
    if env:
        return pathlib.Path(env)
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA") or str(pathlib.Path.home() / "AppData" / "Local")
    else:
        root = os.environ.get("XDG_CACHE_HOME") or str(pathlib.Path.home() / ".cache")
    return pathlib.Path(root) / "run-detached"


def _no_detach() -> bool:
    return os.environ.get(_NO_DETACH_ENV) == "1"


def detached_flags() -> int:
    """Windows flags that cut the link to the parent: DETACHED_PROCESS drops the inherited
    console; CREATE_NEW_PROCESS_GROUP makes it the root of its own group (Ctrl+C and the end of
    the parent session do not reach it)."""
    if os.name != "nt":
        return 0
    return (getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))


def popen_detached(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=None, env=None):
    """Popen that does not die with its launcher. On Windows it also tries to leave the parent's
    job object (a job with KILL_ON_JOB_CLOSE kills its children); if the job forbids it, it
    starts without that flag. On POSIX it starts a new session."""
    if os.name != "nt":
        return subprocess.Popen(cmd, stdout=stdout, stderr=stderr, stdin=subprocess.DEVNULL,
                                cwd=cwd, env=env, close_fds=True, start_new_session=True)
    base = detached_flags()
    try:
        return subprocess.Popen(cmd, stdout=stdout, stderr=stderr, stdin=subprocess.DEVNULL,
                                cwd=cwd, env=env, creationflags=base | _BREAKAWAY, close_fds=True)
    except OSError:
        return subprocess.Popen(cmd, stdout=stdout, stderr=stderr, stdin=subprocess.DEVNULL,
                                cwd=cwd, env=env, creationflags=base, close_fds=True)


def alive(pid) -> bool:
    """The pid exists and (on Windows) is a python process: the OS reuses PIDs."""
    if not pid:
        return False
    if os.name != "nt":
        try:
            os.kill(int(pid), 0)
        except PermissionError:
            return True
        except OSError:
            return False
        return True
    # One query is not enough to declare the watcher dead: under heavy load `tasklist` can fail or
    # return garbage while the process is alive. Failure = unknown (treated as alive); dead only
    # when tasklist answers rc 0 without the pid, 3 times in a row.
    for attempt in range(3):
        try:
            r = subprocess.run(["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
                               capture_output=True, text=True, timeout=15)
        except Exception:
            return True
        out = (r.stdout or "").lower()
        if f'"{int(pid)}"' in out:
            return "python" in out
        if r.returncode != 0:
            return True
        if attempt < 2:
            time.sleep(1.0)
    return False


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _folder(name: str) -> pathlib.Path:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", name or "") or name in (".", ".."):
        raise SystemExit("invalid name (letters, digits, _ . - only): %r" % name)
    return base_dir() / name


def _read(p: pathlib.Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write(p: pathlib.Path, d: dict) -> None:
    """Atomic write. On Windows `os.replace` raises PermissionError while another process (a
    --wait) is reading the file; without the retry the watcher died on its final write and a
    finished command showed up as 'watcher vanished'."""
    tmp = p.with_suffix(".tmp.%d" % os.getpid())
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    for i in range(50):
        try:
            os.replace(tmp, p)
            return
        except PermissionError:
            if i == 49:
                raise
            time.sleep(0.1)


def _run(name: str) -> int:
    """The watcher: runs the command and records its rc."""
    folder = _folder(name)
    st = _read(folder / "status.json")
    cmd = st["cmd"]
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    env.pop(_NO_DETACH_ENV, None)
    delay = env.pop(_WATCHER_DELAY_ENV, None)
    if delay:
        time.sleep(float(delay))
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0
    st.update(state="running", pid=os.getpid(), started=_now())
    with open(folder / "log.txt", "ab") as log:
        log.write(f"=== {_now()} . {cmd}\n".encode("utf-8"))
        log.flush()
        try:
            p = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                 cwd=st.get("cwd") or None, env=env, creationflags=no_window)
        except OSError as e:
            log.write(f"could not start: {e}\n".encode("utf-8"))
            st.update(state="failed", rc=None, ended=_now(), reason=f"could not start: {e}")
            _write(folder / "status.json", st)
            return RC_FAILED
        st["child_pid"] = p.pid
        _write(folder / "status.json", st)
        rc = p.wait()
    st.update(state="finished" if rc == 0 else "failed", rc=rc, ended=_now())
    _write(folder / "status.json", st)
    return RC_FINISHED if rc == 0 else RC_FAILED


def launch(name: str, cmd: list, cwd=None, quiet: bool = False) -> int:
    folder = _folder(name)
    old = _read(folder / "status.json")
    if old.get("state") in ("launching", "running") and alive(old.get("pid")):
        print(f"  '{name}' is already running (pid {old.get('pid')}); nothing launched")
        return RC_USAGE
    folder.mkdir(parents=True, exist_ok=True)
    _write(folder / "status.json", {"name": name, "state": "launching", "cmd": list(cmd), "rc": None,
                                    "cwd": str(pathlib.Path(cwd or os.getcwd()).resolve()),
                                    "launched": _now(), "log": str(folder / "log.txt")})
    me = [sys.executable, str(pathlib.Path(__file__).resolve())]
    if _no_detach():  # negative control: plain child, blocks the launcher
        return _run(name)
    # double launch: the starter brings the watcher up detached and exits; the watcher is orphaned
    r = subprocess.run(me + ["--_start", name], capture_output=True, text=True, timeout=60,
                       creationflags=detached_flags(), env=dict(os.environ, RUN_DETACHED_HOME=str(base_dir())))
    if r.returncode != 0:
        print(f"  starter failed rc={r.returncode}: {(r.stderr or r.stdout)[-400:]}")
        return RC_FAILED
    limit = time.time() + CONFIRM_S
    while True:  # confirm the watcher wrote its pid; do not claim victory on Popen
        st = _read(folder / "status.json")
        if st.get("pid"):
            if not quiet:
                print(f"  launched detached . '{name}' . watcher pid {st['pid']} . log {st['log']}")
                print(f"  check: --status {name}   wait: --wait {name} --timeout SECONDS")
            return RC_FINISHED
        if time.time() >= limit:
            break
        time.sleep(0.2)
    print(f"  watcher did not record its pid within {CONFIRM_S:.0f} s; see {folder / 'log.txt'}")
    return RC_FAILED


def _start(name: str) -> int:
    me = [sys.executable, str(pathlib.Path(__file__).resolve()), "--_run", name]
    with open(_folder(name) / "log.txt", "ab") as log:  # the watcher's own traceback lands in the log
        popen_detached(me, stdout=log, stderr=log)
    return 0


def status(name: str) -> dict:
    """Status, checking the watcher process: 'running' with a dead watcher becomes 'failed'."""
    folder = _folder(name)
    st = _read(folder / "status.json")
    if not st:
        return {}
    if st.get("state") == "running" and not alive(st.get("pid")):
        # Re-read: between the first read and the check the watcher may have written 'finished'.
        st = _read(folder / "status.json") or st
        if st.get("state") == "running":
            st.update(state="failed", ended=_now(),
                      reason="the watcher vanished without recording an rc (killed or machine rebooted)")
            _write(folder / "status.json", st)
    return st


def rc_of(st: dict) -> int:
    if not st:
        return RC_USAGE
    return {"finished": RC_FINISHED, "failed": RC_FAILED}.get(st.get("state"), RC_RUNNING)


def show(st: dict, tail: int = 5) -> None:
    if not st:
        print("  unknown name")
        return
    print(f"  {st.get('name')}: {str(st.get('state')).upper()} rc={st.get('rc')} . "
          f"start {st.get('started') or st.get('launched')} . end {st.get('ended') or '-'}"
          + (f" . {st['reason']}" if st.get("reason") else ""))
    try:
        lines = pathlib.Path(st["log"]).read_text(encoding="utf-8", errors="replace").splitlines()
        for ln in lines[-tail:]:
            print("    | " + ln[:200])
    except Exception:
        pass


def wait(name: str, timeout: float, quiet: bool = False) -> int:
    """Wait until the command ends or the timeout passes. The timeout is HARD: an agent that
    waits in slices relies on it to stay inside its prompt-cache window. The loop reads only
    status.json (cheap) and checks the watcher process at most every 30 s, and only with >= 50 s
    left, because the process check can be slow under load."""
    end = time.time() + max(0.0, timeout)
    folder = _folder(name)
    last_check = 0.0
    while True:
        now = time.time()
        if now - last_check >= 30.0 and end - now >= 50.0:
            st = status(name)
            last_check = time.time()
        else:
            st = _read(folder / "status.json")
        if not st or st.get("state") in ("finished", "failed") or time.time() >= end:
            if not quiet:
                show(st)
            return rc_of(st)
        time.sleep(min(2.0, max(0.1, end - time.time())))


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = []
    if "--" in argv:
        i = argv.index("--")
        argv, cmd = argv[:i], argv[i + 1:]
    ap = argparse.ArgumentParser(prog="run_detached",
                                 description="Run a long command detached, with status in a file.")
    ap.add_argument("--launch", metavar="NAME", help="launch the command given after --")
    ap.add_argument("--cwd", help="working directory for the command (default: current)")
    ap.add_argument("--status", metavar="NAME", help="show state, rc and the log tail")
    ap.add_argument("--wait", metavar="NAME", help="wait until NAME ends or --timeout passes")
    ap.add_argument("--timeout", type=float, default=270.0, help="seconds for --wait (default 270)")
    ap.add_argument("--list", action="store_true", help="list known jobs")
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--_start", metavar="NAME", help=argparse.SUPPRESS)
    ap.add_argument("--_run", metavar="NAME", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if a._start:
        return _start(a._start)
    if a._run:
        return _run(a._run)
    if a.launch:
        if not cmd:
            print("  missing the command after --")
            return RC_USAGE
        return launch(a.launch, cmd, a.cwd)
    if a.status:
        st = status(a.status)
        show(st)
        return rc_of(st)
    if a.wait:
        return wait(a.wait, a.timeout)
    if a.list:
        base = base_dir()
        for p in sorted(base.glob("*/status.json")) if base.exists() else []:
            st = status(p.parent.name)
            print(f"  {p.parent.name:30s} {str(st.get('state', '?')):9s} rc={st.get('rc')} end={st.get('ended') or '-'}")
        return 0
    ap.print_help()
    return RC_USAGE


if __name__ == "__main__":
    sys.exit(main())
