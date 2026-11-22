"""Witness for the stamped copy a project carries (`tools/stamp.py`, `tools/copy_check.py`).

Each test builds a small source repository with the real tools and module, stamps a project
folder from it, and runs both tools as a project would. Run from the repository root:
`python3 -m unittest discover -s tests`.
Guarantees protected: a copy is never edited in place unnoticed, a project's own test can detect
such an edit without a clone of this repository, and a stamped module still runs.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def run(*args: object, cwd: Path | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([str(a) for a in args], cwd=cwd, capture_output=True, text=True, env=env or GIT_ENV)


class StampedCopy(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="agent-tooling-stamp-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.source = self.tmp / "agent-tooling"
        (self.source / "tools").mkdir(parents=True)
        for name in ("tools/stamp.py", "tools/copy_check.py", "codex_run.py"):
            shutil.copy(ROOT / name, self.source / name)
        run("git", "init", "-q", cwd=self.source)
        run("git", "add", ".", cwd=self.source)
        run("git", "commit", "-qm", "source", cwd=self.source)
        self.dest = self.tmp / "project" / "vendor"
        self.dest.mkdir(parents=True)
        self.stamp = self.source / "tools" / "stamp.py"

    def test_a_fresh_copy_is_current_intact_and_runs(self) -> None:
        made = run(sys.executable, self.stamp, "into", self.dest, "codex_run")
        self.assertEqual(made.returncode, 0, made.stderr)
        self.assertEqual(run(sys.executable, self.stamp, "check", self.dest, "codex_run").returncode, 0)
        local = run(sys.executable, self.dest / "copy_check.py", self.dest)
        self.assertEqual(local.returncode, 0, local.stdout)
        self.assertEqual(local.stdout.count("INTACT"), 2)
        helped = run(sys.executable, "-m", "codex_run", "run", "--help", env={**os.environ, "PYTHONPATH": str(self.dest)})
        self.assertEqual(helped.returncode, 0, helped.stderr)

    def test_an_edit_in_place_is_caught_with_and_without_the_source(self) -> None:
        run(sys.executable, self.stamp, "into", self.dest, "codex_run")
        copy = self.dest / "codex_run.py"
        copy.write_text(copy.read_text() + "\n# a local change\n")
        self.assertEqual(run(sys.executable, self.stamp, "check", self.dest, "codex_run").returncode, 1)
        local = run(sys.executable, self.dest / "copy_check.py", self.dest)
        self.assertEqual(local.returncode, 1)
        self.assertIn(f"EDITED IN PLACE {copy}", local.stdout)

    def test_a_copy_behind_the_source_says_so(self) -> None:
        run(sys.executable, self.stamp, "into", self.dest, "codex_run")
        module = self.source / "codex_run.py"
        module.write_text(module.read_text() + "\n# a newer source\n")
        run("git", "commit", "-qam", "newer", cwd=self.source)
        checked = run(sys.executable, self.stamp, "check", self.dest, "codex_run")
        self.assertEqual(checked.returncode, 2)
        self.assertIn("BEHIND", checked.stdout)

    def test_uncommitted_source_is_refused(self) -> None:
        module = self.source / "codex_run.py"
        module.write_text(module.read_text() + "\n# not committed\n")
        made = run(sys.executable, self.stamp, "into", self.dest, "codex_run")
        self.assertEqual(made.returncode, 1)
        self.assertIn("REFUSED", made.stderr)


if __name__ == "__main__":
    unittest.main()
