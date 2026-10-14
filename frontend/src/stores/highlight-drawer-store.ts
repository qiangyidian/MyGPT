import { create } from "zustand";

/**
 * 原文定位抽屉的开关状态。
 *
 * 只存"看哪个 chunk"这三样，**不存 locator 本身** —— 抽屉每次打开都重新取一次，
 * 否则上一次的位置会残留在新一次点击上（改了代码删掉 chunk 之后，用户会看到
 * 一个指向旧位置的框而没有任何提示）。
 */
interface HighlightDrawerState {
  open: boolean;
  documentId: string | null;
  chunkId: string | null;
  title: string;
  show: (documentId: string, chunkId: string, title: string) => void;
  close: () => void;
}

export const useHighlightDrawerStore = create<HighlightDrawerState>((set) => ({
  open: false,
  documentId: null,
  chunkId: null,
  title: "",
  show: (documentId, chunkId, title) =>
    set({ open: true, documentId, chunkId, title }),
  close: () => set({ open: false, documentId: null, chunkId: null, title: "" }),
}));
