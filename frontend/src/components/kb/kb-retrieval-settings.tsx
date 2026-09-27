"use client";

/** 每库检索/切分参数。留空 = 跟随平台默认（后端把 null 当继承，不是关闭）。 */
import { useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Save } from "lucide-react";

import { api } from "@/lib/api";
import { userErrorMessage } from "@/lib/api-error";
import {
  retrievalFormFromSettings,
  retrievalPatch,
  validateRetrievalForm,
  type RetrievalForm,
} from "@/lib/kb-retrieval";
import type { KnowledgeBase } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

const FIELDS: {
  key: keyof RetrievalForm;
  label: string;
  placeholder: string;
}[] = [
  { key: "top_k", label: "召回条数", placeholder: "如 6" },
  {
    key: "score_threshold",
    label: "相似度阈值",
    placeholder: "0 到 1，如 0.25",
  },
  { key: "chunk_size", label: "切块长度", placeholder: "如 800" },
  { key: "chunk_overlap", label: "切块重叠", placeholder: "如 120" },
];

export function KbRetrievalSettings({ kb }: { kb: KnowledgeBase }) {
  const qc = useQueryClient();
  const [form, setForm] = useState<RetrievalForm>(() =>
    retrievalFormFromSettings(kb),
  );
  // 只有服务端的值真的变了才覆盖本地输入（保存后的回填、或别处改了同一个库）。
  const [synced, setSynced] = useState(() => JSON.stringify(kb));
  useEffect(() => {
    const next = JSON.stringify(kb);
    if (next !== synced) {
      setSynced(next);
      setForm(retrievalFormFromSettings(kb));
    }
  }, [kb, synced]);

  const errors = validateRetrievalForm(form);
  const hasErrors = Object.keys(errors).length > 0;
  const dirty = retrievalPatch(form, kb) !== null;

  const saveMut = useMutation({
    mutationFn: () =>
      api.updateKnowledgeBase(kb.id, retrievalPatch(form, kb) ?? {}),
    onSuccess: () => {
      toast.success("检索参数已更新");
      qc.invalidateQueries({ queryKey: ["kb", kb.id] });
      qc.invalidateQueries({ queryKey: ["knowledge-bases"] });
    },
    onError: (e: unknown) =>
      toast.error("保存失败", { description: userErrorMessage(e) }),
  });

  return (
    <div className="space-y-3 rounded-lg border border-border p-4">
      <div>
        <h2 className="text-sm font-semibold">检索设置</h2>
        <p className="mt-0.5 text-xs text-muted-foreground">
          留空表示跟随平台默认。切块长度/重叠只对之后索引（或重新向量化）的文档生效。
        </p>
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        {FIELDS.map((f) => (
          <div key={f.key} className="space-y-1">
            <Label htmlFor={`kb-${f.key}`} className="text-xs">
              {f.label}
            </Label>
            <Input
              id={`kb-${f.key}`}
              value={form[f.key] as string}
              placeholder={f.placeholder}
              onChange={(e) =>
                setForm((prev) => ({ ...prev, [f.key]: e.target.value }))
              }
            />
            {errors[f.key] && (
              <div className="text-xs text-destructive">{errors[f.key]}</div>
            )}
          </div>
        ))}
        <div className="space-y-1">
          <Label htmlFor="kb-rerank" className="text-xs">
            重排（Rerank）
          </Label>
          <Select
            value={form.rerank_enabled}
            onValueChange={(v) =>
              setForm((prev) => ({
                ...prev,
                rerank_enabled: v as RetrievalForm["rerank_enabled"],
              }))
            }
          >
            <SelectTrigger id="kb-rerank" className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="inherit">跟随平台默认</SelectItem>
              <SelectItem value="on">本库启用</SelectItem>
              <SelectItem value="off">本库关闭</SelectItem>
            </SelectContent>
          </Select>
          <p className="text-xs text-muted-foreground">
            跨库检索时，任一库启用即生效。
          </p>
        </div>
      </div>

      <div className="flex items-center gap-2">
        <Button
          size="sm"
          className="gap-2"
          disabled={hasErrors || !dirty || saveMut.isPending}
          onClick={() => saveMut.mutate()}
        >
          <Save className="h-4 w-4" /> {saveMut.isPending ? "保存中…" : "保存"}
        </Button>
        {hasErrors && (
          <span className="text-xs text-destructive">请先修正上面的参数</span>
        )}
      </div>
    </div>
  );
}
