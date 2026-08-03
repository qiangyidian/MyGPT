import { describe, it, expect } from "vitest";
import {
  SSEFrameDecoder,
  parseSSEStream,
  isAbortError,
  type SSEFrame,
} from "@/lib/sse-parser";

/** Feed `text` to a fresh decoder in fixed-size chunks; return all frames. */
function feedChunked(text: string, chunkSize: number): SSEFrame[] {
  const dec = new SSEFrameDecoder();
  const out: SSEFrame[] = [];
  for (let i = 0; i < text.length; i += chunkSize) {
    out.push(...dec.push(text.slice(i, i + chunkSize)));
  }
  const last = dec.flush();
  if (last) out.push(last);
  return out;
}

/** Feed one character at a time (exercises every possible boundary). */
const feedCharByChar = (text: string) => feedChunked(text, 1);

/** Feed the whole string in one push. */
const feedWhole = (text: string) => feedChunked(text, text.length);

const TOKEN_STREAM = [
  "event: token",
  'data: {"delta":"abc"}',
  "",
  "event: token",
  'data: {"delta":"你好"}',
  "",
  ": keepalive",
  "",
  "event: done",
  'data: {"finish_reason":"stop"}',
  "",
  "",
].join("\n");

const EXPECTED = [
  { event: "token", data: '{"delta":"abc"}' },
  { event: "token", data: '{"delta":"你好"}' },
  { event: "done", data: '{"finish_reason":"stop"}' },
];

describe("SSEFrameDecoder — chunk-independence", () => {
  it("parses identically whether fed whole, char-by-char, or in 3-byte chunks", () => {
    expect(feedWhole(TOKEN_STREAM)).toEqual(EXPECTED);
    expect(feedCharByChar(TOKEN_STREAM)).toEqual(EXPECTED);
    for (let size = 2; size <= 7; size++) {
      expect(feedChunked(TOKEN_STREAM, size)).toEqual(EXPECTED);
    }
  });

  it("handles \\r\\n line endings (cut inside \\r\\n\\r\\n)", () => {
    const crlf = [
      "event: token",
      'data: {"delta":"x"}',
      "",
      "event: done",
      'data: {"finish_reason":"length"}',
      "",
      "",
    ].join("\r\n");
    expect(feedCharByChar(crlf)).toEqual([
      { event: "token", data: '{"delta":"x"}' },
      { event: "done", data: '{"finish_reason":"length"}' },
    ]);
  });

  it("joins multiple data: lines with newline", () => {
    const s = ["data: line1", "data: line2", "data: line3", "", ""].join("\n");
    expect(feedCharByChar(s)).toEqual([{ event: "", data: "line1\nline2\nline3" }]);
  });

  it("strips a single leading space after the colon, keeps the rest", () => {
    const s = ["data:   spaced", "", ""].join("\n"); // 3 spaces → 1 stripped, 2 kept
    expect(feedWhole(s)).toEqual([{ event: "", data: "  spaced" }]);
  });

  it("ignores heartbeat / comment lines (leading colon)", () => {
    const s = [": ping", ":heartbeat", "event: token", 'data: {"delta":"a"}', "", ""].join("\n");
    expect(feedCharByChar(s)).toEqual([{ event: "token", data: '{"delta":"a"}' }]);
  });

  it("flushes a pending frame at EOF even without a trailing blank line", () => {
    // No final empty line — socket ended mid-stream.
    const dec = new SSEFrameDecoder();
    const frames: SSEFrame[] = [];
    frames.push(...dec.push('event: token\ndata: {"delta":"partial"}'));
    const last = dec.flush();
    if (last) frames.push(last);
    expect(frames).toEqual([{ event: "token", data: '{"delta":"partial"}' }]);
  });

  it("emits nothing for a stream of only separators/heartbeats", () => {
    expect(feedCharByChar("\n\n\n: hi\n\n")).toEqual([]);
  });

  it("supports data with no value (empty data line)", () => {
    const s = ["data:", "", ""].join("\n");
    expect(feedWhole(s)).toEqual([{ event: "", data: "" }]);
  });
});

describe("parseSSEStream — byte-level + abort", () => {
  async function fromChunks(chunkStrings: string[]): Promise<SSEFrame[]> {
    const encoder = new TextEncoder();
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        for (const c of chunkStrings) controller.enqueue(encoder.encode(c));
        controller.close();
      },
    });
    const out: SSEFrame[] = [];
    for await (const f of parseSSEStream(stream)) out.push(f);
    return out;
  }

  it("parses a normal multi-event stream", async () => {
    const frames = await fromChunks([TOKEN_STREAM]);
    expect(frames).toEqual(EXPECTED);
  });

  it("survives a cut in the middle of a multibyte (Chinese) character in the JSON", async () => {
    // "你好" → split the UTF-8 bytes of the first char across chunks.
    const encoder = new TextEncoder();
    const full =
      'event: token\ndata: {"delta":"你好"}\n\nevent: done\ndata: {"finish_reason":"stop"}\n\n';
    const bytes = encoder.encode(full);
    // Cut at byte 30 (inside 你, which is bytes 17..19 of the data) — arbitrary mid-char.
    const cut = Math.min(30, bytes.length - 1);
    const a = bytes.slice(0, cut);
    const b = bytes.slice(cut);
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(a);
        controller.enqueue(b);
        controller.close();
      },
    });
    const out: SSEFrame[] = [];
    for await (const f of parseSSEStream(stream)) out.push(f);
    expect(out).toEqual([
      { event: "token", data: '{"delta":"你好"}' },
      { event: "done", data: '{"finish_reason":"stop"}' },
    ]);
  });

  it("stops cleanly on abort (not a thrown error)", async () => {
    const encoder = new TextEncoder();
    // Simulate a real fetch whose read() rejects with AbortError after a chunk.
    const abortErr = new Error("The user aborted a request.");
    abortErr.name = "AbortError";
    const stream = new ReadableStream<Uint8Array>({
      start(c) {
        c.enqueue(encoder.encode('event: token\ndata: {"delta":"a"}\n\n'));
      },
      pull() {
        throw abortErr; // next read() rejects with AbortError
      },
    });
    const out: SSEFrame[] = [];
    for await (const f of parseSSEStream(stream)) out.push(f);
    // Did not throw; got the first token before the abort.
    expect(out).toEqual([{ event: "token", data: '{"delta":"a"}' }]);
  });

  it("flushes a pending frame when the socket ends without done", async () => {
    const frames = await fromChunks(['event: token\ndata: {"delta":"partial"}']);
    expect(frames).toEqual([{ event: "token", data: '{"delta":"partial"}' }]);
  });
});

describe("isAbortError", () => {
  it("detects AbortError and aborted signals", () => {
    const err = new Error("x");
    err.name = "AbortError";
    expect(isAbortError(err)).toBe(true);
    const ac = new AbortController();
    expect(isAbortError(new Error("nope"), ac.signal)).toBe(false);
    ac.abort();
    expect(isAbortError(new Error("nope"), ac.signal)).toBe(true);
  });
});
