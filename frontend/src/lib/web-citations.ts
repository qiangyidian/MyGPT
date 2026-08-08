/**
 * Turn real web tool output into verifiable sources for the "来源" panel.
 *
 * The backend already runs `web_search` / `http_get` in search / deep_research
 * / escalated-auto modes, and the ToolGateway forwards each tool's raw output
 * (stringified JSON) as the `tool_result.result` SSE field. Until now that
 * payload was only rendered as an Execution-tab tool step; these helpers
 * promote it to structured {@link Citation} objects so the Sources tab lists
 * the REAL web pages and search results the agent used.
 *
 * Mirror-of-backend contract: `web_search` → `{ok, query, results:[{title,url,snippet}]}`;
 * `http_get` → `{ok, status_code, url, content_type, body, truncated}`. These
 * are best-effort: any malformed/missing payload returns `[]` rather than
 * throwing, so a bad result never breaks the stream.
 */
import type { Citation } from "@/lib/types";

const WEB_TOOLS = new Set(["web_search", "http_get"]);

function domainOf(url?: string | null): string | null {
  if (!url) return null;
  try {
    return new URL(url).hostname.replace(/^www\./, "") || null;
  } catch {
    return null;
  }
}

/** Normalized dedup key for a web URL: host + path, no fragment/query/trailing slash. */
function urlKey(url?: string | null): string | null {
  if (!url) return null;
  try {
    const u = new URL(url);
    const path = u.pathname.replace(/\/+$/, "");
    return `${u.hostname.replace(/^www\./, "")}${path}`.toLowerCase();
  } catch {
    return null;
  }
}

/** Best-effort plain-text snippet from an HTML body: strip script/style/tags, collapse. */
function snippetFromHtml(body?: string | null): string {
  if (!body) return "";
  const text = body
    .replace(/<script[\s\S]*?<\/script>/gi, " ")
    .replace(/<style[\s\S]*?<\/style>/gi, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/gi, " ")
    .replace(/&amp;/gi, "&")
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">")
    .replace(/&#(\d+);/g, (_, d) => {
      const n = Number(d);
      return Number.isFinite(n) ? String.fromCodePoint(n) : " ";
    })
    .replace(/\s+/g, " ")
    .trim();
  return text.slice(0, 300);
}

function asObject(result: unknown): Record<string, unknown> | null {
  if (result == null) return null;
  if (typeof result === "string") {
    const trimmed = result.trim();
    if (!trimmed) return null;
    try {
      const parsed = JSON.parse(trimmed);
      return parsed && typeof parsed === "object" ? (parsed as Record<string, unknown>) : null;
    } catch {
      return null;
    }
  }
  return typeof result === "object" ? (result as Record<string, unknown>) : null;
}

function isRecordArray(v: unknown): v is Record<string, unknown>[] {
  return Array.isArray(v) && v.every((x) => x != null && typeof x === "object");
}

function str(v: unknown): string {
  return typeof v === "string" ? v : "";
}

/** Extract web Citation[] from a `web_search` / `http_get` tool_result payload. */
export function extractWebCitations(toolName: string, result: unknown): Citation[] {
  const name = (toolName || "").toLowerCase();
  if (!WEB_TOOLS.has(name)) return [];

  const parsed = asObject(result);
  if (!parsed) return [];

  const now = new Date().toISOString();

  if (name === "web_search") {
    const query = str(parsed.query);
    const results = isRecordArray(parsed.results) ? parsed.results : [];
    const out: Citation[] = [];
    results.forEach((item, i) => {
      const url = str(item.url || item.link || item.href);
      if (!url) return; // no verifiable target
      const title = str(item.title || item.name);
      out.push({
        document_id: null,
        document_name: title.trim() || domainOf(url) || "网络来源",
        chunk_id: null,
        chunk_index: i,
        snippet: str(item.snippet || item.abstract || item.description),
        score: 0,
        source_type: "web",
        url,
        accessed_at: now,
        metadata: { tool: "web_search", ...(query ? { query } : {}) },
      });
    });
    return out;
  }

  // http_get: the page the agent actually read → one source.
  const url = str(parsed.url);
  if (!url) return [];
  return [
    {
      document_id: null,
      document_name: domainOf(url) || "网络来源",
      chunk_id: null,
      chunk_index: 0,
      snippet: snippetFromHtml(typeof parsed.body === "string" ? parsed.body : ""),
      score: 0,
      source_type: "web",
      url,
      accessed_at: now,
      metadata: {
        tool: "http_get",
        ...(parsed.content_type ? { content_type: str(parsed.content_type) } : {}),
      },
    },
  ];
}

/**
 * Merge two citation lists without duplicates. Web sources dedup by normalized
 * URL (fragment/query/trailing-slash-insensitive); non-web sources are kept
 * as-is (their identity is document_id + chunk, already unique from retrieval).
 */
export function mergeCitations(existing: Citation[], incoming: Citation[]): Citation[] {
  const seen = new Set<string>();
  for (const c of existing) {
    if (c.source_type === "web") {
      const k = urlKey(c.url);
      if (k) seen.add(k);
    }
  }
  const out = [...existing];
  for (const c of incoming) {
    if (c.source_type === "web") {
      const k = urlKey(c.url);
      if (k) {
        if (seen.has(k)) continue;
        seen.add(k);
      }
    }
    out.push(c);
  }
  return out;
}
