"""Start, watch, stop and record one headless `codex exec` worker per task.

What it does: `run()` waits while the host's gate lock is held and while every worker slot is
taken, starts `codex exec` in its own session with stdin from /dev/null, watches it, stops it when
it makes no progress for `idle_s` seconds (or after `timeout_s`, only when a caller asks for one),
and appends one usage line per run. `python3 -m codex_run run ...` is the same door from a shell.
Where it fits: the one way an architect runs a Codex worker. A project's adapter supplies its
header text, paths, gate lock and binary; this module knows no project. Standard library only,
Python 3.9 and newer, macOS and Linux. A project carries a stamped copy (see README.md).

Progress is the log growing, or the worker's processes using at least `cpu_fraction` of one core
since the log last grew: a long, silent test run is progress, a stalled model call is not. A stop
sends SIGTERM to every process group in the worker's session (codex may start its commands in
groups of their own), waits `grace_s`, then sends SIGKILL to whatever is left.
"""
from __future__ import annotations

import argparse
import datetime
import fcntl
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

GRACE_S = 10.0
POLL_S = 2.0
CPU_FRACTION = 0.05
STOPPED_EXIT = 124
LAST_NAME = "last.md"
LOG_NAME = "codex.log"
SCRATCH_NAME = "scratch"
USAGE_NAME = "usage.jsonl"
LOCK_PREFIX = "worker-"
MUTEX_NAME = "slots.mutex"
DEFAULT_LOCK_DIR = Path.home() / ".codex-run"
TOKENS_MARK = "tokens used"


class CodexNotFound(RuntimeError):
    """No codex binary: none named by the caller, by CODEX_BIN, or on PATH."""


@dataclass(frozen=True)
class RunResult:
    """One worker run. `exit` is codex's status (128 + N when signal N ended it)."""

    exit: int
    timed_out: bool
    idle_killed: bool
    last_message: str
    tokens: int | None
    wall_s: float
    log_path: Path
    out_path: Path
    scratch_dir: Path
    stopped_by: str | None  # "TERM", "KILL", or None when codex ended on its own
    waited_s: float

    @property
    def stopped(self) -> bool:
        return self.timed_out or self.idle_killed

    def summary(self) -> str:
        tokens = "none" if self.tokens is None else str(self.tokens)
        return (
            f"CODEX_RUN exit={self.exit} tokens={tokens} wall_s={self.wall_s:.1f} "
            f"idle_killed={str(self.idle_killed).lower()} timed_out={str(self.timed_out).lower()} "
            f"stopped_by={self.stopped_by or 'none'} last={self.out_path} log={self.log_path}"
        )


def _say(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# --- the binary and the command ---------------------------------------------------------------


def resolve_codex_bin(codex_bin: str | None = None, env: Mapping[str, str] | None = None) -> str:
    """The codex to run: `codex_bin`, else $CODEX_BIN, else `codex` on PATH; an absolute path."""
    env = os.environ if env is None else env
    for source, value in (("codex_bin", codex_bin), ("CODEX_BIN", env.get("CODEX_BIN"))):
        if value:
            found = shutil.which(value, path=env.get("PATH"))
            if found is None:
                raise CodexNotFound(f"{source} names {value!r}, which is not an executable file")
            return os.path.abspath(found)
    found = shutil.which("codex", path=env.get("PATH"))
    if found is None:
        raise CodexNotFound(
            "no codex binary: pass codex_bin (CLI: --codex-bin), set CODEX_BIN, or put codex on PATH"
        )
    return os.path.abspath(found)


def build_args(
    codex: str,
    prompt: str,
    *,
    workdir: Path,
    effort: str,
    sandbox: str,
    network: bool,
    add_dirs: Iterable[Path],
    last_path: Path,
) -> list[str]:
    """The `codex exec` command line. Writable roots are exactly `add_dirs`, beside the workdir."""
    args = [
        codex, "exec", "--skip-git-repo-check",
        "-s", sandbox,
        "-c", "project_doc_max_bytes=0",
        "-c", f'model_reasoning_effort="{effort}"',
        "-c", f"sandbox_workspace_write.network_access={'true' if network else 'false'}",
        "-C", str(workdir),
    ]
    for folder in add_dirs:
        args += ["--add-dir", str(folder)]
    return [*args, "-o", str(last_path), prompt]


def scratch_line(scratch: Path) -> str:
    """The prompt's first line: where this run, and no other, may write outside the working copy."""
    return (
        f"RUN SCRATCH: {scratch} is this run's own writable folder outside the working copy; "
        "wherever this brief says SCRATCH, use this folder."
    )


def compose_prompt(brief: str, scratch: Path, preamble: str = "") -> str:
    head = scratch_line(scratch) + "\n"
    if preamble:
        head += preamble.rstrip("\n") + "\n"
    return head + brief


def parse_tokens(log_text: str) -> int | None:
    """The last count codex printed after "tokens used" (same line or the next), or None."""
    lines = log_text.splitlines()
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i]
        at = line.lower().find(TOKENS_MARK)
        if at < 0:
            continue
        for candidate in (line[at + len(TOKENS_MARK):], lines[i + 1] if i + 1 < len(lines) else ""):
            digits = candidate.strip().lstrip(":").strip().replace(",", "")
            if digits.isdigit():
                return int(digits)
    return None


# --- host etiquette: the gate lock and the worker slots ---------------------------------------


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _lock_pids(path: Path) -> list[int]:
    """The pids a worker lock holds: a JSON object's pid and codex_pid, or a bare pid."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    values = [data.get("pid"), data.get("codex_pid")] if isinstance(data, dict) else [data]
    return [v for v in values if isinstance(v, int) and not isinstance(v, bool)]


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class _Mutex:
    """An flock on <lock_dir>/slots.mutex, so counting and taking a slot is one step."""

    def __init__(self, lock_dir: Path) -> None:
        self.path = lock_dir / MUTEX_NAME

    def __enter__(self) -> _Mutex:
        self.handle = open(self.path, "a")
        fcntl.flock(self.handle, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc: object) -> None:
        fcntl.flock(self.handle, fcntl.LOCK_UN)
        self.handle.close()


def live_worker_locks(lock_dir: Path) -> list[Path]:
    """The worker locks in <lock_dir> whose pids live; removes each stale one, saying so."""
    live = []
    for path in sorted(lock_dir.glob(LOCK_PREFIX + "*.lock")):
        pids = _lock_pids(path)
        if any(_pid_alive(pid) for pid in pids):
            live.append(path)
        else:
            path.unlink(missing_ok=True)
            _say(f"CODEX_RUN removed a stale worker lock {path.name} (pids {pids or 'unreadable'} are gone)")
    return live


def acquire_slot(lock_dir: Path, max_concurrent: int, poll_s: float, info: dict) -> Path:
    """Wait for a free slot, then hold it: a lock file in <lock_dir> holding this process's pid."""
    if max_concurrent < 1:
        raise ValueError(f"max_concurrent must be at least 1, not {max_concurrent}")
    lock_dir.mkdir(parents=True, exist_ok=True)
    waiting = False
    while True:
        with _Mutex(lock_dir):
            live = live_worker_locks(lock_dir)
            if len(live) < max_concurrent:
                path = lock_dir / f"{LOCK_PREFIX}{os.getpid()}-{os.urandom(4).hex()}.lock"
                _write_atomic(path, json.dumps({**info, "pid": os.getpid()}))
                return path
        if not waiting:
            _say(f"CODEX_RUN waiting: {len(live)} of {max_concurrent} worker slots in use ({lock_dir})")
            waiting = True
        time.sleep(poll_s)


def wait_for_gate(gate_lock: Path | None, poll_s: float) -> None:
    """Return once <gate_lock> does not exist, saying one line if it has to wait."""
    if gate_lock is None or not os.path.lexists(gate_lock):
        return
    _say(f"CODEX_RUN waiting: the gate lock {gate_lock} is held")
    while os.path.lexists(gate_lock):
        time.sleep(poll_s)


# --- the worker's processes -------------------------------------------------------------------


def _cpu_seconds(text: str) -> float:
    """ps's cumulative CPU time: [D-]HH:MM:SS, MM:SS.ss or similar."""
    try:
        days = 0
        if "-" in text:
            head, text = text.split("-", 1)
            days = int(head)
        total = 0.0
        for part in text.split(":"):
            total = total * 60 + float(part)
        return days * 86400 + total
    except ValueError:
        return 0.0


def session_processes(sid: int) -> dict[int, tuple[int, float]]:
    """pid -> (process group, CPU seconds) for each live process in session <sid>."""
    try:
        out = subprocess.run(
            ["ps", "-A", "-o", "pid=,pgid=,stat=,time="],
            capture_output=True, text=True, timeout=60, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    found: dict[int, tuple[int, float]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[2].startswith("Z"):
            continue
        try:
            pid, pgid = int(parts[0]), int(parts[1])
            if os.getsid(pid) != sid:
                continue
        except (ValueError, OSError):
            continue
        found[pid] = (pgid, _cpu_seconds(parts[3]))
    return found


class _CpuMeter:
    """CPU seconds the session has used since the meter began, summed over each process seen."""

    def __init__(self, sid: int) -> None:
        self.sid = sid
        self.seen: dict[int, float] = {}
        self.used = 0.0

    def sample(self) -> float:
        for pid, (_, cpu) in session_processes(self.sid).items():
            self.used += max(0.0, cpu - self.seen.get(pid, 0.0))
            self.seen[pid] = cpu
        return self.used


def _signal_session(proc: subprocess.Popen, sig: int) -> None:
    groups = {pgid for pgid, _ in session_processes(proc.pid).values()}
    if proc.poll() is None:
        groups.add(proc.pid)  # the leader's own group; its id cannot be reused while it is unreaped
    for pgid in groups:
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def stop_session(proc: subprocess.Popen, grace_s: float) -> str | None:
    """SIGTERM every group in the worker's session, SIGKILL after <grace_s>; which one ended it.

    None when nothing of the session was left to stop.
    """
    if proc.poll() is not None and not session_processes(proc.pid):
        return None
    _signal_session(proc, signal.SIGTERM)
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if proc.poll() is not None and not session_processes(proc.pid):
            return "TERM"
        time.sleep(0.1)
    _signal_session(proc, signal.SIGKILL)
    proc.wait()
    settle = time.monotonic() + 5
    while session_processes(proc.pid) and time.monotonic() < settle:
        _signal_session(proc, signal.SIGKILL)
        time.sleep(0.1)
    return "KILL"


def _exit_status(returncode: int) -> int:
    return returncode if returncode >= 0 else 128 - returncode


# --- the run ----------------------------------------------------------------------------------


def _usage_line(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def run(
    brief: str,
    workdir: Path,
    *,
    out_dir: Path,
    lock_dir: Path,
    effort: str = "medium",
    idle_s: float = 1800,
    timeout_s: float | None = None,
    codex_bin: str | None = None,
    sandbox: str = "workspace-write",
    extra_dirs: Iterable[Path] = (),
    network: bool = True,
    gate_lock: Path | None = None,
    max_concurrent: int = 2,
    usage_log: Path | None = None,
    log_path: Path | None = None,
    preamble: str = "",
    grace_s: float = GRACE_S,
    cpu_fraction: float = CPU_FRACTION,
    poll_s: float = POLL_S,
) -> RunResult:
    """Run one codex worker on <brief> in <workdir>; return when it has ended or been stopped.

    Writes <out_dir>/last.md (codex's last message), <out_dir>/codex.log (or <log_path>) and
    <out_dir>/scratch/, the run's own writable folder, named on the prompt's first line. The usage
    line goes to <usage_log>, else <lock_dir>/usage.jsonl. <preamble> is a project's header,
    sent before the brief but not counted in the usage line's brief sha256 and first line.
    """
    codex = resolve_codex_bin(codex_bin)
    workdir = Path(workdir).resolve()
    if not workdir.is_dir():
        raise ValueError(f"workdir {workdir} is not a folder")
    out_dir = Path(out_dir).resolve()
    lock_dir = Path(lock_dir).resolve()
    extra = [Path(d).resolve() for d in extra_dirs]
    for folder in extra:
        if not folder.is_dir():
            raise ValueError(f"extra dir {folder} is not a folder")
        if folder == out_dir or folder in out_dir.parents:
            raise ValueError(
                f"extra dir {folder} holds this run's out_dir: a shared root lets every worker "
                "write every other worker's scratch; name the one folder this worker needs"
            )
    scratch = out_dir / SCRATCH_NAME
    scratch.mkdir(parents=True, exist_ok=True)
    last_path = out_dir / LAST_NAME
    last_path.unlink(missing_ok=True)  # an earlier run's message must not read as this one's
    log_path = Path(log_path).resolve() if log_path else out_dir / LOG_NAME
    log_path.parent.mkdir(parents=True, exist_ok=True)
    usage_path = Path(usage_log) if usage_log else lock_dir / USAGE_NAME
    gate = Path(gate_lock) if gate_lock else None

    first_line = brief.strip().splitlines()[0] if brief.strip() else ""
    record = {
        "brief_sha256": hashlib.sha256(brief.encode("utf-8")).hexdigest(),
        "brief_first_line": first_line[:200],
        "workdir": str(workdir),
        "out_dir": str(out_dir),
        "effort": effort,
    }
    args = build_args(
        codex, compose_prompt(brief, scratch, preamble),
        workdir=workdir, effort=effort, sandbox=sandbox, network=network,
        add_dirs=[*extra, scratch], last_path=last_path,
    )

    queued = time.monotonic()
    slot: Path | None = None
    proc: subprocess.Popen | None = None
    timed_out = idle_killed = False
    stopped_by: str | None = None
    finished = False
    try:
        while True:
            wait_for_gate(gate, poll_s)
            slot = acquire_slot(lock_dir, max_concurrent, poll_s, {"workdir": str(workdir), "out_dir": str(out_dir)})
            if gate is None or not os.path.lexists(gate):
                break
            slot.unlink(missing_ok=True)  # a gate began while this run waited for a slot
            slot = None
        waited_s = time.monotonic() - queued

        started = time.monotonic()
        with open(os.devnull, "rb") as devnull, open(log_path, "wb") as log:
            proc = subprocess.Popen(
                args, stdin=devnull, stdout=log, stderr=subprocess.STDOUT,
                cwd=str(workdir), start_new_session=True,
            )
        _write_atomic(slot, json.dumps({"pid": os.getpid(), "codex_pid": proc.pid, **record}))

        meter = _CpuMeter(proc.pid)
        cpu_every = max(poll_s, idle_s / 10)
        quiet_since, size_seen, next_sample = started, -1, started
        cpu_base: tuple[float, float] | None = None
        while True:
            try:
                proc.wait(timeout=poll_s)
                break
            except subprocess.TimeoutExpired:
                pass
            now = time.monotonic()
            size = log_path.stat().st_size if log_path.exists() else 0
            if size != size_seen:
                size_seen, quiet_since, cpu_base = size, now, None
            if cpu_fraction > 0 and now >= next_sample:
                next_sample = now + cpu_every
                used = meter.sample()
                if cpu_base is None:
                    cpu_base = (now, used)
                elif used - cpu_base[1] >= cpu_fraction * (now - cpu_base[0]):
                    quiet_since, cpu_base = now, (now, used)  # silent, but working
            if timeout_s is not None and now - started >= timeout_s:
                timed_out = True
                break
            if now - quiet_since >= idle_s:
                idle_killed = True
                break
        if timed_out or idle_killed:
            stopped_by = stop_session(proc, grace_s)
        else:
            leftover = stop_session(proc, grace_s)
            if leftover:
                _say(f"CODEX_RUN stopped processes the worker left running (by {leftover})")
        finished = True
    finally:
        if proc is not None:
            if not finished:
                stopped_by = stop_session(proc, grace_s)
            proc.wait()
            wall_s = time.monotonic() - started
            log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
            result = RunResult(
                exit=_exit_status(proc.returncode),
                timed_out=timed_out,
                idle_killed=idle_killed,
                last_message=last_path.read_text(encoding="utf-8", errors="replace") if last_path.exists() else "",
                tokens=parse_tokens(log_text),
                wall_s=wall_s,
                log_path=log_path,
                out_path=last_path,
                scratch_dir=scratch,
                stopped_by=stopped_by,
                waited_s=waited_s,
            )
            _usage_line(usage_path, {
                "time": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                **record,
                "exit": result.exit,
                "timed_out": timed_out,
                "idle_killed": idle_killed,
                "stopped_by": stopped_by,
                "interrupted": not finished,
                "tokens": result.tokens,
                "wall_s": round(wall_s, 1),
                "waited_s": round(waited_s, 1),
            })
        if slot is not None:
            slot.unlink(missing_ok=True)
    return result


# --- the command line -------------------------------------------------------------------------


class Interrupted(BaseException):
    """The CLI received a stop signal; run() stops the worker on its way out."""

    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


def _interrupt(signum: int, _frame: object) -> None:
    raise Interrupted(signum)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m codex_run", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="verb", required=True)
    cmd = sub.add_parser("run", help="run one codex worker on a brief")
    cmd.add_argument("--brief", type=Path, required=True, help="the brief, a text file")
    cmd.add_argument("--workdir", type=Path, required=True, help="the worker's working copy")
    cmd.add_argument("--out", type=Path, required=True, help="this run's folder: last.md, codex.log, scratch/")
    cmd.add_argument("--effort", default="medium", help="model_reasoning_effort (default medium)")
    cmd.add_argument("--idle-s", type=float, default=1800, help="stop after this long without progress (default 1800)")
    cmd.add_argument("--timeout-s", type=float, default=None, help="stop after this long in all (default: never)")
    cmd.add_argument("--gate-lock", type=Path, default=None, help="wait while this path exists before starting")
    cmd.add_argument("--max-concurrent", type=int, default=2, help="workers at once on this host (default 2)")
    cmd.add_argument("--lock-dir", type=Path, default=None, help=f"worker locks and usage.jsonl (default $CODEX_RUN_LOCK_DIR or {DEFAULT_LOCK_DIR})")
    cmd.add_argument("--add-dir", dest="add_dirs", type=Path, action="append", default=[], help="another folder the worker may write (repeatable)")
    cmd.add_argument("--codex-bin", default=None, help="the codex binary (default $CODEX_BIN, then codex on PATH)")
    cmd.add_argument("--sandbox", default="workspace-write", help="codex -s (default workspace-write)")
    cmd.add_argument("--no-network", action="store_true", help="turn the sandbox's network access off")
    cmd.add_argument("--usage-log", type=Path, default=None, help="the usage line's file (default <lock-dir>/usage.jsonl)")
    cmd.add_argument("--log", type=Path, default=None, help="codex's output (default <out>/codex.log)")
    cmd.add_argument("--preamble", type=Path, default=None, help="a header file sent before the brief")
    cmd.add_argument("--grace-s", type=float, default=GRACE_S, help=f"SIGTERM to SIGKILL (default {GRACE_S:g})")
    cmd.add_argument("--cpu-fraction", type=float, default=CPU_FRACTION, help=f"CPU share that counts as progress; 0 turns it off (default {CPU_FRACTION:g})")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    lock_dir = args.lock_dir or Path(os.environ.get("CODEX_RUN_LOCK_DIR") or DEFAULT_LOCK_DIR)
    previous = {sig: signal.signal(sig, _interrupt) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    try:
        result = run(
            args.brief.read_text(encoding="utf-8"),
            args.workdir,
            out_dir=args.out,
            lock_dir=lock_dir,
            effort=args.effort,
            idle_s=args.idle_s,
            timeout_s=args.timeout_s,
            codex_bin=args.codex_bin,
            sandbox=args.sandbox,
            extra_dirs=args.add_dirs,
            network=not args.no_network,
            gate_lock=args.gate_lock,
            max_concurrent=args.max_concurrent,
            usage_log=args.usage_log,
            log_path=args.log,
            preamble=args.preamble.read_text(encoding="utf-8") if args.preamble else "",
            grace_s=args.grace_s,
            cpu_fraction=args.cpu_fraction,
        )
    except CodexNotFound as exc:
        print(f"CODEX_RUN ERROR: {exc}", file=sys.stderr)
        return 127
    except (ValueError, OSError) as exc:
        print(f"CODEX_RUN ERROR: {exc}", file=sys.stderr)
        return 2
    except Interrupted as exc:
        print(f"CODEX_RUN interrupted by {signal.Signals(exc.signum).name}; the worker was stopped", file=sys.stderr)
        return 128 + exc.signum
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    print(result.summary(), flush=True)
    return STOPPED_EXIT if result.stopped else result.exit


if __name__ == "__main__":
    sys.exit(main())
