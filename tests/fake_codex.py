#!/usr/bin/env python3
"""A stand-in for `codex exec`, for codex_run's tests; it never calls a model.

It parses the options codex_run passes, checks that stdin is /dev/null (exit 3 otherwise), prints
a header, writes its last message to the -o file, and prints a "tokens used" line as codex does.
Environment variables steer it:
  FAKE_CODEX_ARGS_OUT  write {"argv", "pid", "pgid", "sid"} as JSON to this path
  FAKE_CODEX_TIMES     append "start end" (epoch seconds) to this path
  FAKE_CODEX_TICKS, FAKE_CODEX_TICK_S   print a line every TICK_S seconds, TICKS times
  FAKE_CODEX_SILENT_S  then sleep this long without printing (go silent)
  FAKE_CODEX_BUSY_S    then run a silent child that burns CPU this long, and wait for it
  FAKE_CODEX_IGNORE_TERM   ignore SIGTERM (its children inherit that)
  FAKE_CODEX_CHILD_PID     start a sleeping child in a process group of its own (as codex does
                           for its commands) and write its pid to this path
  FAKE_CODEX_LEAVE_CHILD   with CHILD_PID: exit at once, leaving that child running
  FAKE_CODEX_TOKENS    the count to print (default 1234); FAKE_CODEX_EXIT the exit status
"""
import json
import os
import signal
import subprocess
import sys
import time

TAKES_VALUE = {"-c", "-s", "-C", "-o", "--add-dir", "-m", "-p"}


def parse(argv):
    if not argv or argv[0] != "exec":
        raise SystemExit(f"fake codex: expected 'exec', got {argv[:1]}")
    opts, prompt, i = {}, None, 1
    while i < len(argv):
        arg = argv[i]
        if arg in TAKES_VALUE:
            opts.setdefault(arg, []).append(argv[i + 1])
            i += 2
        elif arg.startswith("-"):
            opts.setdefault(arg, []).append(True)
            i += 1
        else:
            prompt = arg
            i += 1
    if prompt is None:
        raise SystemExit("fake codex: no prompt")
    return opts, prompt


def say(line):
    print(line, flush=True)


def main():
    began = time.time()
    env = os.environ
    opts, prompt = parse(sys.argv[1:])
    if env.get("FAKE_CODEX_ARGS_OUT"):
        with open(env["FAKE_CODEX_ARGS_OUT"], "w") as f:
            json.dump({"argv": sys.argv[1:], "pid": os.getpid(), "pgid": os.getpgid(0), "sid": os.getsid(0)}, f)
    stdin, devnull = os.fstat(0), os.stat(os.devnull)
    if (stdin.st_dev, stdin.st_ino) != (devnull.st_dev, devnull.st_ino) or sys.stdin.read() != "":
        say("FAKE: stdin is not /dev/null")
        return 3
    say("stdin: EOF")
    say("OpenAI Codex (fake)")
    say(f"workdir: {opts['-C'][0]}")
    say("user")
    say(prompt)
    if env.get("FAKE_CODEX_IGNORE_TERM"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if env.get("FAKE_CODEX_CHILD_PID"):
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(600)"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            preexec_fn=os.setpgrp,
        )
        with open(env["FAKE_CODEX_CHILD_PID"], "w") as f:
            f.write(str(child.pid))
        if env.get("FAKE_CODEX_LEAVE_CHILD"):
            os._exit(0)
    for _ in range(int(env.get("FAKE_CODEX_TICKS", "0"))):
        time.sleep(float(env.get("FAKE_CODEX_TICK_S", "0.2")))
        say("exec working")
    if env.get("FAKE_CODEX_SILENT_S"):
        time.sleep(float(env["FAKE_CODEX_SILENT_S"]))
    if env.get("FAKE_CODEX_BUSY_S"):
        burn = f"import time\nend = time.time() + {float(env['FAKE_CODEX_BUSY_S'])}\nwhile time.time() < end: pass"
        subprocess.run([sys.executable, "-c", burn], stdin=subprocess.DEVNULL, check=True)
    last = env.get("FAKE_CODEX_LAST", "fake last message")
    with open(opts["-o"][0], "w") as f:
        f.write(last)
    say("tokens used")
    say(f"{int(env.get('FAKE_CODEX_TOKENS', '1234')):,}")
    say(last)
    if env.get("FAKE_CODEX_TIMES"):
        with open(env["FAKE_CODEX_TIMES"], "a") as f:
            f.write(f"{began} {time.time()}\n")
    return int(env.get("FAKE_CODEX_EXIT", "0"))


if __name__ == "__main__":
    sys.exit(main())
