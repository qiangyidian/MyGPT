"""Tiny golden-snapshot test helper (Codex's insta pattern, dependency-free).

Usage:
    from tests._snapshot import assert_snapshot
    assert_snapshot("my_case", rendered_text)

First run writes the snapshot (``tests/snapshots/<name>.snap``); subsequent runs
assert equality. To update (after an intentional change), delete the file or set
``SNAP_UPDATE=1``. Mirrors insta's review-driven workflow so any prompt/context
assembly change is caught and must be acknowledged.
"""
from __future__ import annotations

import os
import pathlib

SNAP_DIR = pathlib.Path(__file__).parent / "snapshots"


def _normalize(value: str) -> str:
    # Strip trailing whitespace per line + ensure a single trailing newline.
    lines = [line.rstrip() for line in str(value).splitlines()]
    return "\n".join(lines).rstrip() + "\n"


def assert_snapshot(name: str, value: str) -> None:
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    path = SNAP_DIR / f"{name}.snap"
    norm = _normalize(value)
    if os.environ.get("SNAP_UPDATE"):
        path.write_text(norm, encoding="utf-8")
        return
    if not path.exists():
        path.write_text(norm, encoding="utf-8")
        return  # first-write: record, pass
    prev = path.read_text(encoding="utf-8")
    assert prev == norm, (
        f"snapshot mismatch: {name}\n"
        f"--- expected (saved at {path})\n+++ actual\n"
        f"To update: delete {path} or rerun with SNAP_UPDATE=1."
    )
