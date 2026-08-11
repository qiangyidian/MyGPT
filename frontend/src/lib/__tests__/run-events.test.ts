// Pure-reducer unit tests for the durable run-event contract (Task 12).
// No React, no network. Mirrors the agent-graph-reducer test style.

import { describe, expect, it } from "vitest";
import {
  applyEvent,
  disconnectSubscription,
  emptyRunState,
  reduceEvents,
  replayEvents,
  reconnectionShouldStop,
  TERMINAL_RUN_STATUSES,
  type DurableRunEvent,
} from "@/lib/run-events";

/** Build a minimal durable event for the given sequence. */
const event = (
  sequence: number,
  over: Partial<DurableRunEvent> = {},
): DurableRunEvent => ({
  id: `evt-${sequence}`,
  run_id: "r1",
  sequence,
  event_type: "token",
  data: { delta: "x" },
  created_at: "2026-01-01T00:00:00Z",
  ...over,
});

describe("reduceEvents — cursor-aware dedupe", () => {
  it("deduplicates replayed events by run and sequence", () => {
    expect(reduceEvents([event(1), event(2), event(2)])).toHaveLength(2);
  });

  it("keeps the latest copy when a (run, sequence) pair repeats", () => {
    const out = reduceEvents([
      event(1, { data: { delta: "old" } }),
      event(1, { data: { delta: "new" } }),
    ]);
    expect(out).toHaveLength(1);
    expect((out[0].data as { delta: string }).delta).toBe("new");
  });

  it("does NOT dedupe across runs (same sequence, different run_id)", () => {
    const out = reduceEvents([
      event(1, { run_id: "r1" }),
      event(1, { run_id: "r2" }),
    ]);
    expect(out).toHaveLength(2);
  });

  it("returns events in ascending sequence order", () => {
    const out = reduceEvents([event(3), event(1), event(2)]);
    expect(out.map((e) => e.sequence)).toEqual([1, 2, 3]);
  });
});

describe("applyEvent — reducer", () => {
  it("advances the cursor to the event sequence and records the event", () => {
    const s = applyEvent(emptyRunState("r1"), event(5));
    expect(s.cursor).toBe(5);
    expect(s.events).toHaveLength(1);
  });

  it("maps run.started → running", () => {
    const s = applyEvent(
      emptyRunState("r1"),
      event(1, { event_type: "run.started" }),
    );
    expect(s.runStatus).toBe("running");
  });

  it("maps run.completed → completed (terminal)", () => {
    const s = applyEvent(
      emptyRunState("r1"),
      event(1, { event_type: "run.completed" }),
    );
    expect(s.runStatus).toBe("completed");
    expect(TERMINAL_RUN_STATUSES.has(s.runStatus)).toBe(true);
  });

  it("maps run.cancelled → cancelled", () => {
    const s = applyEvent(
      emptyRunState("r1"),
      event(1, { event_type: "run.cancelled" }),
    );
    expect(s.runStatus).toBe("cancelled");
  });

  it("maps run.failed → failed + surfaces the message", () => {
    const s = applyEvent(
      emptyRunState("r1"),
      event(1, {
        event_type: "run.failed",
        data: { message: "boom" },
      }),
    );
    expect(s.runStatus).toBe("failed");
    expect(s.error).toBe("boom");
  });

  it("maps run.paused → paused (workflow state), run.resumed → running", () => {
    const paused = applyEvent(
      emptyRunState("r1"),
      event(1, { event_type: "run.started" }),
    );
    const p = applyEvent(
      paused,
      event(2, { event_type: "run.paused" }),
    );
    expect(p.runStatus).toBe("paused");
    expect(p.paused).toBe(true);
    const r = applyEvent(
      p,
      event(3, { event_type: "run.resumed" }),
    );
    expect(r.runStatus).toBe("running");
    expect(r.paused).toBe(false);
  });

  it("also accepts the chat-stream event names (done/error/run_paused/run_resumed)", () => {
    const s = applyEvent(
      applyEvent(emptyRunState("r1"), event(1, { event_type: "run_started" })),
      event(2, { event_type: "done", data: { finish_reason: "stop" } }),
    );
    expect(s.runStatus).toBe("completed");
    const failed = applyEvent(
      emptyRunState("r1"),
      event(1, { event_type: "error", data: { message: "oops" } }),
    );
    expect(failed.runStatus).toBe("failed");
    expect(failed.error).toBe("oops");
  });

  it("ignores events from a different run_id", () => {
    const s0 = emptyRunState("r1");
    const s = applyEvent(
      s0,
      event(1, { run_id: "r2", event_type: "run.completed" }),
    );
    expect(s.runStatus).toBe(s0.runStatus);
    expect(s.events).toHaveLength(0);
  });

  it("ignores events at or below the cursor (already applied)", () => {
    const s0 = applyEvent(emptyRunState("r1"), event(5)); // cursor 5
    const s = applyEvent(s0, event(3)); // sequence 3 <= 5
    expect(s).toBe(s0);
  });
});

describe("replayEvents — cursor replay", () => {
  it("applies events past the cursor in order", () => {
    const s0 = applyEvent(emptyRunState("r1"), event(1)); // cursor 1
    const s = replayEvents(s0, [
      event(2, { event_type: "token" }),
      event(3, { event_type: "token" }),
    ]);
    expect(s.cursor).toBe(3);
    expect(s.events.map((e) => e.sequence)).toEqual([1, 2, 3]);
  });

  it("drops already-seen events (sequence <= cursor) on replay", () => {
    // cursor 2, events [1,2]
    const s0 = replayEvents(emptyRunState("r1"), [event(1), event(2)]);
    const s = replayEvents(s0, [event(1), event(2), event(3)]);
    expect(s.events.map((e) => e.sequence)).toEqual([1, 2, 3]);
    expect(s.events).toHaveLength(3); // no duplicates
    expect(s.cursor).toBe(3);
  });

  it("dedupes a replay batch that repeats a sequence", () => {
    const s = replayEvents(emptyRunState("r1"), [
      event(1, { data: { v: "a" } }),
      event(1, { data: { v: "b" } }),
    ]);
    expect(s.events).toHaveLength(1);
    expect((s.events[0].data as { v: string }).v).toBe("b");
  });
});

describe("disconnectSubscription — subscription ≠ workflow state", () => {
  it("keeps a run active after the chat SSE subscription closes", () => {
    const runningState = applyEvent(
      emptyRunState("r1"),
      event(1, { event_type: "run.started" }),
    );
    const disconnected = disconnectSubscription(runningState);
    expect(disconnected.runStatus).toBe("running");
    expect(disconnected.subscriptionStatus).toBe("disconnected");
  });

  it("preserves events + cursor on disconnect", () => {
    const s = replayEvents(emptyRunState("r1"), [event(1), event(2)]);
    const disconnected = disconnectSubscription(s);
    expect(disconnected.events).toHaveLength(2);
    expect(disconnected.cursor).toBe(2);
  });

  it("does not touch runStatus even when the run is paused", () => {
    const paused = applyEvent(
      applyEvent(emptyRunState("r1"), event(1, { event_type: "run.started" })),
      event(2, { event_type: "run.paused" }),
    );
    expect(disconnectSubscription(paused).runStatus).toBe("paused");
  });
});

describe("reconnection policy", () => {
  it("stops reconnecting once the run is terminal", () => {
    const completed = applyEvent(
      emptyRunState("r1"),
      event(1, { event_type: "run.completed" }),
    );
    expect(reconnectionShouldStop(completed)).toBe(true);
  });

  it("keeps reconnecting while the run is non-terminal", () => {
    const running = applyEvent(
      emptyRunState("r1"),
      event(1, { event_type: "run.started" }),
    );
    expect(reconnectionShouldStop(running)).toBe(false);
  });
});

describe("TERMINAL_RUN_STATUSES", () => {
  it("treats completed/failed/cancelled as terminal", () => {
    expect(TERMINAL_RUN_STATUSES.has("completed")).toBe(true);
    expect(TERMINAL_RUN_STATUSES.has("failed")).toBe(true);
    expect(TERMINAL_RUN_STATUSES.has("cancelled")).toBe(true);
  });

  it("does NOT treat running/paused/pending as terminal", () => {
    expect(TERMINAL_RUN_STATUSES.has("running")).toBe(false);
    expect(TERMINAL_RUN_STATUSES.has("paused")).toBe(false);
    expect(TERMINAL_RUN_STATUSES.has("pending")).toBe(false);
  });
});
