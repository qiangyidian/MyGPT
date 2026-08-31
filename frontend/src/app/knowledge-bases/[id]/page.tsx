"use client";

import { useState } from "react";
import { useParams, useSearchParams } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { RefreshCw, Trash2, Upload, Search, Eye } from "lucide-react";

import { api, ApiError } from "@/lib/api";
import type { Citation, DocFile } from "@/lib/types";
import { formatBytes } from "@/lib/utils";
import { resolveChatHome, withReturnTo } from "@/lib/navigation";
import { NavSuspense } from "@/components/navigation/page-loading";
import { AppPageShell } from "@/components/navigation/app-page-shell";
import { DocumentPreviewDialog } from "@/components/kb/document-preview-dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";

const IN_PROGRESS = new Set(["pending", "parsing", "chunking", "embedding"]);

function statusVariant(s: DocFile["status"]) {
  if (s === "indexed") return "default";
  if (s === "failed") return "destructive";
  if (IN_PROGRESS.has(s)) return "secondary";
  return "outline";
}

export default function KbDetailPage() {
  return (
    <NavSuspense>
      <KbDetailContent />
    </NavSuspense>
  );
}

function KbDetailContent() {
  const params = useParams<{ id: string }>();
  const kbId = params.id;
  const searchParams = useSearchParams();
  // Chat-home target forwarded to the list/breadcrumbs; always clean (preserves
  // the conversation id), never a non-existent path.
  const returnTo = resolveChatHome(searchParams);
  const qc = useQueryClient();

  const { data: kb } = useQuery({
    queryKey: ["kb", kbId],
    queryFn: () => api.getKnowledgeBase(kbId),
  });

  const docsQ = useQuery({
    queryKey: ["kb-docs", kbId],
    queryFn: () => api.listDocuments(kbId),
    refetchInterval: (q) => {
      const docs = (q.state.data as DocFile[] | undefined) ?? [];
      return docs.some((d) => IN_PROGRESS.has(d.status)) ? 3000 : false;
    },
  });
  const docs = docsQ.data ?? [];

  const uploadMut = useMutation({
    mutationFn: (file: File) => api.uploadDocument(kbId, file),
    onSuccess: () => {
      toast.success("已上传，开始解析…");
      qc.invalidateQueries({ queryKey: ["kb-docs", kbId] });
    },
    onError: (e: ApiError) => toast.error(e.message),
  });

  const reindexMut = useMutation({
    mutationFn: (id: string) => api.reindexDocument(id),
    onSuccess: () => {
      toast.success("已开始重新向量化");
      qc.invalidateQueries({ queryKey: ["kb-docs", kbId] });
    },
    onError: (e: ApiError) => toast.error(e.message),
  });

  const deleteMut = useMutation({
    mutationFn: (id: string) => api.deleteDocument(id),
    onSuccess: () => {
      toast.success("已删除");
      qc.invalidateQueries({ queryKey: ["kb-docs", kbId] });
    },
    onError: (e: ApiError) => toast.error(e.message),
  });

  // Retrieval test box
  const [query, setQuery] = useState("");
  const [citations, setCitations] = useState<Citation[] | null>(null);
  const searchMut = useMutation({
    mutationFn: () => api.searchKnowledgeBase(kbId, query),
    onSuccess: (res) => setCitations(res.citations),
    onError: (e: ApiError) => toast.error(e.message),
  });

  // Online document preview
  const [previewId, setPreviewId] = useState<string | null>(null);

  const kbName = kb?.name ?? "知识库";
  const listHref = withReturnTo("/knowledge-bases", returnTo);

  return (
    <AppPageShell
      title={kbName}
      description={kb?.description ?? undefined}
      breadcrumbs={[
        { label: "对话", href: "/" },
        { label: "知识库", href: listHref },
        { label: kbName },
      ]}
      secondaryBack={{ href: listHref, label: "返回知识库" }}
    >
      {/* Upload */}
      <div className="flex flex-wrap items-center gap-3">
        <label>
          <input
            type="file"
            className="hidden"
            accept=".pdf,.docx,.doc,.txt,.md,.csv,.xlsx,.xls"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) uploadMut.mutate(f);
              e.currentTarget.value = "";
            }}
          />
          <Button asChild className="cursor-pointer gap-2" disabled={uploadMut.isPending}>
            <span>
              <Upload className="h-4 w-4" /> 上传文档
            </span>
          </Button>
        </label>
        <span className="text-xs text-muted-foreground">
          支持 PDF / Word / TXT / Markdown / CSV / Excel
        </span>
      </div>

      {/* Document list */}
      <div className="overflow-x-auto rounded-lg border border-border">
        <table className="w-full text-sm">
          <thead className="bg-secondary/50 text-left text-xs text-muted-foreground">
            <tr>
              <th className="p-3">文件名</th>
              <th className="hidden p-3 md:table-cell">类型</th>
              <th className="hidden p-3 sm:table-cell">大小</th>
              <th className="p-3">状态</th>
              <th className="hidden p-3 sm:table-cell">切片</th>
              <th className="p-3 text-right">操作</th>
            </tr>
          </thead>
          <tbody>
            {docs.length === 0 ? (
              <tr>
                <td colSpan={6} className="p-6 text-center text-muted-foreground">
                  还没有文档
                </td>
              </tr>
            ) : (
              docs.map((d) => (
                <tr key={d.id} className="border-t border-border">
                  <td className="p-3">
                    <button
                      type="button"
                      className="max-w-[22rem] truncate text-left font-medium hover:underline"
                      onClick={() => setPreviewId(d.id)}
                      title="点击预览"
                    >
                      {d.filename}
                    </button>
                    {d.error_message && (
                      <div className="text-xs text-destructive">{d.error_message}</div>
                    )}
                  </td>
                  <td className="hidden p-3 text-muted-foreground md:table-cell">{d.file_type}</td>
                  <td className="hidden p-3 text-muted-foreground sm:table-cell">
                    {formatBytes(d.file_size)}
                  </td>
                  <td className="p-3">
                    <Badge variant={statusVariant(d.status)}>{d.status}</Badge>
                  </td>
                  <td className="hidden p-3 text-muted-foreground sm:table-cell">{d.chunk_count}</td>
                  <td className="p-3">
                    <div className="flex justify-end gap-1">
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => setPreviewId(d.id)}
                        aria-label={`预览 ${d.filename}`}
                        title="在线预览"
                      >
                        <Eye className="h-3.5 w-3.5" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => reindexMut.mutate(d.id)}
                        aria-label={`重新向量化 ${d.filename}`}
                        title="重新向量化"
                      >
                        <RefreshCw className="h-3.5 w-3.5" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => {
                          if (window.confirm(`删除「${d.filename}」？`)) deleteMut.mutate(d.id);
                        }}
                        aria-label={`删除 ${d.filename}`}
                      >
                        <Trash2 className="h-3.5 w-3.5 text-destructive" />
                      </Button>
                    </div>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {/* Retrieval test */}
      <div className="space-y-2 rounded-lg border border-border p-4">
        <h2 className="text-sm font-semibold">检索测试</h2>
        <div className="flex flex-col gap-2 sm:flex-row">
          <Input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="输入一个问题，测试检索结果…"
            onKeyDown={(e) => {
              if (e.key === "Enter" && query.trim()) searchMut.mutate();
            }}
          />
          <Button
            className="gap-2 sm:w-auto"
            onClick={() => searchMut.mutate()}
            disabled={searchMut.isPending || !query.trim()}
          >
            <Search className="h-4 w-4" /> 检索
          </Button>
        </div>
        {citations && (
          <div className="space-y-2 pt-2">
            {citations.length === 0 ? (
              <p className="text-sm text-muted-foreground">没有匹配的片段。</p>
            ) : (
              citations.map((c, i) => (
                <div key={i} className="rounded-md bg-secondary/40 p-3 text-sm">
                  <div className="mb-1 text-xs text-muted-foreground">
                    [source {i + 1}] {c.document_name} · score {c.score.toFixed(3)}
                  </div>
                  <div className="line-clamp-3">{c.snippet}</div>
                </div>
              ))
            )}
          </div>
        )}
      </div>

      <DocumentPreviewDialog documentId={previewId} onClose={() => setPreviewId(null)} />
    </AppPageShell>
  );
}
