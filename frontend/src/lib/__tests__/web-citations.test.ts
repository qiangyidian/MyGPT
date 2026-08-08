import { describe, it, expect } from "vitest";

import {
  extractWebCitations,
  mergeCitations,
} from "@/lib/web-citations";
import type { Citation } from "@/lib/types";

// The backend ToolGateway wraps every tool result as {"content": <json string>},
// and both runtimes forward `.content` in the tool_result SSE event. So the
// frontend receives the tool's raw output as a JSON STRING — these are the
// real shapes that reach onToolResult.
const WEB_SEARCH_RESULT = JSON.stringify({
  ok: true,
  query: "rust async runtime",
  results: [
    { title: "Tokio", url: "https://tokio.rs/", snippet: "A runtime for writing async apps." },
    { title: "Async Book", url: "https://rust-lang.github.io/async-book/", snippet: "Asynchronous programming in Rust." },
  ],
});

const HTTP_GET_RESULT = JSON.stringify({
  ok: true,
  status_code: 200,
  url: "https://example.com/article",
  content_type: "text/html; charset=utf-8",
  truncated: false,
  body: "<html><body><h1>Real Page</h1><p>This is the fetched page body content.</p></body></html>",
});

describe("extractWebCitations — web_search", () => {
  it("turns each result item into a web Citation", () => {
    const out = extractWebCitations("web_search", WEB_SEARCH_RESULT);
    expect(out).toHaveLength(2);
    expect(out.every((c) => c.source_type === "web")).toBe(true);
    expect(out[0]).toMatchObject({
      source_type: "web",
      document_name: "Tokio",
      url: "https://tokio.rs/",
      snippet: "A runtime for writing async apps.",
    });
    // query from the payload is attached for provenance
    expect(out[0].metadata?.query).toBe("rust async runtime");
    expect(out[0].metadata?.tool).toBe("web_search");
    // accessed_at stamped as ISO
    expect(typeof out[0].accessed_at).toBe("string");
  });

  it("accepts an already-parsed object (not just a JSON string)", () => {
    const parsed = JSON.parse(WEB_SEARCH_RESULT);
    const out = extractWebCitations("web_search", parsed);
    expect(out).toHaveLength(2);
    expect(out[1].url).toBe("https://rust-lang.github.io/async-book/");
  });

  it("falls back to the domain for document_name when title is missing", () => {
    const r = JSON.stringify({
      ok: true,
      query: "x",
      results: [{ title: "", url: "https://docs.python.org/3/", snippet: "py" }],
    });
    const out = extractWebCitations("web_search", r);
    expect(out).toHaveLength(1);
    expect(out[0].document_name).toBe("docs.python.org");
  });

  it("skips items without a url (no verifiable target)", () => {
    const r = JSON.stringify({
      ok: true,
      query: "x",
      results: [
        { title: "no url", url: "", snippet: "s" },
        { title: "ok", url: "https://a.com", snippet: "s" },
      ],
    });
    const out = extractWebCitations("web_search", r);
    expect(out).toHaveLength(1);
    expect(out[0].url).toBe("https://a.com");
  });

  it("returns [] when results are missing/empty", () => {
    expect(extractWebCitations("web_search", JSON.stringify({ ok: true, query: "x", results: [] }))).toEqual([]);
    expect(extractWebCitations("web_search", JSON.stringify({ ok: true, query: "x" }))).toEqual([]);
  });

  it("returns [] on unparseable / malformed payload (never throws)", () => {
    expect(extractWebCitations("web_search", "not json{")).toEqual([]);
    expect(extractWebCitations("web_search", null)).toEqual([]);
    expect(extractWebCitations("web_search", undefined)).toEqual([]);
    expect(extractWebCitations("web_search", { results: "nope" })).toEqual([]);
  });
});

describe("extractWebCitations — http_get", () => {
  it("builds ONE citation from the fetched page, stripping HTML for the snippet", () => {
    const out = extractWebCitations("http_get", HTTP_GET_RESULT);
    expect(out).toHaveLength(1);
    const c = out[0];
    expect(c.source_type).toBe("web");
    expect(c.url).toBe("https://example.com/article");
    expect(c.document_name).toBe("example.com"); // domain fallback (no title)
    expect(c.snippet).not.toContain("<");
    expect(c.snippet).toContain("Real Page");
    expect(c.snippet).toContain("fetched page body content");
    expect(c.metadata?.tool).toBe("http_get");
  });

  it("caps the snippet length", () => {
    const long = "x".repeat(5000);
    const r = JSON.stringify({ ok: true, url: "https://a.com/b", body: long });
    const out = extractWebCitations("http_get", r);
    expect(out[0].snippet.length).toBeLessThanOrEqual(300);
  });

  it("returns a citation with empty snippet when body is absent but url exists", () => {
    const r = JSON.stringify({ ok: true, url: "https://a.com/b" });
    const out = extractWebCitations("http_get", r);
    expect(out).toHaveLength(1);
    expect(out[0].url).toBe("https://a.com/b");
    expect(out[0].snippet).toBe("");
  });

  it("returns [] when there is no url", () => {
    const r = JSON.stringify({ ok: true, body: "no url here" });
    expect(extractWebCitations("http_get", r)).toEqual([]);
  });
});

describe("extractWebCitations — guard", () => {
  it("ignores tool names that are not web tools", () => {
    expect(extractWebCitations("db_query", JSON.stringify({ rows: [] }))).toEqual([]);
    expect(extractWebCitations("datetime_now", "{}")).toEqual([]);
  });

  it("is case-insensitive on the tool name", () => {
    expect(extractWebCitations("WEB_SEARCH", WEB_SEARCH_RESULT)).toHaveLength(2);
  });
});

describe("mergeCitations", () => {
  const doc: Citation = {
    document_id: "d1",
    document_name: "handbook.pdf",
    chunk_id: "c1",
    chunk_index: 0,
    snippet: "kb text",
    score: 0.8,
    source_type: "document",
  };
  const web = (url: string, name = url): Citation => ({
    document_id: null,
    document_name: name,
    chunk_id: null,
    chunk_index: 0,
    snippet: "s",
    score: 0,
    source_type: "web",
    url,
  });

  it("appends web sources to existing document sources, preserving order", () => {
    const merged = mergeCitations([doc], [web("https://a.com", "A"), web("https://b.com", "B")]);
    expect(merged.map((c) => c.document_name)).toEqual(["handbook.pdf", "A", "B"]);
  });

  it("dedups web sources by normalized url (ignoring fragment / trailing slash)", () => {
    const merged = mergeCitations(
      [web("https://a.com/page", "A")],
      [web("https://a.com/page/", "A2"), web("https://a.com/page#sec", "A3"), web("https://b.com", "B")],
    );
    const names = merged.map((c) => c.document_name);
    expect(names).toContain("A");
    expect(names).not.toContain("A2");
    expect(names).not.toContain("A3");
    expect(names).toContain("B");
  });

  it("does not dedup document sources against web sources", () => {
    const merged = mergeCitations([web("https://a.com", "A")], [doc]);
    expect(merged).toHaveLength(2);
  });

  it("returns existing as-is when incoming is empty", () => {
    expect(mergeCitations([doc], [])).toEqual([doc]);
  });
});
