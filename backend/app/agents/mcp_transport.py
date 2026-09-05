"""Real MCP JSON-RPC transports (Task 9).

Implements the MCP client protocol directly over JSON-RPC 2.0 (request/response
correlation by ``id``) so it is testable against local fake servers WITHOUT the
optional ``mcp`` SDK. Two transports are provided:

  * :class:`StdioTransport` — launches a subprocess and speaks JSON-RPC over
    its stdin/stdout (one JSON document per line), with a bounded stderr ring
    buffer.
  * :class:`HttpTransport` — Streamable HTTP: POSTs each JSON-RPC request body
    to the server URL over ``httpx`` and reads the JSON (or SSE) response.

:class:`McpSession` owns the lifecycle + JSON-RPC id correlation + the
``initialize`` protocol-version negotiation. It exposes ``list_tools`` /
``call_tool`` (with per-request timeout, cancellation, and reconnect) and a
graceful ``close``.

The transport layer is deliberately protocol-thin: ``send_request`` returns the
JSON-RPC ``result`` field of the response (whatever the server sent). Higher
layers (the gateway wrapper / catalog) interpret MCP ``CallToolResult`` shapes.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import httpx

if TYPE_CHECKING:  # avoid a runtime circular import with app.agents.mcp_client
    from app.agents.mcp_client import McpServerConfig

logger = logging.getLogger(__name__)

# The protocol version we advertise during initialize negotiation. Servers that
# speak a different version still respond; we record what they returned.
MCP_PROTOCOL_VERSION = "2024-11-05"
# Default per-request timeout (seconds). tools/call can be slow, so this is
# generous; callers override per-call.
DEFAULT_REQUEST_TIMEOUT = 30.0


class McpError(Exception):
    """Base class for transport-level MCP failures."""


class McpTimeoutError(McpError):
    """A JSON-RPC request did not complete within its timeout."""


class McpProtocolError(McpError):
    """The server returned a JSON-RPC error response or malformed frame."""


@dataclass
class McpToolDef:
    """A discovered tool (the transport-facing shape)."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Transport protocol
# --------------------------------------------------------------------------- #
@runtime_checkable
class McpTransport(Protocol):
    """The transport contract a :class:`McpSession` relies on."""

    @property
    def closed(self) -> bool:
        ...

    async def start(self) -> None: ...
    async def close(self) -> None: ...

    async def send_request(
        self, method: str, params: dict[str, Any] | None, *, req_id: int, timeout: float
    ) -> Any: ...

    async def send_notification(
        self, method: str, params: dict[str, Any] | None = None
    ) -> None: ...

    async def cancel(self, req_id: int, reason: str = "") -> None: ...


# --------------------------------------------------------------------------- #
# Stdio transport (subprocess over stdin/stdout)
# --------------------------------------------------------------------------- #
class StdioTransport:
    """JSON-RPC over a subprocess stdin/stdout (one document per line).

    A background reader task drains stdout line-by-line and resolves pending
    requests by id. Stderr is captured into a bounded ring buffer so a chatty
    server can't exhaust memory; ``stderr_tail`` exposes the most recent bytes.
    """

    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        *,
        stderr_limit: int = 16384,
    ) -> None:
        self._command = command
        self._args = list(args or [])
        self._env = env or None
        self._stderr_limit = int(stderr_limit)
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._stderr_buf: deque[str] = deque(maxlen=self._stderr_limit)
        self._closed = True

    # -- lifecycle -----------------------------------------------------------
    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def stderr_tail(self) -> str:
        """The most recent stderr output (bounded to ``stderr_limit`` chars)."""
        return "".join(self._stderr_buf)

    async def start(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            return  # already running
        self._proc = await asyncio.create_subprocess_exec(
            self._command,
            *self._args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env,
        )
        self._closed = False
        self._reader = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())

    async def close(self) -> None:
        """Terminate the subprocess and stop the reader tasks (idempotent)."""
        if self._closed:
            return
        self._closed = True
        # Fail any still-pending requests so their awaiters wake up promptly.
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(McpError("transport closed"))
        self._pending.clear()
        await self._terminate_proc()
        for task in (self._reader, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self._reader = self._stderr_task = None

    async def kill(self) -> None:
        """Force-kill the subprocess and tear the transport down.

        Distinct from :meth:`close` only in that it uses ``SIGKILL`` so a wedged
        server is removed immediately. After this the transport is ``closed``
        and a :meth:`~McpSession.reconnect` builds a fresh subprocess.
        """
        # Mark closed first so the reader/stderr loops treat EOF as expected.
        self._closed = True
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(McpError("subprocess killed"))
        self._pending.clear()
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.kill()
            except ProcessLookupError:
                pass
        # Let the process settle and cancel the drain tasks.
        if self._proc is not None:
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
        self._proc = None
        for task in (self._reader, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self._reader = self._stderr_task = None

    async def _terminate_proc(self) -> None:
        proc = self._proc
        if proc is None:
            return
        self._proc = None
        if proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                return
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()

    # -- IO ------------------------------------------------------------------
    async def _read_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break  # EOF — subprocess exited
                line_s = line.decode(errors="replace").strip()
                if not line_s:
                    continue
                try:
                    msg = json.loads(line_s)
                except json.JSONDecodeError:
                    logger.warning("mcp stdio: malformed stdout frame: %r", line_s[:200])
                    continue
                self._dispatch(msg)
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        finally:
            # Subprocess exited: fail any pending requests still waiting.
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(McpError("mcp server closed the connection"))
            self._pending.clear()

    async def _read_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                chunk = await proc.stderr.read(4096)
                if not chunk:
                    break
                text = chunk.decode(errors="replace")
                # Bounded ring buffer of individual chars (deque drops oldest).
                self._stderr_buf.extend(text)
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    def _dispatch(self, msg: dict[str, Any]) -> None:
        if not isinstance(msg, dict):
            return
        # Notifications from the server (no id) are not awaited; ignore them
        # for now (transport-thin).
        req_id = msg.get("id")
        if req_id is None:
            return
        fut = self._pending.pop(int(req_id), None)
        if fut is None or fut.done():
            return
        if "error" in msg:
            err = msg.get("error") or {}
            fut.set_exception(
                McpProtocolError(f"{err.get('code')}: {err.get('message')}")
            )
        else:
            fut.set_result(msg.get("result"))

    # -- protocol ------------------------------------------------------------
    async def send_request(
        self,
        method: str,
        params: dict[str, Any] | None,
        *,
        req_id: int,
        timeout: float,
    ) -> Any:
        if self._proc is None or self._proc.stdin is None:
            raise McpError("transport not started")
        frame = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            frame["params"] = params
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[req_id] = fut
        try:
            payload = (json.dumps(frame) + "\n").encode()
            self._proc.stdin.write(payload)
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._pending.pop(req_id, None)
            raise McpError(f"mcp server pipe closed: {exc}") from exc
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(req_id, None)
            raise McpTimeoutError(
                f"mcp request {method!r} (id={req_id}) timed out after {timeout}s"
            ) from exc
        finally:
            # If it already resolved, this is a no-op removal.
            self._pending.pop(req_id, None)

    async def send_notification(
        self, method: str, params: dict[str, Any] | None = None
    ) -> None:
        if self._proc is None or self._proc.stdin is None:
            return  # best-effort: nothing to send to
        frame: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            frame["params"] = params
        try:
            payload = (json.dumps(frame) + "\n").encode()
            self._proc.stdin.write(payload)
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass  # best-effort

    async def cancel(self, req_id: int, reason: str = "") -> None:
        """Tell the server the request is cancelled + resolve the awaiter."""
        # Resolve the local awaiter immediately so the caller doesn't block on
        # a server that may not honour the cancellation.
        fut = self._pending.pop(req_id, None)
        if fut is not None and not fut.done():
            fut.cancel()
        # Best-effort notify the server.
        await self.send_notification(
            "notifications/cancelled",
            {"requestId": req_id, "reason": reason or "cancelled by client"},
        )


# --------------------------------------------------------------------------- #
# Streamable HTTP transport
# --------------------------------------------------------------------------- #
class HttpTransport:
    """JSON-RPC over Streamable HTTP (POST per request, JSON or SSE response).

    A new client is created unless one is supplied (tests inject an
    ``httpx.AsyncClient`` wired to an ASGI fake via ``httpx.ASGITransport`` so no
    socket is opened).
    """

    def __init__(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
    ) -> None:
        self._url = url
        self._headers = {"Accept": "application/json, text/event-stream"}
        if headers:
            self._headers.update(headers)
        self._owns_client = client is None
        self._client = client
        self._timeout = float(timeout)
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def start(self) -> None:
        # Nothing to pre-open; the client is created lazily on first request.
        await self._ensure_client()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def send_request(
        self,
        method: str,
        params: dict[str, Any] | None,
        *,
        req_id: int,
        timeout: float,
    ) -> Any:
        if self._closed:
            raise McpError("transport closed")
        client = await self._ensure_client()
        frame: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            frame["params"] = params
        try:
            # Hard asyncio backstop around the POST: in-process transports
            # (e.g. httpx.ASGITransport in tests) don't always honour httpx's
            # own timeout, so enforce it ourselves to guarantee cancellation.
            response = await asyncio.wait_for(
                client.post(
                    self._url,
                    json=frame,
                    headers=self._headers,
                    timeout=timeout,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError as exc:
            raise McpTimeoutError(
                f"mcp http {method!r} (id={req_id}) timed out after {timeout}s"
            ) from exc
        except httpx.TimeoutException as exc:
            raise McpTimeoutError(
                f"mcp http {method!r} (id={req_id}) timed out after {timeout}s"
            ) from exc
        except httpx.HTTPError as exc:
            raise McpError(f"mcp http transport error: {exc}") from exc
        if response.status_code >= 400:
            raise McpProtocolError(f"mcp http {response.status_code}: {response.text[:200]}")
        return self._parse_response(response, req_id)

    def _parse_response(self, response: httpx.Response, req_id: int) -> Any:
        ctype = response.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            msg = self._parse_sse(response)
        else:
            try:
                msg = response.json()
            except Exception as exc:
                raise McpProtocolError(f"mcp http non-JSON response: {exc}") from exc
        if not isinstance(msg, dict):
            raise McpProtocolError("mcp http response is not a JSON object")
        if msg.get("id") != req_id and msg.get("id") is not None:
            # Some servers echo the id; others may omit it. Mismatch is an error.
            raise McpProtocolError(
                f"mcp http id mismatch: expected {req_id}, got {msg.get('id')}"
            )
        if "error" in msg:
            err = msg.get("error") or {}
            raise McpProtocolError(f"{err.get('code')}: {err.get('message')}")
        return msg.get("result")

    @staticmethod
    def _parse_sse(response: httpx.Response) -> dict[str, Any]:
        """Extract the first JSON-RPC ``data:`` frame from an SSE response."""
        for raw in response.text.splitlines():
            if raw.startswith("data:"):
                payload = raw[len("data:"):].strip()
                try:
                    parsed = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    return parsed
        raise McpProtocolError("mcp http SSE stream yielded no JSON-RPC frame")

    async def send_notification(
        self, method: str, params: dict[str, Any] | None = None
    ) -> None:
        if self._closed:
            return
        client = await self._ensure_client()
        frame: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            frame["params"] = params
        try:
            await client.post(self._url, json=frame, headers=self._headers)
        except httpx.HTTPError:
            pass  # best-effort

    async def cancel(self, req_id: int, reason: str = "") -> None:
        # HTTP has no in-process future to resolve; the awaiter is unblocked by
        # asyncio cancellation of the POST. Notify the server best-effort.
        await self.send_notification(
            "notifications/cancelled",
            {"requestId": req_id, "reason": reason or "cancelled by client"},
        )


# --------------------------------------------------------------------------- #
# Transport factory + session
# --------------------------------------------------------------------------- #
def build_transport(config: McpServerConfig) -> McpTransport:
    """Construct the right transport for a :class:`McpServerConfig`."""
    t = (getattr(config, "transport", "stdio") or "stdio").lower()
    if t == "stdio":
        return StdioTransport(
            config.command,
            list(getattr(config, "args", []) or []),
            dict(getattr(config, "env", {}) or {}),
        )
    if t in ("http", "sse"):
        # The ``command`` field carries the URL for HTTP transports.
        return HttpTransport(url=config.command)
    raise ValueError(f"unsupported mcp transport: {config.transport!r}")


class McpSession:
    """Owns JSON-RPC id correlation + the MCP ``initialize`` handshake.

    Construct with a :class:`McpServerConfig` (builds the transport) or a
    pre-built transport (for tests). A session is single-server.
    """

    def __init__(
        self,
        config: McpServerConfig | None = None,
        *,
        transport: McpTransport | None = None,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
    ) -> None:
        if transport is not None:
            self._transport: McpTransport = transport
        elif config is not None:
            self._transport = build_transport(config)
        else:
            raise ValueError("McpSession requires either a config or a transport")
        self._request_timeout = float(request_timeout)
        self._next_id = 0
        self._initialized = False
        self._server_info: dict[str, Any] = {}

    @property
    def transport(self) -> McpTransport:
        return self._transport

    @property
    def initialized(self) -> bool:
        return self._initialized

    @property
    def server_info(self) -> dict[str, Any]:
        return dict(self._server_info)

    def _allocate_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def start(self) -> None:
        await self._transport.start()

    async def initialize(self) -> dict[str, Any]:
        """Perform the MCP initialize handshake. Returns the server's reply."""
        await self._transport.start()
        req_id = self._allocate_id()
        result = await self._transport.send_request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "mygpt-mcp-client", "version": "1.0"},
            },
            req_id=req_id,
            timeout=self._request_timeout,
        )
        if not isinstance(result, dict):
            raise McpProtocolError("initialize response was not an object")
        self._server_info = result
        self._initialized = True
        # Acknowledge with the initialized notification.
        await self._transport.send_notification("notifications/initialized", {})
        return result

    async def list_tools(self) -> list[McpToolDef]:
        if not self._initialized:
            await self.initialize()
        req_id = self._allocate_id()
        result = await self._transport.send_request(
            "tools/list", None, req_id=req_id, timeout=self._request_timeout
        )
        tools = (result or {}).get("tools", []) if isinstance(result, dict) else []
        out: list[McpToolDef] = []
        for t in tools:
            if not isinstance(t, dict) or not t.get("name"):
                continue
            out.append(
                McpToolDef(
                    name=str(t["name"]),
                    description=str(t.get("description", "")),
                    input_schema=t.get("inputSchema") or {},
                )
            )
        return out

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        if not self._initialized:
            await self.initialize()
        req_id = self._allocate_id()
        params = {"name": name, "arguments": arguments or {}}
        effective_timeout = float(timeout) if timeout is not None else self._request_timeout
        try:
            return await self._transport.send_request(
                "tools/call", params, req_id=req_id, timeout=effective_timeout
            )
        except asyncio.CancelledError:
            # Surface the cancellation to the server, then re-raise so the
            # caller's task is cancelled as expected.
            try:
                await self._transport.cancel(req_id, reason="caller cancelled")
            except Exception:
                logger.debug("mcp cancel notify failed", exc_info=True)
            raise

    async def reconnect(self) -> None:
        """Close and re-establish the transport + re-run initialize.

        Used to recover from a dropped subprocess / dead HTTP connection.
        """
        try:
            await self._transport.close()
        except Exception:
            logger.debug("mcp reconnect: close failed", exc_info=True)
        # Build a fresh transport of the same kind. We can't re-construct from
        # a bare protocol object portably, so each transport implements its own
        # fresh-copy via __class__ + the original ctor args (cached below).
        self._transport = self._fresh_transport()
        self._initialized = False
        self._server_info = {}
        await self._transport.start()

    def _fresh_transport(self) -> McpTransport:
        """Return a new, unconnected transport equivalent to the current one."""
        t = self._transport
        if isinstance(t, StdioTransport):
            return StdioTransport(t._command, t._args, t._env, stderr_limit=t._stderr_limit)
        if isinstance(t, HttpTransport):
            return HttpTransport(url=t._url, timeout=t._timeout)
        # Fallback: reuse the same transport (caller-managed), e.g. in tests.
        return t

    async def close(self) -> None:
        """Graceful shutdown (idempotent)."""
        self._initialized = False
        try:
            await self._transport.close()
        except Exception:
            logger.debug("mcp close failed", exc_info=True)
