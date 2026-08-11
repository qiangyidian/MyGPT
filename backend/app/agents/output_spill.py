"""Token-budgeted output spill-to-disk (Codex pattern).

A hook/skill/tool that emits a large blob (e.g. a 50KB security scan) would blow
the context window. Codex spills it: if the output exceeds a per-handler token
budget, the full text is written to a temp file and replaced in-context with a
head/tail preview + a filesystem path to the full artifact. The model keeps a
pointer; the window keeps its budget.

Pure + testable (operates on strings + a writer callable); the temp-file writer
is injected.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable

from app.agents.context_compaction import estimate_tokens

_HEAD = 600
_TAIL = 600


@dataclass
class SpillResult:
    in_context: str   # what the model sees (preview, or the full text if not spilled)
    spilled: bool
    path: str | None  # filesystem path to the full artifact when spilled


@dataclass
class ArtifactHandle:
    """Opaque, authorized reference to a spilled artifact (Task 7 seam).

    Generalizes ``SpillResult.path`` into a handle the model can reference but
    cannot misuse as a raw filesystem path. ``id`` is what the model sees
    (e.g. ``artifact:<uuid>``); ``storage_key`` is whatever the injected writer
    returned — the backend resolves it to the real bytes via the artifact
    service.

    Opaqueness of ``storage_key`` is the PRODUCTION writer's job: a real
    artifact-service writer returns an opaque, authorized key (e.g.
    ``stored:<hash>``) that the backend resolves with auth/checksum/retention.
    The default writer (:func:`_default_writer`) is a storage-backed STUB that
    returns a temp-file PATH — sufficient to satisfy the seam in dev and tests,
    but it must be replaced by the real artifact-service writer (Task 10) before
    handles reach a production model. The seam's contract (handle.id is opaque;
    the model never sees a raw path as ``id``) holds either way.
    """

    id: str          # model-facing opaque id, e.g. "artifact:<uuid>"
    storage_key: str # writer-returned key (opaque in prod; temp path in the stub)

    def __str__(self) -> str:
        return self.id


def maybe_spill(
    text: str,
    *,
    budget_tokens: int,
    write_fn: Callable[[str, str], str] | None = None,
    key: str = "blob",
    spill_dir: str | os.PathLike[str] | None = None,
) -> SpillResult:
    """If ``text`` exceeds ``budget_tokens`` (0 = never spill), write the full text
    via ``write_fn(name, content) -> path`` and return a head/tail preview.

    Default ``write_fn`` writes to ``spill_dir`` (or a temp dir) and returns the path.
    """
    if budget_tokens <= 0 or not text:
        return SpillResult(in_context=text, spilled=False, path=None)
    if estimate_tokens(text) <= budget_tokens:
        return SpillResult(in_context=text, spilled=False, path=None)

    writer = write_fn or _default_writer
    name = f"{key}.txt"
    try:
        path = writer(name, text)
    except Exception:  # noqa: BLE001 — spill is best-effort; never block on it
        return SpillResult(in_context=text, spilled=False, path=None)

    preview = _preview(text)
    return SpillResult(
        in_context=f"{preview}\n\n[完整内容已溢出到磁盘，见：{path}]",
        spilled=True,
        path=path,
    )


def _preview(text: str) -> str:
    if len(text) <= _HEAD + _TAIL:
        return text
    return f"{text[:_HEAD]}\n…[已省略 {len(text) - _HEAD - _TAIL} 字符]…\n{text[-_TAIL:]}"


def _default_writer(name: str, content: str) -> str:
    import tempfile

    d = tempfile.mkdtemp(prefix="hook_spill_")
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)
    return p


def _run_async_sync(coro_factory):
    """Run an async coroutine factory to completion from sync code.

    Always runs in a fresh daemon thread with its own event loop, so it is safe
    to call from inside an already-running loop (e.g. an async chat turn that
    reaches the sync spill seam). The spill path is bounded I/O, so blocking the
    caller thread briefly is acceptable and never blocks the model stream.
    """
    import asyncio
    import threading

    box: dict = {}

    def _runner() -> None:
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            box["result"] = loop.run_until_complete(coro_factory())
        except BaseException as exc:  # noqa: BLE001 — propagate to caller
            box["error"] = exc
        finally:
            try:
                loop.close()
            except Exception:  # noqa: BLE001
                pass

    t = threading.Thread(target=_runner, name="artifact-spill", daemon=True)
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box.get("result")


def production_spill_writer(name: str, content: str) -> str:
    """Production ``write_fn`` for :func:`spill`/:func:`maybe_spill`.

    When an artifact auth context is bound for the current turn
    (see :mod:`app.artifacts.context`), persists the blob as a first-class,
    tenant-scoped :class:`Artifact` (source=``spill``) and returns its opaque
    ``storage_key``. The created artifact id is stashed on
    ``production_spill_writer.last_artifact_id`` so the caller can adopt it as
    the handle id (``artifact:<id>``) — this is what makes a spilled blob a
    downloadable row rather than a temp file.

    When no context is bound (no active turn — pure unit tests), falls through
    to :func:`_default_writer` so spill remains best-effort and never blocks.
    """
    import hashlib

    from app.artifacts.context import get_artifact_spill_context

    ctx = get_artifact_spill_context()
    production_spill_writer.last_artifact_id = None  # type: ignore[attr-defined]
    if ctx is None:
        return _default_writer(name, content)

    owner_id = ctx["owner_id"]
    run_id = ctx.get("run_id")
    db_factory = ctx["db_factory"]
    data = content.encode("utf-8")

    async def _create():
        async with db_factory() as db:
            from app.artifacts.service import ArtifactService

            svc = ArtifactService(db)
            gen = {
                "spill_key": name,
                "sha256": hashlib.sha256(data).hexdigest(),
                "source_writer": "production_spill_writer",
            }
            art = await svc.create_from_bytes(
                owner_id=owner_id,
                data=data,
                media_type="text/plain",
                filename=f"{name}.txt" if not name.endswith(".txt") else name,
                source="spill",
                run_id=run_id,
                generator=gen,
            )
            return art

    try:
        art = _run_async_sync(_create)
    except Exception:  # noqa: BLE001 — spill is best-effort; never block on it
        return _default_writer(name, content)

    production_spill_writer.last_artifact_id = str(art.id)  # type: ignore[attr-defined]
    return art.storage_key


production_spill_writer.last_artifact_id = None  # type: ignore[attr-defined]


def spill(
    text: str,
    *,
    budget_tokens: int,
    write_fn: Callable[[str, str], str] | None = None,
    key: str = "blob",
) -> tuple[str, ArtifactHandle | None]:
    """Spill ``text`` to an opaque artifact handle when it exceeds the budget.

    Returns ``(in_context_preview, handle)``. When the text fits the budget
    (or budget is 0 / writer fails — best-effort), ``handle is None`` and the
    original text is returned unchanged. When it spills, the model sees a
    head/tail preview + an opaque ``artifact:<id>`` handle; the writer returns
    an opaque storage key (NOT a raw path) that the Task-10 artifact service
    will resolve with auth/checksum/retention.

    The default writer is a storage-backed stub (a temp file keyed by ``key``)
    sufficient to satisfy the seam today; it is replaced by the full artifact
    service in Task 10.
    """
    if budget_tokens <= 0 or not text:
        return text, None
    if estimate_tokens(text) <= budget_tokens:
        return text, None

    writer = write_fn or _default_writer
    import uuid as _uuid

    artifact_id = f"artifact:{_uuid.uuid4().hex}"
    try:
        storage_key = writer(key, text)
    except Exception:  # noqa: BLE001 — spill is best-effort; never block on it
        return text, None

    preview = _preview(text)
    handle = ArtifactHandle(id=artifact_id, storage_key=storage_key)
    in_context = f"{preview}\n\n[完整内容已溢出，句柄：{handle.id}]"
    return in_context, handle


def build_artifact_writer(
    db_factory,
    *,
    owner_id,
    media_type: str = "text/plain",
    filename: str = "spill.txt",
    source: str = "spill",
    retention_policy: str | None = None,
    expires_at=None,
    run_id=None,
    step_id=None,
    generator: dict | None = None,
) -> Callable[[str, str], str]:
    """Build a sync ``write_fn(name, content) -> storage_key`` backed by the
    real :class:`ArtifactService`.

    This is the production writer for :func:`spill` / :func:`maybe_spill`: it
    persists the blob as a first-class Artifact (source=``spill``) and returns
    the opaque ``storage_key``. The model only ever sees ``artifact:<id>``
    (assembled by the caller from the returned Artifact); the storage key never
    reaches the model or client.

    ``db_factory`` is a zero-arg callable returning an AsyncSession context
    manager (e.g. ``AsyncSessionLocal``). The writer runs the async create in a
    dedicated event loop so it works from the sync spill seam; callers that are
    already inside an event loop should prefer :func:`spill_to_artifact`.

    The created artifact id is cached on the returned closure under
    ``writer.last_artifact_id`` so the caller can publish the opaque handle.
    """
    import asyncio
    import hashlib

    state = {"artifact_id": None}

    def _writer(name: str, content: str) -> str:
        data = content.encode("utf-8")
        # Run the async create outside any existing loop.
        async def _create():
            async with db_factory() as db:
                from app.artifacts.service import ArtifactService

                svc = ArtifactService(db)
                gen = dict(generator or {})
                # Record the logical spill key + content hash for audit.
                gen.setdefault("spill_key", name)
                gen.setdefault("sha256", hashlib.sha256(data).hexdigest())
                art = await svc.create_from_bytes(
                    owner_id=owner_id,
                    data=data,
                    media_type=media_type,
                    filename=filename or name,
                    source=source,
                    retention_policy=retention_policy,
                    expires_at=expires_at,
                    run_id=run_id,
                    step_id=step_id,
                    generator=gen,
                )
                return art

        try:
            loop = asyncio.new_event_loop()
            try:
                art = loop.run_until_complete(_create())
            finally:
                loop.close()
        except Exception:  # noqa: BLE001 — spill is best-effort
            raise
        state["artifact_id"] = str(art.id)
        return art.storage_key

    _writer.last_artifact_id = lambda: state["artifact_id"]  # type: ignore[attr-defined]
    return _writer


async def spill_to_artifact(
    db,
    *,
    owner_id,
    text: str,
    budget_tokens: int,
    media_type: str = "text/plain",
    filename: str = "spill.txt",
    key: str = "blob",
    source: str = "spill",
    retention_policy: str | None = None,
    expires_at=None,
    run_id=None,
    step_id=None,
    generator: dict | None = None,
) -> tuple[str, "ArtifactHandle | None"]:
    """Async spill that persists the blob via :class:`ArtifactService`.

    The Task-10 backing for the Task-7 ``ArtifactHandle`` seam: when ``text``
    exceeds ``budget_tokens``, the full content is stored as a first-class
    Artifact (source=``spill``) and the model sees only the head/tail preview +
    an opaque ``artifact:<id>`` handle. Returns ``(in_context_preview, handle)``;
    ``handle is None`` when the text fit the budget (no spill).
    """
    if budget_tokens <= 0 or not text:
        return text, None
    if estimate_tokens(text) <= budget_tokens:
        return text, None

    import hashlib

    from app.artifacts.service import ArtifactService

    data = text.encode("utf-8")
    gen = dict(generator or {})
    gen.setdefault("spill_key", f"{key}.txt")
    gen.setdefault("sha256", hashlib.sha256(data).hexdigest())
    svc = ArtifactService(db)
    try:
        art = await svc.create_from_bytes(
            owner_id=owner_id,
            data=data,
            media_type=media_type,
            filename=filename,
            source=source,
            retention_policy=retention_policy,
            expires_at=expires_at,
            run_id=run_id,
            step_id=step_id,
            generator=gen,
        )
    except Exception:  # noqa: BLE001 — spill is best-effort; never block on it
        return text, None

    preview = _preview(text)
    handle = ArtifactHandle(id=f"artifact:{art.id}", storage_key=art.storage_key)
    in_context = f"{preview}\n\n[完整内容已溢出，句柄：{handle.id}]"
    return in_context, handle
