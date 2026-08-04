"""accepted_lines: fingerprints of AI-suggested added lines from a unified diff."""
from app.agents.accepted_lines import (
    accepted_line_fingerprints_from_unified_diff,
    fingerprint_hash,
)


def test_fingerprint_whitespace_normalized():
    assert fingerprint_hash("  print('hi')  ") == fingerprint_hash("print('hi')")


def test_fingerprints_from_diff_skips_headers_and_context():
    diff = """\
--- a/f.py
+++ b/f.py
@@ -1,2 +1,3 @@
 def f():
-    return 1
+    return 2
+    return 3
"""
    fps = accepted_line_fingerprints_from_unified_diff(diff)
    # Two added lines (return 2, return 3); headers/context/removals skipped.
    assert len(fps) == 2
    assert fingerprint_hash("return 2") in fps
    assert fingerprint_hash("return 3") in fps


def test_fingerprints_dedup_identical_lines():
    diff = "+x = 1\n+x = 1\n+x = 2\n"
    fps = accepted_line_fingerprints_from_unified_diff(diff)
    assert fps == [fingerprint_hash("x = 1"), fingerprint_hash("x = 2")]


def test_empty_or_whitespace_added_skipped():
    assert accepted_line_fingerprints_from_unified_diff("+\n+   \n+x") == [fingerprint_hash("x")]
    assert accepted_line_fingerprints_from_unified_diff("") == []
