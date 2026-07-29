import { describe, it, expect, beforeEach } from "vitest";
import { useAttachmentStore } from "@/stores/attachment-store";

describe("attachment-store", () => {
  beforeEach(() => {
    useAttachmentStore.setState({ drafts: {} });
  });

  it("adds, updates, and removes drafts per conversation", () => {
    const s = useAttachmentStore.getState();
    s.addDraft("c1", {
      id: "a1",
      filename: "f.txt",
      mime_type: "text/plain",
      size_bytes: 10,
      status: "uploading",
      parse_status: "pending",
      uploading: true,
    });
    expect(useAttachmentStore.getState().getDrafts("c1")).toHaveLength(1);

    s.updateDraft("c1", "a1", { status: "ready", uploading: false });
    expect(useAttachmentStore.getState().getDrafts("c1")[0].status).toBe("ready");

    s.removeDraft("c1", "a1");
    expect(useAttachmentStore.getState().getDrafts("c1")).toHaveLength(0);
  });

  it("clears drafts for a conversation (e.g. after send)", () => {
    const s = useAttachmentStore.getState();
    s.addDraft("c1", {
      id: "a1", filename: "f", mime_type: "", size_bytes: 0, status: "ready", parse_status: "ready",
    });
    s.clearDrafts("c1");
    expect(useAttachmentStore.getState().getDrafts("c1")).toHaveLength(0);
  });

  it("isolates drafts by conversation", () => {
    const s = useAttachmentStore.getState();
    s.addDraft("c1", { id: "a1", filename: "f", mime_type: "", size_bytes: 0, status: "ready", parse_status: "ready" });
    s.addDraft("c2", { id: "a2", filename: "g", mime_type: "", size_bytes: 0, status: "ready", parse_status: "ready" });
    expect(useAttachmentStore.getState().getDrafts("c1")).toHaveLength(1);
    expect(useAttachmentStore.getState().getDrafts("c2")).toHaveLength(1);
  });
});
