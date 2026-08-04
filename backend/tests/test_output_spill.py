"""output_spill: token-budgeted spill-to-disk with head/tail preview."""
from app.agents.output_spill import maybe_spill


def _big():
    return "line\n" * 4000  # well over a small token budget


def test_no_spill_when_under_budget():
    r = maybe_spill("short text", budget_tokens=1000)
    assert r.spilled is False
    assert r.in_context == "short text"
    assert r.path is None


def test_no_spill_when_budget_zero():
    r = maybe_spill(_big(), budget_tokens=0)
    assert r.spilled is False


def test_spill_writes_and_returns_preview():
    written = {}

    def writer(name, content):
        written["name"] = name
        written["content"] = content
        return "/tmp/blob.txt"

    text = _big()
    r = maybe_spill(text, budget_tokens=100, write_fn=writer, key="scan")
    assert r.spilled is True
    assert r.path == "/tmp/blob.txt"
    assert "完整内容已溢出" in r.in_context
    assert "/tmp/blob.txt" in r.in_context
    # The preview is much smaller than the original.
    assert len(r.in_context) < len(text)
    # Full content was written.
    assert written["content"] == text
    assert written["name"] == "scan.txt"


def test_spill_writer_failure_returns_original():
    def boom(name, content):
        raise OSError("disk full")

    r = maybe_spill(_big(), budget_tokens=100, write_fn=boom)
    assert r.spilled is False  # best-effort: never block
    assert r.in_context  # original retained
