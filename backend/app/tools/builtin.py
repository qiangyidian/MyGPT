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
# Sync DB query safety cap — read-only-ish, bounded row count.
_DB_ROW_LIMIT = 50


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

        timeout = httpx.Timeout(15.0, connect=10.0)
        headers = {
            "User-Agent": "MyGPT-Tool/1.0 (+https://example.com/bot)",
            "Accept": "text/html,application/json,text/plain,*/*",
        }
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                resp = await client.get(url, headers=headers)
        except httpx.TimeoutException as exc:
            return {"ok": False, "error": f"timeout: {exc}", "status_code": None, "body": ""}
        except httpx.HTTPError as exc:
            return {"ok": False, "error": f"http error: {exc}", "status_code": None, "body": ""}

        body = resp.text or ""
        truncated = len(body) > max_chars
        return {
            "ok": True,
            "status_code": resp.status_code,
            "url": str(resp.url),
            "content_type": resp.headers.get("content-type", ""),
            "truncated": truncated,
            "body": body[:max_chars],
        }


class WebSearchTool(BaseTool):
    """Lightweight web search.

    Strategy:
      1. If WEB_SEARCH_ENDPOINT is configured, GET it (or a DuckDuckGo JSON endpoint)
         and forward the `query` param — robust provider-agnostic path.
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
        endpoint = getattr(settings, "WEB_SEARCH_ENDPOINT", "") or ""

        results: list[dict[str, str]] = []
        try:
            if endpoint:
                results = await self._search_via_endpoint(endpoint, query, top_k)
            else:
                results = await self._search_duckduckgo(query, top_k)
        except Exception as exc:  # noqa: BLE001 — must never raise
            return {"ok": False, "error": str(exc), "results": [], "query": query}

        return {"ok": True, "query": query, "results": results[:top_k]}

    async def _search_via_endpoint(
        self, endpoint: str, query: str, top_k: int
    ) -> list[dict[str, str]]:
        timeout = httpx.Timeout(15.0, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            # Try common query param names; provider decides which it reads.
            params = {"q": query, "query": query, "format": "json", "count": top_k}
            resp = await client.get(endpoint, params=params, headers={"Accept": "application/json"})
            data: Any
            try:
                data = resp.json()
            except Exception:
                # If the endpoint returns HTML, hand off to the DDG HTML scraper instead.
                return await self._search_duckduckgo(query, top_k)

        return self._normalize_search_data(data, top_k)

    async def _search_duckduckgo(self, query: str, top_k: int) -> list[dict[str, str]]:
        """Scrape DuckDuckGo HTML results as a dependency-free fallback."""
        import re

        url = "https://html.duckduckgo.com/html/"
        timeout = httpx.Timeout(15.0, connect=10.0)
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; MyGPT-Tool/1.0)",
        }
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.post(url, data={"q": query}, headers=headers)
        html = resp.text or ""

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

        titles = title_re.findall(html)
        snippets = snippet_re.findall(html)
        for i, (raw_href, raw_title) in enumerate(titles):
            if i >= top_k:
                break
            # DDG wraps real URLs in a redirect like //duckduckgo.com/l/?uddg=<encoded>
            href = self._unwrap_ddg_href(raw_href)
            title = tag_re.sub("", raw_title).strip()
            snippet = ""
            if i < len(snippets):
                snippet = tag_re.sub("", snippets[i]).strip()
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
        """Map assorted JSON search-API shapes onto {title,url,snippet}."""
        out: list[dict[str, str]] = []
        # Candidate containers: list itself, dict with Results / results / items.
        items: list[Any] = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            for key in ("Results", "results", "items", "data"):
                v = data.get(key)
                if isinstance(v, list):
                    items = v
                    break
                if isinstance(v, dict):
                    nested = v.get("results") or v.get("items")
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
                or raw.get("first")
                or ""
            )
            snippet = str(
                raw.get("snippet")
                or raw.get("abstract")
                or raw.get("description")
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
        if not sql.lower().startswith("select"):
            return {
                "ok": False,
                "error": "only SELECT statements are allowed",
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
