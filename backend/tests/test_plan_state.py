"""PlanState: live plan status discipline (update_plan tool)."""
from __future__ import annotations

import pytest

from app.agents.plan_state import PlanError, PlanState, PlanStep


def _steps(*pairs):
    return [PlanStep(id=i, title=t, status=s) for i, t, s in pairs]


def test_replace_allows_single_in_progress():
    p = PlanState()
    p.replace(_steps(("1", "a", "pending"), ("2", "b", "in_progress")))
    assert p.has_in_progress() is True


def test_replace_rejects_two_in_progress():
    p = PlanState()
    with pytest.raises(PlanError):
        p.replace(_steps(("1", "a", "in_progress"), ("2", "b", "in_progress")))


def test_pending_to_completed_forbidden():
    p = PlanState()
    p.replace(_steps(("1", "a", "pending")))
    with pytest.raises(PlanError):
        p.transition("1", "completed")


def test_two_in_progress_at_once_forbidden():
    p = PlanState()
    p.replace(_steps(("1", "a", "in_progress"), ("2", "b", "pending")))
    with pytest.raises(PlanError):
        p.transition("2", "in_progress")


def test_normal_progression_pending_inprogress_completed():
    p = PlanState()
    p.replace(_steps(("1", "a", "pending"), ("2", "b", "pending")))
    p.transition("1", "in_progress")
    p.transition("1", "completed")
    p.transition("2", "in_progress")  # previous completed, so this is fine
    p.transition("2", "completed")
    assert p.all_completed() is True


def test_unknown_step_raises():
    p = PlanState()
    p.replace(_steps(("1", "a", "pending")))
    with pytest.raises(PlanError):
        p.transition("nope", "in_progress")


def test_to_public_shape():
    p = PlanState()
    p.replace(_steps(("1", "a", "pending")))
    assert p.to_public() == [{"id": "1", "title": "a", "status": "pending"}]
