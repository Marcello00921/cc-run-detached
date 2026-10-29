# -*- coding: utf-8 -*-
"""Tests for run_detached. Short canaries (each under ~20 s). Run: python -m unittest"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import run_detached as rd  # noqa: E402

PY = sys.executable
SCRIPT = str(ROOT / "run_detached.py")


def kill_tree(pid: int) -> None:
    """How a process tree is killed: `taskkill /T /F` on Windows, a process-group kill on POSIX."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=30)
    else:
        try:
            os.killpg(os.getpgid(pid), 9)
        except Exception:
            pass


class RunDetachedTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="run_detached_test_")
        self._old = {k: os.environ.get(k) for k in ("RUN_DETACHED_HOME", rd._NO_DETACH_ENV)}
        os.environ["RUN_DETACHED_HOME"] = self.home
        os.environ.pop(rd._NO_DETACH_ENV, None)

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.home, ignore_errors=True)

    def test_launch_returns_while_command_runs(self):
        # State, not wall-clock: a loaded machine can take seconds just to start Python.
        self.assertEqual(rd.launch("bg", [PY, "-c", "import time; time.sleep(60)"], quiet=True), rd.RC_FINISHED)
        st = rd.status("bg")
        self.assertEqual(st["state"], "running", "--launch blocked until the command ended")
        if os.name == "nt":
            kill_tree(st["child_pid"])
        else:
            os.kill(st["child_pid"], 9)
        self.assertEqual(rd.wait("bg", 30, quiet=True), rd.RC_FAILED)

    def test_finished_rc0(self):
        self.assertEqual(rd.launch("ok", [PY, "-c", "import time; time.sleep(1)"], quiet=True), rd.RC_FINISHED)
        self.assertEqual(rd.wait("ok", 30, quiet=True), rd.RC_FINISHED)
        st = rd.status("ok")
        self.assertEqual((st["state"], st["rc"]), ("finished", 0))

    def test_failed_rc3_and_log(self):
        rd.launch("rc3", [PY, "-c", "import sys; print('exiting with 3'); sys.exit(3)"], quiet=True)
        self.assertEqual(rd.wait("rc3", 30, quiet=True), rd.RC_FAILED)
        st = rd.status("rc3")
        self.assertEqual((st["state"], st["rc"]), ("failed", 3))
        self.assertIn("exiting with 3", pathlib.Path(st["log"]).read_text(encoding="utf-8"))

    def test_wait_timeout_returns_running(self):
        rd.launch("slow", [PY, "-c", "import time; time.sleep(8)"], quiet=True)
        t0 = time.time()
        self.assertEqual(rd.wait("slow", 1, quiet=True), rd.RC_RUNNING)
        self.assertLess(time.time() - t0, 5, "--timeout is not hard")
        self.assertEqual(rd.wait("slow", 30, quiet=True), rd.RC_FINISHED)

    def test_slow_watcher_is_not_reported_as_failed(self):
        """The watcher records its pid 15 s after --launch (a starved machine). --launch must
        report it as launched, and the command must run to completion. With the old fixed
        10 s window (RUN_DETACHED_CONFIRM_S=10) this test fails."""
        os.environ[rd._WATCHER_DELAY_ENV] = "15"
        try:
            rc = rd.launch("slowstart", [PY, "-c", "print('slow ok')"], quiet=True)
        finally:
            os.environ.pop(rd._WATCHER_DELAY_ENV, None)
        self.assertEqual(rc, rd.RC_FINISHED, "a watcher that is slow to start was reported as failed")
        self.assertEqual(rd.wait("slowstart", 60, quiet=True), rd.RC_FINISHED)

    def test_unknown_and_invalid_names(self):
        self.assertEqual(rd.wait("does_not_exist", 1, quiet=True), rd.RC_USAGE)
        with self.assertRaises(SystemExit):
            rd.launch("../escape", [PY, "-c", "pass"], quiet=True)

    def test_cli_status_and_list(self):
        r = subprocess.run([PY, SCRIPT, "--launch", "cli", "--", PY, "-c", "print('hi')"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = subprocess.run([PY, SCRIPT, "--wait", "cli", "--timeout", "30"], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("FINISHED", r.stdout)
        r = subprocess.run([PY, SCRIPT, "--list"], capture_output=True, text=True, timeout=60)
        self.assertIn("cli", r.stdout)

    def _survives_tree_kill(self, name: str) -> bool:
        """A launcher (the role of the agent's background shell) calls --launch and stays alive;
        we kill its whole process tree. Returns whether the command still ran to completion."""
        marker = pathlib.Path(self.home) / (name + "_marker.txt")
        cmd = [PY, "-c", "import time,pathlib; time.sleep(12); pathlib.Path(%r).write_text('alive')" % str(marker)]
        code = ("import subprocess,sys,time; subprocess.run([sys.executable, %r, '--launch', %r, '--'] + %r); "
                "time.sleep(120)" % (SCRIPT, name, cmd))
        extra = {} if os.name == "nt" else {"start_new_session": True}
        launcher = subprocess.Popen([PY, "-c", code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    env=dict(os.environ), **extra)
        status_file = pathlib.Path(self.home) / name / "status.json"
        child = None
        for _ in range(150):  # wait until the command is up before killing
            try:
                child = json.loads(status_file.read_text(encoding="utf-8")).get("child_pid")
            except Exception:
                child = None
            if child:
                break
            time.sleep(0.1)
        self.assertTrue(child, "the command did not start within 15 s")
        kill_tree(launcher.pid)
        try:
            launcher.wait(timeout=15)
        except Exception:
            pass
        rc = rd.wait(name, 40, quiet=True)
        return marker.exists() and rc == rd.RC_FINISHED

    def test_survives_launcher_tree_kill(self):
        self.assertTrue(self._survives_tree_kill("survive"),
                        "killed the launcher's tree and the command did NOT survive")

    def test_negative_control_plain_child_dies(self):
        """Proves the survival test can fail: as a plain child, the command dies with the tree."""
        os.environ[rd._NO_DETACH_ENV] = "1"
        try:
            self.assertFalse(self._survives_tree_kill("control"),
                             "negative control survived: the survival test does not discriminate")
        finally:
            os.environ.pop(rd._NO_DETACH_ENV, None)
        st = rd.status("control")
        self.assertEqual(st.get("state"), "failed")


if __name__ == "__main__":
    unittest.main()
