"""apply-patch: structured file-edit primitive (parser + in-memory applier)."""
from __future__ import annotations

import pytest

from app.agents.apply_patch import PatchError, apply_ops, parse_patch


def test_parse_add_delete_update_ops():
    patch = """\
*** Begin Patch
*** Add File: hello.txt
+Hello world
+line two
*** Update File: src/app.py
@@ def greet():
-print("Hi")
+print("Hello, world!")
*** Delete File: obsolete.txt
*** End Patch"""
    ops = parse_patch(patch)
    assert [o.action for o in ops] == ["add", "update", "delete"]
    assert ops[0].content == ["Hello world", "line two"]
    assert ops[1].path == "src/app.py"
    assert [k for k, _ in ops[1].hunks[0].lines] == ["ctx", "-", "+"]
    assert ops[2].path == "obsolete.txt"


def test_apply_add_then_update_then_delete():
    files: dict[str, list[str]] = {}
    add_patch = """\
*** Begin Patch
*** Add File: a.py
+def f():
+    return 1
*** End Patch"""
    apply_ops(parse_patch(add_patch), files)
    assert files["a.py"] == ["def f():", "    return 1"]

    upd = """\
*** Begin Patch
*** Update File: a.py
@@ def f():
-    return 1
+    return 2
*** End Patch"""
    apply_ops(parse_patch(upd), files)
    assert files["a.py"] == ["def f():", "    return 2"]

    dele = """\
*** Begin Patch
*** Delete File: a.py
*** End Patch"""
    apply_ops(parse_patch(dele), files)
    assert "a.py" not in files


def test_update_missing_file_raises():
    with pytest.raises(PatchError):
        apply_ops(parse_patch("*** Begin Patch\n*** Update File: nope.py\n-x\n+y\n*** End Patch"), {})


def test_update_hunk_not_found_raises():
    files = {"a.py": ["one", "two"]}
    bad = """\
*** Begin Patch
*** Update File: a.py
@@ not present
-x
+y
*** End Patch"""
    with pytest.raises(PatchError):
        apply_ops(parse_patch(bad), files)


def test_update_move_to_renames_file():
    files = {"old.py": ["x"]}
    mv = """\
*** Begin Patch
*** Update File: old.py
*** Move to: new.py
*** End Patch"""
    apply_ops(parse_patch(mv), files)
    assert "old.py" not in files
    assert files["new.py"] == ["x"]
