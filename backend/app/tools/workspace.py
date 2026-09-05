"""Workspace-confined tools (Task 8).

Every tool in this module operates strictly inside an assigned workspace root.
The security core is :func:`resolve_under_root`: it resolves a user-supplied
path (following symlinks, normalising ``..``) and requires the result to remain
UNDER the resolved workspace root. It rejects:

  * ``..`` traversal out of the root (``../secret``);
  * absolute paths that resolve outside the root (``/etc/passwd``);
  * symlinks whose target escapes the root (``resolve()`` follows the link, the
    target then fails the confinement check).

The same helper is applied to EVERY path the tools touch (read/list/search/
write/apply-patch/git). Writes are atomic (temp file in the same dir +
``os.replace``); a failed write or a failed patch leaves the original file
byte-identical (no partial writes).

Reads/list/search are low-risk; writes/patch/shell/git-mutations require the
``:workspace-write`` permission profile (see
:mod:`app.agents.permission_profiles` and the registration helper in
:mod:`app.tools.registry_init`).
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

from app.agents.apply_patch import PatchError, apply_ops, parse_patch
from app.agents.sandbox.base import Runner, RunResult
from app.agents.sandbox.local import LocalRunner
from app.tools.base import BaseTool, ToolError, ToolParameter

# Cap on search matches so a huge workspace can't flood the context.
_SEARCH_MAX_MATCHES = 200
# Max bytes of a single file the read tool will load (bound memory / context).
_READ_MAX_BYTES = 256 * 1024


# --------------------------------------------------------------------------- #
# Path confinement — the security core
# --------------------------------------------------------------------------- #
def resolve_under_root(path: str | Path, root: Path) -> Path:
    """Resolve ``path`` and require it to remain under ``root``.

    Symlinks are followed (``Path.resolve()``), so a link pointing outside the
    workspace is caught by the post-resolution confinement check. Raises
    :class:`ToolError` on any escape; returns the resolved absolute Path on
    success.
    """
    root_resolved = root.resolve()
    p = Path(path)
    candidate = p if p.is_absolute() else root_resolved / p
    # strict=False: resolve existing components (incl. symlinks) and lexically
    # normalise the rest, so paths to not-yet-existing files still work.
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        raise ToolError(
            f"path escapes workspace root: {path!r} -> {resolved} "
            f"is not under {root_resolved}"
        )
    return resolved


def _atomic_write_bytes(target: Path, data: bytes) -> None:
    """Write ``data`` to ``target`` atomically (temp + ``os.replace``).

    The temp file is created in the SAME directory as ``target`` (so the rename
    is atomic on both POSIX and Windows). On any error the temp file is removed
    and the original is left byte-identical.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=".ws-write-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #
class WorkspaceReadTool(BaseTool):
    """Read a UTF-8 text file from the workspace (low-risk read)."""

    name = "workspace_read"
    description = "Read a file from the workspace. The path must stay inside the workspace root."
    category = "workspace"
    parameters = [
        ToolParameter(name="path", type="string", description="Workspace-relative path."),
    ]

    def __init__(self, workspace_root: Path) -> None:
        self._root = Path(workspace_root)

    async def run(self, **kwargs: Any) -> dict[str, Any]:
        path = kwargs.get("path")
        if not path or not isinstance(path, str):
            raise ToolError("'path' is required and must be a string")
        resolved = resolve_under_root(path, self._root)
        if not resolved.exists() or not resolved.is_file():
            raise ToolError(f"file not found: {path}")
        raw = resolved.read_bytes()[:_READ_MAX_BYTES]
        text = raw.decode("utf-8", errors="replace")
        return {"path": str(resolved), "content": text, "bytes": len(raw)}


class WorkspaceListTool(BaseTool):
    """List entries under a workspace directory (low-risk read)."""

    name = "workspace_list"
    description = "List files/subdirectories under a workspace path."
    category = "workspace"
    parameters = [
        ToolParameter(name="path", type="string", description="Workspace-relative path.", default="."),
    ]

    def __init__(self, workspace_root: Path) -> None:
        self._root = Path(workspace_root)

    async def run(self, **kwargs: Any) -> dict[str, Any]:
        rel = kwargs.get("path", ".")
        if not isinstance(rel, str):
            raise ToolError("'path' must be a string")
        resolved = resolve_under_root(rel, self._root)
        if not resolved.exists():
            raise ToolError(f"path not found: {rel}")
        if not resolved.is_dir():
            raise ToolError(f"not a directory: {rel}")
        entries = []
        for child in sorted(resolved.iterdir(), key=lambda p: p.name):
            try:
                st = child.stat()
            except OSError:
                continue
            entries.append(
                {
                    "name": child.name,
                    "type": "dir" if child.is_dir() else "file",
                    "size": st.st_size,
                }
            )
        return {"path": str(resolved), "entries": entries}


class WorkspaceSearchTool(BaseTool):
    """Grep-ish text search across the workspace (low-risk read)."""

    name = "workspace_search"
    description = "Search for a pattern (substring or regex) across workspace text files."
    category = "workspace"
    parameters = [
        ToolParameter(name="pattern", type="string", description="Text or regex pattern."),
        ToolParameter(
            name="path",
            type="string",
            description="Workspace-relative directory to search.",
            default=".",
        ),
        ToolParameter(
            name="regex",
            type="boolean",
            description="Treat pattern as a regex (default substring).",
            required=False,
            default=False,
        ),
    ]

    def __init__(self, workspace_root: Path) -> None:
        self._root = Path(workspace_root)

    async def run(self, **kwargs: Any) -> dict[str, Any]:
        pattern = kwargs.get("pattern")
        if not pattern or not isinstance(pattern, str):
            raise ToolError("'pattern' is required and must be a string")
        rel = kwargs.get("path", ".")
        if not isinstance(rel, str):
            raise ToolError("'path' must be a string")
        use_regex = bool(kwargs.get("regex", False))
        resolved = resolve_under_root(rel, self._root)
        if not resolved.exists():
            raise ToolError(f"path not found: {rel}")

        matcher = re.compile(pattern) if use_regex else None
        matches: list[dict[str, Any]] = []
        # Walk under the resolved root only; skip hidden dirs + .git.
        for current, dirs, files in os.walk(resolved):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != ".git"]
            for fname in files:
                fpath = Path(current) / fname
                # Resolve each walked file and re-check confinement so a symlinked
                # dir can't sneak a file in from outside the workspace.
                try:
                    safe = resolve_under_root(fpath, self._root)
                except ToolError:
                    continue
                if not safe.is_file():
                    continue
                try:
                    raw = safe.read_bytes()[:_READ_MAX_BYTES]
                    text = raw.decode("utf-8", errors="replace")
                except OSError:
                    continue
                for lineno, line in enumerate(text.splitlines(), start=1):
                    hit = bool(matcher.search(line)) if matcher else (pattern in line)
                    if hit:
                        matches.append(
                            {"path": str(safe), "line": lineno, "text": line[:500]}
                        )
                        if len(matches) >= _SEARCH_MAX_MATCHES:
                            return {
                                "path": str(resolved),
                                "matches": matches,
                                "truncated": True,
                            }
        return {"path": str(resolved), "matches": matches, "truncated": False}


class WorkspaceWriteTool(BaseTool):
    """Atomically write/overwrite a workspace file (requires workspace-write)."""

    name = "workspace_write"
    description = "Create or overwrite a workspace file atomically. Path must stay in the workspace."
    category = "workspace"
    dangerous = True  # mutates the filesystem -> gated behind approval
    parameters = [
        ToolParameter(name="path", type="string", description="Workspace-relative path."),
        ToolParameter(name="content", type="string", description="File contents."),
    ]

    def __init__(self, workspace_root: Path) -> None:
        self._root = Path(workspace_root)

    async def run(self, **kwargs: Any) -> dict[str, Any]:
        path = kwargs.get("path")
        content = kwargs.get("content", "")
        if not path or not isinstance(path, str):
            raise ToolError("'path' is required and must be a string")
        if not isinstance(content, str):
            raise ToolError("'content' must be a string")
        resolved = resolve_under_root(path, self._root)
        _atomic_write_bytes(resolved, content.encode("utf-8"))
        return {"path": str(resolved), "bytes": len(content.encode("utf-8"))}


class WorkspaceApplyPatchTool(BaseTool):
    """Apply a Codex ``*** Begin Patch`` block atomically (requires workspace-write).

    The whole patch is applied in-memory first; ONLY if every op succeeds are the
    resulting files written (each atomically). A hunk that won't match raises
    :class:`ToolError` and leaves ALL touched files byte-identical. Records the
    added-line fingerprint count (audit/quality signal).
    """

    name = "workspace_apply_patch"
    description = "Apply a structured patch (Add/Update/Delete File) atomically within the workspace."
    category = "workspace"
    dangerous = True
    parameters = [
        ToolParameter(
            name="patch",
            type="string",
            description="A *** Begin Patch ... *** End Patch block.",
        ),
    ]

    def __init__(self, workspace_root: Path) -> None:
        self._root = Path(workspace_root)

    async def run(self, **kwargs: Any) -> dict[str, Any]:
        patch = kwargs.get("patch")
        if not patch or not isinstance(patch, str):
            raise ToolError("'patch' is required and must be a string")

        try:
            ops = parse_patch(patch)
        except Exception as exc:  # parser is lenient but be defensive
            raise ToolError(f"failed to parse patch: {exc}")

        if not ops:
            raise ToolError("patch contained no operations")

        # 1) Resolve EVERY referenced path first; reject escapes before any I/O.
        for op in ops:
            resolve_under_root(op.path, self._root)
            if getattr(op, "move_to", None):
                resolve_under_root(op.move_to, self._root)

        # 2) Load the files the patch touches into an in-memory map; run the
        #    applier (raises PatchError on a mismatching hunk). Disk is untouched
        #    until this fully succeeds -> whole-patch atomicity.
        snapshot: dict[str, list[str]] = {}
        for op in ops:
            target = op.move_to if getattr(op, "move_to", None) else op.path
            for p in (op.path, target):
                if p is None:
                    continue
                if p in snapshot:
                    continue
                resolved = resolve_under_root(p, self._root)
                if resolved.exists() and resolved.is_file():
                    snapshot[p] = resolved.read_text(encoding="utf-8").splitlines()

        try:
            deltas = apply_ops(ops, snapshot)
        except PatchError as exc:
            raise ToolError(f"patch did not apply cleanly: {exc}")

        # 3) Persist each resulting file atomically.
        for op in ops:
            target = op.move_to if getattr(op, "move_to", None) else op.path
            if op.action == "delete":
                resolved = resolve_under_root(op.path, self._root)
                if resolved.exists():
                    resolved.unlink()
                continue
            lines = snapshot.get(target)
            if lines is None:
                # move_to of an update whose new name wasn't snapshotted: write
                # the moved content if present, else skip.
                continue
            content = "\n".join(lines)
            if content and not content.endswith("\n"):
                content += "\n"
            resolved = resolve_under_root(target, self._root)
            _atomic_write_bytes(resolved, content.encode("utf-8"))

        return {
            "applied": True,
            "files_changed": len(deltas),
            "added_line_fingerprints": _count_added_fingerprints(patch),
        }


def _count_added_fingerprints(patch_text: str) -> int:
    """Distinct fingerprints of added (``+``) lines — a stable acceptance signal."""
    from app.agents.accepted_lines import accepted_line_fingerprints_from_unified_diff

    return len(accepted_line_fingerprints_from_unified_diff(patch_text))


class WorkspaceShellTool(BaseTool):
    """Run a NON-INTERACTIVE command (argv list) in the workspace (requires workspace-write).

    Takes an argv LIST only — never a shell string — so ``sh -c`` injection is
    impossible. Honours the runner's timeout + output cap.
    """

    name = "workspace_shell"
    description = "Run a non-interactive command (argv list) in the workspace via the sandbox runner."
    category = "workspace"
    dangerous = True
    parameters = [
        ToolParameter(
            name="command",
            type="array",
            description="Argv list, e.g. [\"python\", \"-c\", \"print(1)\"].",
        ),
        ToolParameter(
            name="timeout",
            type="integer",
            description="Timeout in seconds.",
            required=False,
            default=30,
        ),
    ]

    def __init__(
        self,
        workspace_root: Path,
        *,
        runner: Runner | None = None,
        output_limit: int = 8192,
    ) -> None:
        self._root = Path(workspace_root)
        self._runner = runner or LocalRunner()
        self._output_limit = output_limit

    async def run(self, **kwargs: Any) -> dict[str, Any]:
        command = kwargs.get("command")
        if not isinstance(command, list) or not command:
            raise ToolError("'command' must be a non-empty argv list (no shell strings)")
        try:
            timeout = int(kwargs.get("timeout", 30))
        except (TypeError, ValueError):
            timeout = 30
        result: RunResult = await self._runner.run(
            command,
            cwd=str(self._root.resolve()),
            timeout=float(timeout),
            output_limit=self._output_limit,
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
        }


class WorkspaceGitStatusTool(BaseTool):
    """``git status`` in the workspace (low-risk read)."""

    name = "workspace_git_status"
    description = "Run `git status` in the workspace (read-only)."
    category = "workspace"
    parameters = [
        ToolParameter(
            name="timeout", type="integer", description="Timeout in seconds.", required=False, default=15
        ),
    ]

    def __init__(
        self, workspace_root: Path, *, runner: Runner | None = None, output_limit: int = 8192
    ) -> None:
        self._root = Path(workspace_root)
        self._runner = runner or LocalRunner()
        self._output_limit = output_limit

    async def run(self, **kwargs: Any) -> dict[str, Any]:
        try:
            timeout = int(kwargs.get("timeout", 15))
        except (TypeError, ValueError):
            timeout = 15
        res = await self._runner.run(
            ["git", "status", "--short"],
            cwd=str(self._root.resolve()),
            timeout=float(timeout),
            output_limit=self._output_limit,
        )
        return _runner_result_dict(res)


class WorkspaceGitDiffTool(BaseTool):
    """``git diff`` in the workspace (low-risk read)."""

    name = "workspace_git_diff"
    description = "Run `git diff` in the workspace (read-only)."
    category = "workspace"
    parameters = [
        ToolParameter(
            name="timeout", type="integer", description="Timeout in seconds.", required=False, default=15
        ),
    ]

    def __init__(
        self, workspace_root: Path, *, runner: Runner | None = None, output_limit: int = 8192
    ) -> None:
        self._root = Path(workspace_root)
        self._runner = runner or LocalRunner()
        self._output_limit = output_limit

    async def run(self, **kwargs: Any) -> dict[str, Any]:
        try:
            timeout = int(kwargs.get("timeout", 15))
        except (TypeError, ValueError):
            timeout = 15
        res = await self._runner.run(
            ["git", "diff"],
            cwd=str(self._root.resolve()),
            timeout=float(timeout),
            output_limit=self._output_limit,
        )
        return _runner_result_dict(res)


def _runner_result_dict(res: RunResult) -> dict[str, Any]:
    return {
        "stdout": res.stdout,
        "stderr": res.stderr,
        "exit_code": res.exit_code,
        "timed_out": res.timed_out,
    }


# --------------------------------------------------------------------------- #
# Registry factory (see app.tools.registry_init.get_workspace_registry)
# --------------------------------------------------------------------------- #
WORKSPACE_TOOL_CLASSES: tuple[type[BaseTool], ...] = (
    WorkspaceReadTool,
    WorkspaceListTool,
    WorkspaceSearchTool,
    WorkspaceWriteTool,
    WorkspaceApplyPatchTool,
    WorkspaceShellTool,
    WorkspaceGitStatusTool,
    WorkspaceGitDiffTool,
)


__all__ = [
    "WORKSPACE_TOOL_CLASSES",
    "WorkspaceApplyPatchTool",
    "WorkspaceGitDiffTool",
    "WorkspaceGitStatusTool",
    "WorkspaceListTool",
    "WorkspaceReadTool",
    "WorkspaceSearchTool",
    "WorkspaceShellTool",
    "WorkspaceWriteTool",
    "resolve_under_root",
]
