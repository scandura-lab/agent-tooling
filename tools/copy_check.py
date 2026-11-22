"""Check stamped copies of agent-tooling modules, using only the copies themselves.

What it does: for each file in a folder, reads its stamp line, hashes the text below the stamp,
and reports INTACT when that hash equals the sha256 the stamp carries, EDITED IN PLACE when it
does not, and NO STAMP for a file without a stamp line.
Where it fits: `tools/stamp.py` writes this file, stamped, into every project folder it stamps,
so a project's own test can run it with no clone of agent-tooling and no network:
`python3 <folder>/copy_check.py <folder>` exits 0 only when every file in the folder is INTACT.
The folder holds the copies and nothing else (Python's __pycache__ folder aside). This module
reads; it never writes. `tools/stamp.py` imports its stamp parser, so the stamp form has one owner.
The stamp form is agent-guidance's, with the source repository named in it.
"""
from __future__ import annotations

import hashlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_NAME = "agent-tooling"
STAMP = re.compile(
    r"^(?:# )?Copied from (?P<repo>[A-Za-z0-9._-]+) (?P<page>\S+) at (?P<sha>[0-9a-f]{12}) "
    r"\((?P<date>\d{4}-\d{2}-\d{2}); sha256 (?P<digest>[0-9a-f]{64})\)\. "
)
STAMP_TAIL = "Do not edit here; propose changes to that repository."


def split_copy(text: str) -> tuple[list[str], re.Match[str] | None, str]:
    """(project header lines, stamp match, body below the stamp and its blank line)."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        m = STAMP.match(line)
        if m:
            rest = lines[i + 1 :]
            if rest and rest[0] == "":
                rest = rest[1:]
            return lines[:i], m, "\n".join(rest)
    return [], None, text


def body_digest(body: str) -> str:
    """The sha256 a stamp carries: of the copied text below the stamp line and its blank line."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def stamp_line(page: str, sha: str, date: str, body: str, *, comment: bool = False) -> str:
    """One stamp line for <body>, copied from <page> at commit <sha>; `comment` prefixes '# '."""
    line = f"Copied from {REPO_NAME} {page} at {sha[:12]} ({date}; sha256 {body_digest(body)}). {STAMP_TAIL}"
    return f"# {line}" if comment else line


@dataclass(frozen=True)
class CopyCheck:
    """One file's result: its path, its state (INTACT, EDITED IN PLACE, NO STAMP), its source."""

    path: Path
    state: str
    page: str | None


def check_copy(path: Path) -> CopyCheck:
    _, stamp, body = split_copy(path.read_text(encoding="utf-8"))
    if stamp is None:
        return CopyCheck(path, "NO STAMP", None)
    state = "INTACT" if body_digest(body) == stamp["digest"] else "EDITED IN PLACE"
    return CopyCheck(path, state, f"{stamp['repo']} {stamp['page']}")


def check_folder(folder: Path) -> list[CopyCheck]:
    """One result per file in <folder>, hidden files and subfolders aside; refuses an empty folder."""
    files = sorted(p for p in Path(folder).iterdir() if p.is_file() and not p.name.startswith("."))
    if not files:
        raise ValueError(f"{folder} holds no files to check")
    return [check_copy(p) for p in files]


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python3 copy_check.py FOLDER", file=sys.stderr)
        return 1
    results = check_folder(Path(argv[0]))
    for r in results:
        print(f"{r.state} {r.path}" + (f" (from {r.page})" if r.page else ""))
    return 0 if all(r.state == "INTACT" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
