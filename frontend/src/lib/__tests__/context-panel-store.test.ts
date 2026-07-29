import { describe, it, expect, beforeEach } from "vitest";
import { useContextPanelStore } from "@/stores/context-panel-store";

describe("context-panel-store", () => {
  beforeEach(() => {
    useContextPanelStore.setState({
      open: false,
      tab: "execution",
      focusSourceIndex: null,
      focusAttachmentId: null,
      suppressedRunIds: new Set(),
      sources: [],
    });
  });

  it("opens a tab with focus hints", () => {
    useContextPanelStore.getState().openWith("sources", { sourceIndex: 2 });
    const s = useContextPanelStore.getState();
    expect(s.open).toBe(true);
    expect(s.tab).toBe("sources");
    expect(s.focusSourceIndex).toBe(2);
  });

  it("suppresses a run on manual close and lifts the suppression on a new task", () => {
    useContextPanelStore.getState().openWith("execution");
    useContextPanelStore.getState().close("run-1");
    expect(useContextPanelStore.getState().isSuppressed("run-1")).toBe(true);
    expect(useContextPanelStore.getState().open).toBe(false);

    // A new task clears the suppression so the panel can auto-open again.
    useContextPanelStore.getState().resetForNewTask();
    expect(useContextPanelStore.getState().isSuppressed("run-1")).toBe(false);
  });

  it("mirrors sources for the Sources tab", () => {
    useContextPanelStore.getState().setSources([
      { document_id: "d1", document_name: "Doc", chunk_id: null, chunk_index: 0, snippet: "", score: 0.8 },
    ]);
    expect(useContextPanelStore.getState().sources).toHaveLength(1);
  });
});
