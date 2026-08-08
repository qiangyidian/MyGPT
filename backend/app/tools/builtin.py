"""Builtin tools.

Each tool subclasses BaseTool and declares its parameters via ToolParameter,
which gives it a correct OpenAI function schema for free (see BaseTool.to_openai_schema).

Tools are intentionally self-contained: they read config through get_settings(),
open short-lived clients (httpx / sync sqlalchemy) inside run(), and never mutate
global state. DANGEROUS tools (code exec, raw SQL) set dangerous=True so the
agent loop / UI can gate them behind confirmation.

All run() methods are async and return JSON-serialisable values.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
import tempfile
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import create_engine, text

from app.core.config import get_settings
from app.db import AsyncSessionLocal
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.tools.base import BaseTool, ToolError, ToolParameter

# How much of an HTTP body we keep — payloads can be huge and bust the LLM context.
_HTTP_MAX_CHARS = 4000
# Hard cap on how many bytes we will ever buffer from a single HTTP response
# before truncating. Stops a hostile/misconfigured endpoint from exhausting
# memory by serving a multi-hundred-MB body (the [:max_chars] slice used to run
# only AFTER the whole body was already decoded into memory).
_HTTP_MAX_BYTES = 4 * 1024 * 1024
# Sync DB query safety cap — read-only-ish, bounded row count.
_DB_ROW_LIMIT = 50


def _is_private_ip(ip: ipaddress._BaseAddress) -> bool:
    """True for any address an SSRF-safe tool must never reach.

    Covers private/loopback/link-local/reserved/multicast/unspecified ranges, and
    also reduces IPv4-mapped IPv6 (e.g. ::ffff:169.254.169.254) to its embedded
    IPv4 and re-tests — some stdlib versions do NOT flag the mapped form as
    private, which would otherwise bypass the check.
    """
    if (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    ):
        return True
    if ip.version == 6:
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None and _is_private_ip(mapped):
            return True
    return False


def _resolve_public_ip(host: str) -> str | None:
    """Resolve ``host`` once and return one validated public IP to pin to.

    Returns None if the host is unset, unresolvable, or resolves to ANY
    private/loopback/link-local address. The caller connects directly to the
    returned IP (keeping the original Host header + TLS SNI), which eliminates the
    DNS-rebinding TOCTOU window where httpx's own second resolution could flip the
    record to an internal address between our check and the connect. Prefers IPv4
    for reachability when both families are available.
    """
    if not host:
        return None
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return None
    chosen: str | None = None
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except (ValueError, IndexError):
            return None
        if _is_private_ip(ip):
            return None
        candidate = str(ip) if ip.version == 4 else f"[{ip}]"
        if chosen is None or (ip.version == 4 and ":" in chosen):
            # First valid address, or upgrade from an IPv6 choice to IPv4.
            chosen = candidate
    return chosen


async def _bounded_request(
    client: httpx.AsyncClient, method: str, url: str, *, max_bytes: int = _HTTP_MAX_BYTES, **kwargs: Any
) -> tuple[int, bytes, str, str]:
    """Stream a request and read at most ``max_bytes`` of the body.

    Returns (status_code, body_bytes, content_type, final_url). The body is never
    fully buffered past max_bytes, so a huge response cannot exhaust memory. Any
    extra bytes are simply dropped (callers signal truncation via [:max_chars]).
    """
    async with client.stream(method, url, **kwargs) as resp:
        chunks: list[bytes] = []
        total = 0
        async for chunk in resp.aiter_raw():
            remaining = max_bytes - total
            if remaining <= 0:
                break
            if len(chunk) > remaining:
                chunks.append(chunk[:remaining])
                break
            chunks.append(chunk)
            total += len(chunk)
        return resp.status_code, b"".join(chunks), resp.headers.get("content-type", ""), str(resp.url)


class DateTimeNowTool(BaseTool):
    """Return the current UTC timestamp as an ISO-8601 string."""

    name = "datetime_now"
    description = "Get the current UTC date and time in ISO-8601 format. Takes no parameters."
    category = "utility"
    dangerous = False
    parameters: list[ToolParameter] = []

    async def run(self, **kwargs: Any) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        return {
            "utc_iso": now.isoformat(),
            "utc_rfc2822": now.strftime("%a, %d %b %Y %H:%M:%S +0000"),
            "unix_ts": int(now.timestamp()),
        }


class HttpGetTool(BaseTool):
    """Fetch a URL and return truncated response text."""

    name = "http_get"
    description = (
        "Perform an HTTP GET request and return the response body as text (truncated). "
        "Useful for retrieving web pages or JSON APIs."
    )
    category = "network"
    dangerous = False
    parameters = [
        ToolParameter(
            name="url",
            type="string",
            description="Absolute URL to fetch (http or https).",
            required=True,
        ),
        ToolParameter(
            name="max_chars",
            type="integer",
            description="Maximum number of characters of the response body to return.",
            required=False,
            default=_HTTP_MAX_CHARS,
        ),
    ]

    async def run(self, **kwargs: Any) -> dict[str, Any]:
        url = kwargs.get("url")
        if not url or not isinstance(url, str):
            raise ToolError("'url' is required and must be a string")
        max_chars = int(kwargs.get("max_chars", _HTTP_MAX_CHARS) or _HTTP_MAX_CHARS)
        if max_chars <= 0:
            max_chars = _HTTP_MAX_CHARS

        # SSRF guard, in order:
        #   * scheme allow-list (http/https only);
        #   * operator network-egress policy (NetworkPolicy) — allow-all by default;
        #   * resolve ONCE and connect to a validated public IP, so httpx's own
        #     second resolution cannot DNS-rebind us to an internal host (TOCTOU).
        try:
            parsed = httpx.URL(url)
        except Exception as exc:  # noqa: BLE001
            raise ToolError(f"invalid url: {exc}")
        if parsed.scheme not in ("http", "https"):
            raise ToolError("only http/https URLs are allowed")

        from app.agents.network_policy import load_active_policy

        if load_active_policy().decide(parsed.host, parsed.scheme) == "forbidden":
            raise ToolError(f"network policy forbids egress to {parsed.host!r}")

        pinned_ip = _resolve_public_ip(parsed.host)
        if pinned_ip is None:
            raise ToolError(
                "URL host is unresolvable or resolves to a blocked (internal/private) address"
            )

        timeout = httpx.Timeout(15.0, connect=10.0)
        headers = {
            "User-Agent": "MyGPT-Tool/1.0 (+https://example.com/bot)",
            "Accept": "text/html,application/json,text/plain,*/*",
            # Pin the connection to the validated IP but keep the original Host
            # header so virtual-hosted servers still route correctly.
            "Host": parsed.host,
        }
        # follow_redirects=False: don't let a server redirect us to an internal
        # host. status_code is returned so the caller can follow a redirect
        # explicitly if it chooses.
        ip_url = parsed.copy_with(host=pinned_ip)
        # For HTTPS, drive TLS SNI + cert verification with the ORIGINAL hostname
        # (the URL host is now the IP), so the cert still validates and the right
        # vhost is selected on the server.
        req_kwargs: dict[str, Any] = {"headers": headers}
        if parsed.scheme == "https":
            req_kwargs["extensions"] = {"sni_hostname": parsed.host}
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                status_code, raw, content_type, final_url = await _bounded_request(
                    client, "GET", str(ip_url), **req_kwargs
                )
        except httpx.TimeoutException as exc:
            return {"ok": False, "error": f"timeout: {exc}", "status_code": None, "body": ""}
        except httpx.HTTPError as exc:
            return {"ok": False, "error": f"http error: {exc}", "status_code": None, "body": ""}

        body = raw.decode("utf-8", errors="replace")
        truncated = len(body) > max_chars
        return {
            "ok": True,
            "status_code": status_code,
            "url": final_url,
            "content_type": content_type,
            "truncated": truncated,
            "body": body[:max_chars],
        }


class WebSearchTool(BaseTool):
    """Lightweight web search.

    Strategy:
      1. If ``WEB_SEARCH_ENDPOINT`` is configured, call it (GET or POST per
         ``WEB_SEARCH_METHOD``), attach ``WEB_SEARCH_API_KEY`` in the common
         provider-accepted forms, and normalize the JSON response — a
         provider-agnostic path covering SearXNG / Bing / Tavily / Serper shapes.
      2. Otherwise fall back to scraping DuckDuckGo's HTML results and parsing the
         result links/snippets with a tolerant regex.

    Everything is wrapped in try/except so a transient network/parse failure returns
    an empty result list rather than raising.
    """

    name = "web_search"
    description = (
        "Search the web for a query and return a list of result items "
        "(title, url, snippet). Best-effort; may return an empty list on failure."
    )
    category = "network"
    dangerous = False
    parameters = [
        ToolParameter(
            name="query",
            type="string",
            description="The search query.",
            required=True,
        ),
        ToolParameter(
            name="top_k",
            type="integer",
            description="Maximum number of results to return.",
            required=False,
            default=5,
        ),
    ]

    async def run(self, **kwargs: Any) -> dict[str, Any]:
        query = str(kwargs.get("query") or "").strip()
        if not query:
            raise ToolError("'query' is required")
        try:
            top_k = int(kwargs.get("top_k", 5))
        except (TypeError, ValueError):
            top_k = 5
        if top_k <= 0:
            top_k = 5

        settings = get_settings()
        endpoint = (getattr(settings, "WEB_SEARCH_ENDPOINT", "") or "").strip()
        api_key = (getattr(settings, "WEB_SEARCH_API_KEY", "") or "").strip()
        method = (getattr(settings, "WEB_SEARCH_METHOD", "get") or "get").strip().lower() or "get"

        results: list[dict[str, str]] = []
        try:
            if endpoint:
                results = await self._search_via_endpoint(
                    endpoint, query, top_k, api_key=api_key, method=method
                )
            else:
                results = await self._search_duckduckgo(query, top_k)
        except Exception as exc:  # noqa: BLE001 — must never raise
            return {"ok": False, "error": str(exc), "results": [], "query": query}

        return {"ok": True, "query": query, "results": results[:top_k]}

    async def _search_via_endpoint(
        self,
        endpoint: str,
        query: str,
        top_k: int,
        *,
        api_key: str = "",
        method: str = "get",
    ) -> list[dict[str, str]]:
        timeout = httpx.Timeout(15.0, connect=10.0)
        headers: dict[str, str] = {"Accept": "application/json"}
        # Send the key in every common form; each provider reads the header it
        # expects and ignores the rest. (Bing: Ocp-Apim-Subscription-Key;
        # generic bearer: Authorization; Serper: X-API-DB-KEY.)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
            headers["X-API-Key"] = api_key
            headers["X-API-DB-KEY"] = api_key
            headers["Ocp-Apim-Subscription-Key"] = api_key
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            if method == "post":
                # Tavily reads query + api_key from the JSON body; Serper reads q
                # from the body + X-API-DB-KEY from the header. Sending all forms
                # lets one call satisfy either provider.
                body: dict[str, Any] = {
                    "q": query,
                    "query": query,
                    "max_results": top_k,
                    "num": top_k,
                }
                if api_key:
                    body["api_key"] = api_key
                _status, raw, _ct, _url = await _bounded_request(
                    client, "POST", endpoint, json=body, headers=headers
                )
            else:
                # Common query param names; the provider decides which it reads.
                params = {"q": query, "query": query, "format": "json", "count": top_k}
                _status, raw, _ct, _url = await _bounded_request(
                    client, "GET", endpoint, params=params, headers=headers
                )
            data: Any
            try:
                # Bounded read above caps memory; parse the (possibly truncated)
                # JSON. A truncation-induced parse failure hands off to the DDG
                # HTML scraper, preserving the historic fallback behaviour.
                data = json.loads(raw.decode("utf-8", errors="replace"))
            except Exception:
                # If the endpoint returns HTML (or non-JSON), hand off to the DDG
                # HTML scraper instead.
                return await self._search_duckduckgo(query, top_k)

        return self._normalize_search_data(data, top_k)

    async def _search_duckduckgo(self, query: str, top_k: int) -> list[dict[str, str]]:
        """Scrape DuckDuckGo HTML results as a dependency-free fallback."""
        import html
        import re

        url = "https://html.duckduckgo.com/html/"
        timeout = httpx.Timeout(15.0, connect=10.0)
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; MyGPT-Tool/1.0)",
        }
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            _status, raw, _ct, _url = await _bounded_request(
                client, "POST", url, data={"q": query}, headers=headers
            )
        page = raw.decode("utf-8", errors="replace")

        results: list[dict[str, str]] = []
        # DDG HTML wraps results in <a class="result__a" href="...">title</a>
        # with a sibling <a class="result__snippet" ...>snippet</a>.
        title_re = re.compile(
            r'<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL
        )
        snippet_re = re.compile(
            r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL
        )
        tag_re = re.compile(r"<[^>]+>")

        titles = title_re.findall(page)
        snippets = snippet_re.findall(page)
        for i, (raw_href, raw_title) in enumerate(titles):
            if i >= top_k:
                break
            # DDG wraps real URLs in a redirect like //duckduckgo.com/l/?uddg=<encoded>
            href = self._unwrap_ddg_href(raw_href)
            # Strip tags then decode HTML entities (DDG emits &#x27; etc.) so a
            # source title reads "Python's" not "Python&#x27;s" in the panel.
            title = html.unescape(tag_re.sub("", raw_title)).strip()
            snippet = ""
            if i < len(snippets):
                snippet = html.unescape(tag_re.sub("", snippets[i])).strip()
            if not title and not href:
                continue
            results.append({"title": title, "url": href, "snippet": snippet})
        return results

    @staticmethod
    def _unwrap_ddg_href(href: str) -> str:
        from urllib.parse import parse_qs, urlparse

        if href.startswith("//"):
            href = "https:" + href
        parsed = urlparse(href)
        if "duckduckgo.com" in (parsed.netloc or "") and parsed.path.startswith("/l/"):
            qs = parse_qs(parsed.query)
            uddg = qs.get("uddg", [None])[0]
            if uddg:
                return uddg
        return href

    @staticmethod
    def _normalize_search_data(data: Any, top_k: int) -> list[dict[str, str]]:
        """Map assorted JSON search-API shapes onto {title,url,snippet}.

        Covers SearXNG/Google ``items``, DDG ``Results``, Tavily ``results``,
        Serper ``organic``, and Bing ``webPages.value`` without branching on
        provider — the first list-shaped container wins.
        """
        out: list[dict[str, str]] = []
        items: list[Any] = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            # Bing nests its hits under webPages.value.
            wp = data.get("webPages")
            if isinstance(wp, dict) and isinstance(wp.get("value"), list):
                items = wp["value"]
            else:
                for key in ("Results", "results", "items", "data", "organic"):
                    v = data.get(key)
                    if isinstance(v, list):
                        items = v
                        break
                    if isinstance(v, dict):
                        nested = v.get("results") or v.get("items") or v.get("organic")
                        if isinstance(nested, list):
                            items = nested
                            break

        for raw in items:
            if not isinstance(raw, dict):
                continue
            title = str(
                raw.get("title")
                or raw.get("Title")
                or raw.get("name")
                or raw.get("heading")
                or ""
            )
            link = (
                raw.get("url")
                or raw.get("link")
                or raw.get("href")
                or raw.get("URL")
                or raw.get("FirstURL")  # DuckDuckGo JSON
                or raw.get("first")
                or raw.get("displayUrl")  # Bing v7
                or ""
            )
            snippet = str(
                raw.get("snippet")
                or raw.get("abstract")
                or raw.get("description")
                or raw.get("content")  # Tavily
                or raw.get("Text")  # DuckDuckGo JSON
                or raw.get("text")
                or raw.get("body")
                or ""
            )
            if not title and not link:
                continue
            out.append({"title": title, "url": str(link), "snippet": snippet})
            if len(out) >= top_k:
                break
        return out


class PythonExecTool(BaseTool):
    """Execute a Python snippet in a subprocess sandbox (DANGEROUS).

    The code is written to a temp file and run with `python <file>` in a fresh temp
    directory, with a hard timeout. stdout/stderr are captured and returned. We never
    eval the code inline (no exec()/eval() in this process).
    """

    name = "python_exec"
    description = (
        "Execute a Python 3 code snippet in a sandboxed subprocess and return "
        "stdout/stderr. DANGEROUS: the code runs with the process's permissions; "
        "use only for trusted computations. A 10-second timeout is enforced."
    )
    category = "code"
    dangerous = True
    parameters = [
        ToolParameter(
            name="code",
            type="string",
            description="The Python source code to execute.",
            required=True,
        ),
        ToolParameter(
            name="timeout",
            type="integer",
            description="Execution timeout in seconds (capped at 10).",
            required=False,
            default=10,
        ),
    ]

    async def run(self, **kwargs: Any) -> dict[str, Any]:
        code = kwargs.get("code")
        if not code or not isinstance(code, str):
            raise ToolError("'code' is required and must be a string")
        try:
            timeout = int(kwargs.get("timeout", 10))
        except (TypeError, ValueError):
            timeout = 10
        timeout = max(1, min(timeout, 10))

        # Defense in depth: the ToolGateway also gates this, but this run() is
        # reached directly by /api/tools/test (and any other caller that bypasses
        # the gateway), so enforce the same environment gate HERE. python_exec is
        # fail-closed outside dev unless explicitly opted in (ALLOW_PYTHON_EXEC)
        # or a real sandbox backend is configured (PYTHON_SANDBOX). Without this
        # guard, POST /api/tools/test {name:python_exec} is arbitrary code
        # execution with the backend process's privileges.
        from app.agents.policies.tool_policy import is_tool_allowed

        if not is_tool_allowed("python_exec", None):
            return {
                "ok": False,
                "error": (
                    "python_exec is disabled in this environment (allow only in dev, "
                    "or set ALLOW_PYTHON_EXEC=true / PYTHON_SANDBOX)"
                ),
                "stdout": "",
                "stderr": "",
                "returncode": None,
                "blocked": True,
            }

        with tempfile.TemporaryDirectory(prefix="pyexec_") as workdir:
            script_path = f"{workdir}/snippet.py"
            with open(script_path, "w", encoding="utf-8") as fh:
                fh.write(code)

            try:
                proc = await asyncio.create_subprocess_exec(
                    "python",
                    script_path,
                    cwd=workdir,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError:
                return {
                    "ok": False,
                    "error": "python interpreter not found",
                    "stdout": "",
                    "stderr": "",
                    "returncode": None,
                }

            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            except asyncio.TimeoutError:
                # Kill the timed-out child so it doesn't leak.
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()
                return {
                    "ok": False,
                    "error": f"timed out after {timeout}s",
                    "stdout": "",
                    "stderr": "",
                    "returncode": None,
                    "timed_out": True,
                }

            stdout = (stdout_b or b"").decode("utf-8", errors="replace")
            stderr = (stderr_b or b"").decode("utf-8", errors="replace")
            return {
                "ok": proc.returncode == 0,
                "stdout": stdout[:_HTTP_MAX_CHARS],
                "stderr": stderr[:_HTTP_MAX_CHARS],
                "returncode": proc.returncode,
            }


class DbQueryTool(BaseTool):
    """Read-only-ish SQL query against the app database (DANGEROUS).

    Uses a short-lived *sync* SQLAlchemy connection derived from DATABASE_URL (we
    swap the async driver for a sync one). Only the first _DB_ROW_LIMIT rows are
    returned. Any error is wrapped, never raised.
    """

    name = "db_query"
    description = (
        "Run a SQL query against the application database and return up to 50 rows. "
        "DANGEROUS: intended for read-only SELECTs; mutations are discouraged."
    )
    category = "data"
    dangerous = True
    parameters = [
        ToolParameter(
            name="sql",
            type="string",
            description="The SQL statement to execute (prefer SELECT).",
            required=True,
        ),
    ]

    @staticmethod
    def _sync_url() -> str:
        url = get_settings().DATABASE_URL
        # asyncpg -> psycopg2, sqlite+aiosqlite -> sqlite
        replacements = (
            ("+asyncpg", "+psycopg2"),
            ("+aiosqlite", ""),
            ("+asyncmy", "+pymysql"),
        )
        for old, new in replacements:
            if old in url:
                url = url.replace(old, new)
                break
        return url

    async def run(self, **kwargs: Any) -> dict[str, Any]:
        sql = str(kwargs.get("sql") or "").strip()
        if not sql:
            raise ToolError("'sql' is required")
        # Defense in depth: the ToolGateway also validates, but this path is
        # reached directly by /api/tools/test, so enforce the same hardening
        # here. Rejects multi-statement, DML/DDL, and session-control keywords.
        from app.agents.policies.tool_policy import UnsafeSQLError, validate_readonly_sql

        try:
            sql = validate_readonly_sql(sql)
        except UnsafeSQLError as exc:
            return {
                "ok": False,
                "error": str(exc),
                "rows": [],
            }

        def _exec() -> dict[str, Any]:
            engine = create_engine(self._sync_url(), future=True)
            try:
                with engine.connect() as conn:
                    result = conn.execute(text(sql))
                    cols = list(result.keys()) if result.returns_rows else []
                    rows: list[dict[str, Any]] = []
                    if result.returns_rows:
                        for row in result.fetchmany(_DB_ROW_LIMIT):
                            rows.append(
                                {c: _jsonable(v) for c, v in zip(cols, row)}
                            )
                    return {
                        "ok": True,
                        "columns": cols,
                        "rows": rows,
                        "rowcount": len(rows),
                        "truncated": result.returns_rows and len(rows) >= _DB_ROW_LIMIT,
                    }
            finally:
                engine.dispose()

        try:
            # Run the blocking sync DB call off the event loop.
            return await asyncio.to_thread(_exec)
        except Exception as exc:  # noqa: BLE001 — wrap, don't raise
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "rows": []}


class FileAnalyzeTool(BaseTool):
    """Read a document's parsed text from the DB and return a summary-ish payload.

    This is the lightweight placeholder file analyzer: given a document_id it loads
    the Document and its chunk texts from the DB and returns filename, status,
    total length, and the first chunk's content. It deliberately avoids touching the
    filesystem or storage backend so it works regardless of storage config.
    """

    name = "file_analyze"
    description = (
        "Look up a stored document by id and return its filename, status, and "
        "extracted text (truncated). Useful for asking questions about an uploaded file."
    )
    category = "rag"
    dangerous = False
    parameters = [
        ToolParameter(
            name="document_id",
            type="string",
            description="UUID of the document to analyze.",
            required=True,
        ),
        ToolParameter(
            name="max_chars",
            type="integer",
            description="Maximum characters of extracted text to return.",
            required=False,
            default=_HTTP_MAX_CHARS,
        ),
    ]

    async def run(self, **kwargs: Any) -> dict[str, Any]:
        raw_id = str(kwargs.get("document_id") or "").strip()
        if not raw_id:
            raise ToolError("'document_id' is required")
        try:
            max_chars = int(kwargs.get("max_chars", _HTTP_MAX_CHARS))
        except (TypeError, ValueError):
            max_chars = _HTTP_MAX_CHARS
        if max_chars <= 0:
            max_chars = _HTTP_MAX_CHARS

        from sqlalchemy import select

        async with AsyncSessionLocal() as session:
            doc = (
                await session.execute(
                    select(Document).where(Document.id == raw_id)
                )
            ).scalar_one_or_none()
            if doc is None:
                return {"ok": False, "error": "document not found", "document_id": raw_id}

            chunk_rows = (
                await session.execute(
                    select(DocumentChunk.content)
                    .where(DocumentChunk.document_id == doc.id)
                    .order_by(DocumentChunk.chunk_index)
                )
            ).scalars().all()

        full_text = "\n".join(c for c in chunk_rows if c)
        truncated = len(full_text) > max_chars
        return {
            "ok": True,
            "document_id": str(doc.id),
            "filename": doc.filename,
            "file_type": doc.file_type,
            "status": doc.status,
            "chunk_count": doc.chunk_count,
            "text_length": len(full_text),
            "truncated": truncated,
            "text": full_text[:max_chars],
        }


def _jsonable(value: Any) -> Any:
    """Coerce a DB cell value into something json.dumps can serialise."""
    import decimal
    import uuid

    if isinstance(value, (uuid.UUID, decimal.Decimal)):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        try:
            return value.decode("utf-8")
        except Exception:
            return value.hex()
    return value


__all__ = [
    "DateTimeNowTool",
    "HttpGetTool",
    "WebSearchTool",
    "PythonExecTool",
    "DbQueryTool",
    "FileAnalyzeTool",
]
