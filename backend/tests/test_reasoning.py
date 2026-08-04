"""Reasoning-effort resolution (three-layer + Custom escape hatch)."""
from app.agents.reasoning import (
    CANONICAL_EFFORTS,
    ModelReasoningCatalog,
    ReasoningPreset,
    parse_effort,
    resolve_effort,
)


def test_parse_canonical_and_custom():
    assert parse_effort("High") == "high"
    assert parse_effort("medium") == "medium"
    assert parse_effort(None) is None
    # Unknown value -> custom escape hatch (forward-compat).
    assert parse_effort("ultra-plus") == "custom:ultra-plus"


def test_resolve_user_override_supported():
    cat = ModelReasoningCatalog(
        default="medium",
        supported=(ReasoningPreset("low"), ReasoningPreset("medium"), ReasoningPreset("high")),
    )
    assert resolve_effort("high", None, cat) == "high"


def test_resolve_user_override_unsupported_falls_to_default():
    cat = ModelReasoningCatalog(
        default="medium",
        supported=(ReasoningPreset("low"), ReasoningPreset("medium")),
    )
    assert resolve_effort("high", None, cat) == "medium"  # high not supported -> default


def test_resolve_custom_override_always_allowed():
    cat = ModelReasoningCatalog(default="medium", supported=(ReasoningPreset("medium"),))
    # Custom efforts bypass the supported check (forward-compat with new server tiers).
    assert resolve_effort("brand-new-tier", None, cat) == "custom:brand-new-tier"


def test_resolve_no_override_uses_model_default():
    cat = ModelReasoningCatalog(default="high", supported=(ReasoningPreset("high"),))
    assert resolve_effort(None, None, cat) == "high"
