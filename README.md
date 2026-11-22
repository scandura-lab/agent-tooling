# agent-tooling

Shared, tested tools for Joe Scandura's AI-agent projects (Joe, 2026-11-22). It is the sibling of `agent-guidance`, which holds the rules as prose only; this repository holds code. Each module has its own tests here, and each project carries a stamped copy that its own test checks.

| Path | What it is |
|---|---|
| `codex_run.py` | Start, watch, time out, stop and record one headless `codex exec` worker per task. |
| `tools/stamp.py` | Puts a stamped copy of named modules into a project folder, and checks such a copy against this repository. |
| `tools/copy_check.py` | Copied, stamped, beside the modules: checks the copies using only the copies. |
| `tests/` | The tests, with `tests/fake_codex.py` standing in for codex: `python3 -m unittest discover -s tests`. |

Planned modules, each added only when a second project needs it: focused testing (choosing the tests a change affects), landing, and the module map.

Rules for code here: Python standard library only, running under macOS's `/usr/bin/python3` (3.9) and newer. A module knows no project; a project's adapter supplies its paths, header text and binary.

## Carrying a copy in a project

A project keeps the copies in one folder that holds nothing else, for example `tools/agent_tooling/`:

```sh
cd ~/dev/agent-tooling && git pull --ff-only && python3 tools/stamp.py into /path/to/project/tools/agent_tooling codex_run
cd ~/dev/agent-tooling && python3 tools/stamp.py check /path/to/project/tools/agent_tooling codex_run
```

`check` prints `CURRENT`, `BEHIND` (refresh with `into`) or `EDITED IN PLACE` for each copy. Each copy starts with one comment line naming this repository's commit and the sha256 of the text below it; the copy still runs (`PYTHONPATH=tools/agent_tooling python3 -m codex_run ...`). The project's own test runs `python3 tools/agent_tooling/copy_check.py tools/agent_tooling`, which exits 0 only when every copy is `INTACT`, with no clone of this repository. A copy is never edited in place; change it here, then refresh each project's copy.

## codex_run

```python
codex_run.run(brief, workdir, *, out_dir, lock_dir, effort="medium", idle_s=1800, timeout_s=None,
              codex_bin=None, sandbox="workspace-write", extra_dirs=(), network=True, gate_lock=None,
              max_concurrent=2, usage_log=None, log_path=None, preamble="", grace_s=10,
              cpu_fraction=0.05, poll_s=2) -> RunResult
```

`RunResult`: `exit`, `timed_out`, `idle_killed`, `last_message`, `tokens`, `wall_s`, `log_path`, `out_path`, `scratch_dir`, `stopped_by` (`"TERM"`, `"KILL"` or `None`), `waited_s`. `exit` is codex's exit status, or 128 + N when signal N ended it.

- **Binary:** `codex_bin`, else `$CODEX_BIN`, else `codex` on `PATH`; otherwise `CodexNotFound` names all three. A project's adapter finds a moving install path (a VS Code extension, for example); the library does not.
- **Start:** `codex exec --skip-git-repo-check -s SANDBOX -c project_doc_max_bytes=0 -c model_reasoning_effort="EFFORT" -c sandbox_workspace_write.network_access=true|false -C WORKDIR --add-dir ... -o OUT_DIR/last.md PROMPT`, in a new session (`start_new_session`), stdin from `/dev/null` (codex otherwise waits on "Reading additional input from stdin"). `project_doc_max_bytes=0` keeps the project's `AGENTS.md` onboarding out of the worker's reading.
- **Writable folders:** the workdir, each folder in `extra_dirs`, and the run's own `OUT_DIR/scratch/`, nothing else. The prompt's first line names that scratch folder (`RUN SCRATCH: ...`). An `extra_dirs` entry that contains `out_dir` is refused: a shared root would let every worker write every other worker's scratch. Codex's workspace sandbox also lets every worker write `/tmp` (on macOS, all of `/private/tmp`), so keep run folders outside `/tmp` where the isolation matters.
- **Prompt:** the scratch line, then `preamble` (a project's header), then `brief`. The usage line's sha256 and first line are the brief's alone.
- **Liveness:** no total-time limit by default ("hang = no progress"). The run is stopped when it has made no progress for `idle_s`: progress is the log growing, or the worker's processes using at least `cpu_fraction` of one core since the log last grew (codex prints nothing while one of its commands, a long test run for example, is working). `cpu_fraction=0` counts the log alone. `timeout_s` stops the run after that long in all, only when a caller asks.
- **Stop:** SIGTERM to every process group in the worker's session (codex may start its commands in groups of their own), `grace_s` (10 s), then SIGKILL to whatever is left; `stopped_by` records which ended it. Processes a worker leaves running after codex exits are stopped the same way.
- **Host etiquette:** before starting, wait while `gate_lock` exists (one line when the wait begins). Then take one of `max_concurrent` slots: a lock file per running worker in `lock_dir` holding the wrapper's and codex's pids, taken under an `flock`; a lock whose pids are all gone is removed, with one line. A gate that begins while a run waits for a slot sends it back to waiting for the gate. Both apply at start only; a gate that begins later does not pause a running worker.
- **Record:** after each run, one JSON line in `usage_log`, else `lock_dir/usage.jsonl`: `time`, `brief_sha256`, `brief_first_line`, `workdir`, `out_dir`, `effort`, `exit`, `timed_out`, `idle_killed`, `stopped_by`, `interrupted`, `tokens` (from codex's "tokens used" line), `wall_s`, `waited_s`.

From a shell:

```sh
PYTHONPATH=~/dev/agent-tooling python3 -m codex_run run --brief FILE --workdir DIR --out DIR \
  [--effort E] [--idle-s N] [--timeout-s N] [--gate-lock PATH] [--max-concurrent N] [--lock-dir DIR] \
  [--add-dir DIR ...] [--codex-bin PATH] [--sandbox MODE] [--no-network] [--usage-log FILE] [--log FILE] \
  [--preamble FILE] [--grace-s N] [--cpu-fraction F]
```

It prints one line, `CODEX_RUN exit=.. tokens=.. wall_s=.. idle_killed=.. timed_out=.. stopped_by=.. last=.. log=..`, and exits with codex's status, 124 when it stopped the worker, 127 when no codex binary is found, 2 for a bad argument, or 128 + N when signal N stopped the CLI itself (it stops the worker first). `--lock-dir` defaults to `$CODEX_RUN_LOCK_DIR`, else `~/.codex-run`.
