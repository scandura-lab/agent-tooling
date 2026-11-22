#!/usr/bin/env python3
"""Put a stamped copy of agent-tooling modules into a project, and check such a copy.

What it does: `into DEST MODULE [MODULE ...]` writes each named module (for example `codex_run`,
the file codex_run.py) into the folder DEST under one stamp line naming this repository's commit
and the sha256 of the copied text, plus a stamped copy of `tools/copy_check.py`, which lets the
project's own test detect an edit with no clone of this repository; `check DEST MODULE ...`
reports, for each copy, whether it is current, behind the source, or edited in place.
Where it fits: the one door between this repository and a project's copy, as agent-guidance's
`tools/stamp.py` is for the rule pages. It reads this repository's git history and writes only
DEST. Called by an architect from a clone of this repository.

A copy is: the project's own header lines (kept as they are), then the stamp line (a comment),
a blank line, and the source file byte for byte. A stamped copy still runs: `PYTHONPATH=DEST
python3 -m codex_run ...`.

Exit status: 0 current; 1 a refusal or a copy edited in place or missing; 2 a copy behind the source.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from copy_check import body_digest, split_copy, stamp_line

MODULES = {"codex_run": "codex_run.py"}
#: Copied with every module, so a project can check its copies without this repository.
CHECKER = "tools/copy_check.py"
REPO = Path(__file__).resolve().parent.parent


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(REPO), *args], check=True, capture_output=True, text=True).stdout


def sources(modules: list[str]) -> list[str]:
    unknown = [m for m in modules if m not in MODULES]
    if unknown or not modules:
        raise SystemExit(f"REFUSED: unknown or no module(s) {unknown}; known: {sorted(MODULES)}")
    return [MODULES[m] for m in modules] + [CHECKER]


def last_commit(path: str) -> str:
    sha = git("log", "-1", "--format=%H", "HEAD", "--", path).strip()
    if not sha:
        raise SystemExit(f"REFUSED: {path} has no commit in this repository")
    return sha


def into(dest: Path, modules: list[str]) -> int:
    paths = sources(modules)
    if git("status", "--porcelain", "--", *paths).strip():
        print(f"REFUSED: {paths} have uncommitted changes; commit them first, so the stamp names the text", file=sys.stderr)
        return 1
    if not dest.is_dir():
        print(f"REFUSED: {dest} is not a folder", file=sys.stderr)
        return 1
    for path in paths:
        sha = last_commit(path)
        date = git("log", "-1", "--format=%cs", sha).strip()
        body = git("show", f"{sha}:{path}")
        target = dest / Path(path).name
        header: list[str] = []
        if target.exists():
            header, stamp, _ = split_copy(target.read_text(encoding="utf-8"))
            if stamp is None:
                print(f"REFUSED: {target} exists without a stamp line; move it aside first", file=sys.stderr)
                return 1
        target.write_text("\n".join([*header, stamp_line(path, sha, date, body, comment=True), "", body]), encoding="utf-8")
        print(f"STAMPED {target} at {sha[:12]}")
    return 0


def check(dest: Path, modules: list[str]) -> int:
    worst = 0
    for path in sources(modules):
        target = dest / Path(path).name
        if not target.exists():
            print(f"MISSING {target}")
            worst = 1
            continue
        _, stamp, body = split_copy(target.read_text(encoding="utf-8"))
        if stamp is None or stamp["page"] != path:
            print(f"NO STAMP {target}: not a copy this tool made")
            worst = 1
            continue
        if body_digest(body) != stamp["digest"]:
            print(f"EDITED IN PLACE {target}: the text no longer matches the stamp's sha256; propose the change here instead")
            worst = 1
            continue
        latest = last_commit(path)
        if not latest.startswith(stamp["sha"]):
            print(f"BEHIND {target}: stamped {stamp['sha']}, source {latest[:12]}; run: python3 tools/stamp.py into {dest} {' '.join(modules)}")
            worst = worst or 2
            continue
        print(f"CURRENT {target} at {stamp['sha']}")
    return worst


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="verb", required=True)
    for verb in ("into", "check"):
        cmd = sub.add_parser(verb)
        cmd.add_argument("dest", type=Path, help="the project folder that holds the copies")
        cmd.add_argument("modules", nargs="+", metavar="MODULE", help=f"one or more of {sorted(MODULES)}")
    args = parser.parse_args()
    return into(args.dest, args.modules) if args.verb == "into" else check(args.dest, args.modules)


if __name__ == "__main__":
    sys.exit(main())
