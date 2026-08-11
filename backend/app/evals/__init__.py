"""Deterministic offline evaluation suite (Task 11).

Contracts here assert non-negotiable properties of the runtime / gateway /
budget / observability layers WITHOUT any external credentials — they read the
already-instrumented seams. Live "golden" tasks (which require a real model
endpoint) are gated on an explicit env credential and skipped otherwise.
"""
from app.evals.runner import run_evals

__all__ = ["run_evals"]
