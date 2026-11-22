"""Witness for codex_run: start, watch, stop and record one codex worker, against a fake codex.

`tests/fake_codex.py` stands in for `codex exec`, so no test calls a model or spends tokens.
Run from the repository root: `python3 -m unittest discover -s tests`.
Guarantees protected: stdin is /dev/null; a worker that makes no progress is stopped with its
whole session, children included, by SIGTERM and then SIGKILL; a silent worker that is using CPU
is not stopped; the host's gate lock and worker cap are honoured; a stale worker lock is removed;
each run's usage line is written; the binary is found in the documented order; a run's writable
folders are its own.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import codex_run  # noqa: E402

FAKE = Path(__file__).resolve().parent / "fake_codex.py"
FAST = {"grace_s": 1.0, "poll_s": 0.05}


def alive(pid: int) -> bool:
    """True while <pid> exists and is not a zombie."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(stat) and not stat.startswith("Z")


def gone_within(pid: int, seconds: float = 5.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return not alive(pid)


class CodexRunCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="codex-run-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin_dir = self.tmp / "bin"
        self.bin_dir.mkdir()
        self.codex = self.bin_dir / "codex"
        self.codex.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
        self.codex.chmod(0o755)
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.locks = self.tmp / "locks"
        self.said: list[str] = []
        patcher = mock.patch.object(codex_run, "_say", self.said.append)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {"FAKE_CODEX_ARGS_OUT": str(self.tmp / "args.json")})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self._kill_leftovers)

    def _kill_leftovers(self) -> None:
        for path in self.tmp.glob("*.pid"):
            try:
                os.kill(int(path.read_text()), signal.SIGKILL)
            except (ValueError, OSError):
                pass

    def run_codex(self, brief: str = "TASK one\nmore", out: str = "out", **kwargs) -> codex_run.RunResult:
        options = {"out_dir": self.tmp / out, "lock_dir": self.locks, "codex_bin": str(self.codex), **FAST, **kwargs}
        return codex_run.run(brief, self.work, **options)

    def fake_args(self, name: str = "args.json") -> dict:
        return json.loads((self.tmp / name).read_text())

    def set_env(self, **values: str) -> None:
        patcher = mock.patch.dict(os.environ, {f"FAKE_CODEX_{k}": v for k, v in values.items()})
        patcher.start()
        self.addCleanup(patcher.stop)


class Success(CodexRunCase):
    def test_last_message_tokens_and_command(self) -> None:
        self.set_env(TOKENS="161132", LAST="all done")
        result = self.run_codex(effort="high")
        self.assertEqual(result.exit, 0)
        self.assertEqual(result.last_message, "all done")
        self.assertEqual(result.tokens, 161132)
        self.assertFalse(result.timed_out or result.idle_killed)
        self.assertIsNone(result.stopped_by)
        self.assertEqual(result.out_path, (self.tmp / "out" / "last.md").resolve())
        argv = self.fake_args()["argv"]
        for pair in (["-c", "project_doc_max_bytes=0"], ["-c", 'model_reasoning_effort="high"'],
                     ["-c", "sandbox_workspace_write.network_access=true"], ["-s", "workspace-write"],
                     ["-C", str(self.work.resolve())], ["-o", str(result.out_path)]):
            self.assertIn(pair, [argv[i:i + 2] for i in range(len(argv) - 1)])
        self.assertIn("--skip-git-repo-check", argv)

    def test_stdin_is_dev_null(self) -> None:
        # Give this process an open pipe as stdin, so a worker that inherited it would see no EOF.
        read_end, write_end = os.pipe()
        saved = os.dup(0)
        os.dup2(read_end, 0)
        try:
            result = self.run_codex(idle_s=5)
        finally:
            os.dup2(saved, 0)
            for fd in (saved, read_end, write_end):
                os.close(fd)
        self.assertEqual(result.exit, 0)  # the fake exits 3 when stdin is anything else
        self.assertIn("stdin: EOF", result.log_path.read_text())

    def test_exit_status_passes_through_and_network_can_be_off(self) -> None:
        self.set_env(EXIT="5")
        result = self.run_codex(network=False)
        self.assertEqual(result.exit, 5)
        self.assertIn("sandbox_workspace_write.network_access=false", self.fake_args()["argv"])

    def test_an_earlier_runs_message_is_not_reported(self) -> None:
        self.set_env(SILENT_S="600")
        out = self.tmp / "out"
        out.mkdir()
        (out / "last.md").write_text("from an earlier run")
        result = self.run_codex(idle_s=0.5)
        self.assertEqual(result.last_message, "")

    def test_usage_line(self) -> None:
        brief = "ROW 7 build the thing\nsecond line"
        self.run_codex(brief=brief, effort="low", preamble="SCOPED WORKER: a project header")
        record = json.loads((self.locks / "usage.jsonl").read_text().splitlines()[-1])
        self.assertEqual(record["brief_sha256"], hashlib.sha256(brief.encode()).hexdigest())
        self.assertEqual(record["brief_first_line"], "ROW 7 build the thing")
        self.assertEqual(record["workdir"], str(self.work.resolve()))
        self.assertEqual(record["effort"], "low")
        self.assertEqual((record["exit"], record["timed_out"], record["idle_killed"], record["tokens"]), (0, False, False, 1234))
        self.assertIsInstance(record["wall_s"], float)
        self.assertIn("time", record)
        prompt = self.fake_args()["argv"][-1].splitlines()
        self.assertTrue(prompt[0].startswith("RUN SCRATCH: "))
        self.assertEqual(prompt[1:3], ["SCOPED WORKER: a project header", "ROW 7 build the thing"])

    def test_usage_log_can_be_named(self) -> None:
        self.run_codex(usage_log=self.tmp / "elsewhere" / "u.jsonl")
        self.assertEqual(len((self.tmp / "elsewhere" / "u.jsonl").read_text().splitlines()), 1)
        self.assertFalse((self.locks / "usage.jsonl").exists())


class Stopping(CodexRunCase):
    def test_idle_stops_the_session_child_included(self) -> None:
        self.set_env(SILENT_S="600", CHILD_PID=str(self.tmp / "child.pid"))
        result = self.run_codex(idle_s=1)
        child = int((self.tmp / "child.pid").read_text())
        self.assertTrue(result.idle_killed)
        self.assertFalse(result.timed_out)
        self.assertEqual(result.stopped_by, "TERM")
        self.assertEqual(result.exit, 128 + signal.SIGTERM)
        self.assertTrue(gone_within(child), f"child {child} survived the stop")
        self.assertTrue(gone_within(self.fake_args()["pid"]))

    def test_a_worker_ignoring_term_gets_kill(self) -> None:
        self.set_env(SILENT_S="600", IGNORE_TERM="1", CHILD_PID=str(self.tmp / "child.pid"))
        result = self.run_codex(idle_s=1)
        child = int((self.tmp / "child.pid").read_text())
        self.assertTrue(result.idle_killed)
        self.assertEqual(result.stopped_by, "KILL")
        self.assertEqual(result.exit, 128 + signal.SIGKILL)
        self.assertTrue(gone_within(child), f"child {child} survived SIGKILL")

    def test_output_is_progress(self) -> None:
        self.set_env(TICKS="8", TICK_S="0.3")
        result = self.run_codex(idle_s=1)
        self.assertFalse(result.idle_killed)
        self.assertEqual(result.exit, 0)

    def test_cpu_work_is_progress_and_its_absence_is_not(self) -> None:
        self.set_env(BUSY_S="2.5")
        working = self.run_codex(idle_s=1)
        self.assertFalse(working.idle_killed, "a silent worker using CPU was stopped")
        self.assertEqual(working.exit, 0)
        stopped = self.run_codex(idle_s=1, cpu_fraction=0, out="out2")
        self.assertTrue(stopped.idle_killed, "with CPU not counted, the same worker should be idle")

    def test_timeout_only_when_asked(self) -> None:
        self.set_env(TICKS="20", TICK_S="0.2")
        result = self.run_codex(idle_s=60, timeout_s=1)
        self.assertTrue(result.timed_out)
        self.assertFalse(result.idle_killed)

    def test_processes_left_running_are_stopped(self) -> None:
        self.set_env(CHILD_PID=str(self.tmp / "child.pid"), LEAVE_CHILD="1")
        result = self.run_codex()
        self.assertEqual(result.exit, 0)
        self.assertIsNone(result.stopped_by)
        self.assertTrue(gone_within(int((self.tmp / "child.pid").read_text())))


class HostEtiquette(CodexRunCase):
    def test_concurrency_cap_waits(self) -> None:
        self.set_env(SILENT_S="1", TIMES=str(self.tmp / "times.txt"))
        results: list[codex_run.RunResult] = []
        threads = [threading.Thread(target=lambda o=o: results.append(self.run_codex(out=o, max_concurrent=1)))
                   for o in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual([r.exit for r in results], [0, 0])
        (s1, e1), (s2, e2) = sorted(tuple(map(float, line.split())) for line in (self.tmp / "times.txt").read_text().splitlines())
        self.assertLessEqual(e1, s2, "two workers ran at once under max_concurrent=1")
        self.assertEqual(sum("worker slots in use" in s for s in self.said), 1)
        self.assertEqual(list(self.locks.glob("worker-*.lock")), [])

    def test_stale_locks_are_removed(self) -> None:
        dead = subprocess.Popen(["true"])
        dead.wait()
        self.locks.mkdir()
        (self.locks / "worker-1-json.lock").write_text(json.dumps({"pid": dead.pid, "codex_pid": dead.pid}))
        (self.locks / "worker-2-bare.lock").write_text(str(dead.pid))
        started = time.monotonic()
        result = self.run_codex(max_concurrent=1)
        self.assertEqual(result.exit, 0)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(list(self.locks.glob("worker-*.lock")), [])
        self.assertEqual(sum("stale worker lock" in s for s in self.said), 2)
        self.assertFalse(any("waiting" in s for s in self.said))

    def test_gate_lock_wait(self) -> None:
        gate = self.tmp / "full-gate.lock"
        gate.mkdir()
        results: list[codex_run.RunResult] = []
        thread = threading.Thread(target=lambda: results.append(self.run_codex(gate_lock=gate)))
        thread.start()
        time.sleep(0.6)
        self.assertFalse((self.tmp / "args.json").exists(), "codex started while the gate lock was held")
        gate.rmdir()
        thread.join(30)
        self.assertEqual(results[0].exit, 0)
        self.assertGreater(results[0].waited_s, 0.5)
        self.assertEqual(sum("gate lock" in s for s in self.said), 1)


class Isolation(CodexRunCase):
    def test_each_run_writes_only_its_own_scratch(self) -> None:
        shared = self.tmp / "named"
        shared.mkdir()
        self.run_codex(out="run-a", extra_dirs=[shared])
        argv_a = self.fake_args()["argv"]
        self.run_codex(out="run-b")
        argv_b = self.fake_args()["argv"]
        add_a = [argv_a[i + 1] for i, a in enumerate(argv_a) if a == "--add-dir"]
        add_b = [argv_b[i + 1] for i, a in enumerate(argv_b) if a == "--add-dir"]
        self.assertEqual(add_a, [str(shared.resolve()), str((self.tmp / "run-a" / "scratch").resolve())])
        self.assertEqual(add_b, [str((self.tmp / "run-b" / "scratch").resolve())])
        self.assertFalse(any("run-b" in a for a in argv_a[:-1]))
        self.assertFalse(any("run-a" in a for a in argv_b[:-1]))
        self.assertIn(add_a[1], argv_a[-1].splitlines()[0])  # the prompt names the run's scratch

    def test_a_shared_root_is_refused(self) -> None:
        (self.tmp / "runs").mkdir()
        with self.assertRaisesRegex(ValueError, "shared root"):
            self.run_codex(out="runs/one", extra_dirs=[self.tmp / "runs"])


class Binary(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="codex-run-bin-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for name in ("arg-codex", "env-codex", "path/codex"):
            path = self.tmp / name
            path.parent.mkdir(exist_ok=True)
            path.write_text("#!/bin/sh\n")
            path.chmod(0o755)

    def test_resolution_order(self) -> None:
        path_dir = str(self.tmp / "path")
        env = {"CODEX_BIN": str(self.tmp / "env-codex"), "PATH": path_dir}
        self.assertEqual(codex_run.resolve_codex_bin(str(self.tmp / "arg-codex"), env), str(self.tmp / "arg-codex"))
        self.assertEqual(codex_run.resolve_codex_bin(None, env), str(self.tmp / "env-codex"))
        self.assertEqual(codex_run.resolve_codex_bin(None, {"PATH": path_dir}), str(self.tmp / "path" / "codex"))

    def test_none_found_names_all_three(self) -> None:
        with self.assertRaises(codex_run.CodexNotFound) as caught:
            codex_run.resolve_codex_bin(None, {"PATH": str(self.tmp / "empty")})
        for name in ("codex_bin", "CODEX_BIN", "PATH"):
            self.assertIn(name, str(caught.exception))

    def test_a_named_binary_that_is_missing_is_an_error(self) -> None:
        with self.assertRaisesRegex(codex_run.CodexNotFound, "CODEX_BIN"):
            codex_run.resolve_codex_bin(None, {"CODEX_BIN": str(self.tmp / "nope"), "PATH": str(self.tmp / "path")})


class Tokens(unittest.TestCase):
    def test_formats(self) -> None:
        self.assertEqual(codex_run.parse_tokens("x\ntokens used\n161,132\nfinal"), 161132)
        self.assertEqual(codex_run.parse_tokens("tokens used: 2,000\n"), 2000)
        self.assertEqual(codex_run.parse_tokens("tokens used\n5\n...\ntokens used\n7\n"), 7)
        self.assertIsNone(codex_run.parse_tokens("no count here"))


class CommandLine(CodexRunCase):
    def cli(self, *extra: str, **popen: object) -> subprocess.Popen:
        brief = self.tmp / "brief.txt"
        brief.write_text("TASK from the CLI\n")
        env = {**os.environ, "PYTHONPATH": str(ROOT)}
        cmd = [sys.executable, "-m", "codex_run", "run", "--brief", str(brief), "--workdir", str(self.work),
               "--out", str(self.tmp / "cli-out"), "--lock-dir", str(self.locks), "--codex-bin", str(self.codex),
               "--grace-s", "1", *extra]
        return subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **popen)

    def test_summary_line_and_exit(self) -> None:
        proc = self.cli()
        out, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 0, err)
        self.assertEqual(len(out.splitlines()), 1)
        self.assertRegex(out, r"^CODEX_RUN exit=0 tokens=1234 wall_s=[0-9.]+ idle_killed=false timed_out=false ")

    def test_a_stopped_run_exits_124(self) -> None:
        self.set_env(SILENT_S="600")
        proc = self.cli("--idle-s", "1")
        out, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 124, err)
        self.assertIn("idle_killed=true", out)

    def test_the_binary_error_exits_127(self) -> None:
        brief = self.tmp / "b.txt"
        brief.write_text("x")
        env = {k: v for k, v in os.environ.items() if k != "CODEX_BIN"}
        env.update(PYTHONPATH=str(ROOT), PATH=str(self.tmp / "nothing-here"))
        proc = subprocess.run([sys.executable, "-m", "codex_run", "run", "--brief", str(brief), "--workdir", str(self.work),
                               "--out", str(self.tmp / "o"), "--lock-dir", str(self.locks)],
                              env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 127)
        self.assertIn("CODEX_BIN", proc.stderr)

    def test_stopping_the_cli_stops_the_worker(self) -> None:
        child_file = self.tmp / "child.pid"
        self.set_env(SILENT_S="600", CHILD_PID=str(child_file))
        proc = self.cli()
        end = time.monotonic() + 30
        while not child_file.exists() and time.monotonic() < end:
            time.sleep(0.05)
        time.sleep(0.2)
        proc.send_signal(signal.SIGTERM)
        _, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 128 + signal.SIGTERM, err)
        self.assertTrue(gone_within(int(child_file.read_text())))
        self.assertTrue(gone_within(self.fake_args()["pid"]))
        record = json.loads((self.locks / "usage.jsonl").read_text().splitlines()[-1])
        self.assertTrue(record["interrupted"])


if __name__ == "__main__":
    unittest.main()
