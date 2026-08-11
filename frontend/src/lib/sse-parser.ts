/**
 * Incremental Server-Sent Events framer.
 *
 * The previous SSE reader declared `dataLines` inside the `reader.read()` loop,
 * so any event whose bytes spanned two network chunks was silently dropped
 * (random missing tokens, lost `done`, premature stream end). This module frames
 * SSE correctly: a buffer + the current event/data accumulator live ACROSS
 * chunks, and a frame is emitted only on a blank line.
 *
 * Two layers:
 *  - `SSEFrameDecoder`: pure, synchronous, testable. Feed it decoded text chunks
 *    (cut anywhere); it returns the frames that completed within each push, and
 *    a `flush()` for EOF.
 *  - `parseSSEStream`: async generator over a `ReadableStream<Uint8Array>`,
 *    handling `TextDecoder` tail-flush, abort (not treated as a network error),
 *    and reader release.
 */

export interface SSEFrame {
  /** The `event:` field, or "" when not set (the default SSE event type). */
  event: string;
  /** All `data:` lines joined with "\n" (per the SSE spec). */
  data: string;
  /**
   * The `id:` field when present (used by the durable run-event stream to
   * carry the event sequence for `Last-Event-ID` cursor resume). Undefined
   * when the frame had no `id:` line — kept absent so it stays invisible to
   * `toEqual` comparisons in existing tests.
   */
  id?: string;
}

/**
 * Decode an arbitrary stream of text chunks into complete SSE frames.
 *
 * Rules implemented:
 *  - state (`buffer`, `event`, `dataLines`) persists across `push()` calls;
 *  - lines may end with `\n` or `\r\n`;
 *  - a blank line emits the accumulated frame and resets state;
 *  - multiple complete events may arrive in one chunk;
 *  - multiple `data:` lines are joined with "\n";
 *  - leading `:` lines are comments/heartbeats and are ignored;
 *  - a single space after the `:` is stripped (SSE spec);
 *  - `flush()` emits any pending frame at EOF (compat for streams that omit the
 *    final blank line).
 */
export class SSEFrameDecoder {
  private buffer = "";
  private event = "";
  private dataLines: string[] = [];
  private id: string | undefined = undefined;

  /** Feed a decoded text chunk; returns frames completed within it. */
  push(chunk: string): SSEFrame[] {
    this.buffer += chunk;
    const out: SSEFrame[] = [];
    let nl = this.buffer.indexOf("\n");
    while (nl !== -1) {
      const rawLine = this.buffer.slice(0, nl);
      this.buffer = this.buffer.slice(nl + 1);
      const line = rawLine.endsWith("\r") ? rawLine.slice(0, -1) : rawLine;
      const frame = this.processLine(line);
      if (frame) out.push(frame);
      nl = this.buffer.indexOf("\n");
    }
    return out;
  }

  /** Emit any pending frame at EOF. Returns null if nothing accumulated. */
  flush(): SSEFrame | null {
    // A trailing line without a final newline (e.g. "data: x") still counts.
    if (this.buffer !== "") {
      const line = this.buffer.endsWith("\r") ? this.buffer.slice(0, -1) : this.buffer;
      this.buffer = "";
      const frame = this.processLine(line);
      if (frame) return frame;
    }
    return this.emit();
  }

  private processLine(line: string): SSEFrame | null {
    if (line === "") {
      // Blank line = frame boundary.
      return this.emit();
    }
    if (line.startsWith(":")) {
      // Comment / heartbeat.
      return null;
    }
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    const value = colon === -1 ? "" : line.slice(colon + 1).replace(/^ /, "");
    if (field === "event") {
      this.event = value;
    } else if (field === "data") {
      this.dataLines.push(value);
    } else if (field === "id") {
      // Per the SSE spec, the `id:` field is the last event ID; the browser
      // echoes it back as `Last-Event-ID` on reconnect. Empty value clears it.
      this.id = value || undefined;
    }
    // retry / unknown fields are ignored.
    return null;
  }

  private emit(): SSEFrame | null {
    if (this.dataLines.length === 0 && this.event === "") {
      return null; // nothing accumulated (plain separator line)
    }
    const frame: SSEFrame = {
      event: this.event,
      data: this.dataLines.join("\n"),
      // `id` is captured per-frame: a frame without an `id:` line has none.
      // (The durable run-event endpoint stamps `id:` on every frame; the chat
      // stream never uses `id:`, so this stays out of its frames entirely.)
      ...(this.id !== undefined ? { id: this.id } : {}),
    };
    this.event = "";
    this.dataLines = [];
    this.id = undefined;
    return frame;
  }
}

/** True for an AbortError (browser/fetch) or an aborted signal. */
export function isAbortError(err: unknown, signal?: AbortSignal): boolean {
  if (signal?.aborted) return true;
  if (err instanceof DOMException && err.name === "AbortError") return true;
  if (err instanceof Error && err.name === "AbortError") return true;
  return false;
}

/**
 * Parse a `ReadableStream<Uint8Array>` (a fetch `body`) into SSE frames.
 *
 * Abort (user stop) returns cleanly instead of throwing — callers must not treat
 * a stop as a network failure. The reader is released in `finally`.
 */
export async function* parseSSEStream(
  body: ReadableStream<Uint8Array>,
  signal?: AbortSignal
): AsyncGenerator<SSEFrame> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  const framer = new SSEFrameDecoder();
  try {
    while (true) {
      let result: ReadableStreamReadResult<Uint8Array>;
      try {
        result = await reader.read();
      } catch (err) {
        if (isAbortError(err, signal)) return;
        throw err;
      }
      if (result.done) {
        // Flush the TextDecoder tail, then any pending frame.
        const tail = decoder.decode();
        if (tail) {
          for (const frame of framer.push(tail)) yield frame;
        }
        const last = framer.flush();
        if (last) yield last;
        return;
      }
      const text = decoder.decode(result.value, { stream: true });
      for (const frame of framer.push(text)) yield frame;
    }
  } finally {
    // Cancel the underlying stream so the server/connection is torn down on
    // abort (releaseLock alone detaches the reader but leaves the stream open).
    try {
      await reader.cancel();
    } catch {
      // Already closed / cancelled; ignore.
    }
    try {
      reader.releaseLock();
    } catch {
      // Already released / locked; ignore.
    }
  }
}
