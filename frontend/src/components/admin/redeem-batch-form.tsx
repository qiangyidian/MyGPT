"use client";

// 兑换码批次生成表单（`POST /api/admin/redeem-batches`）。
//
// 明文码在这个界面上只有一次生命：后端只保存 peppered HMAC-SHA256
// （`backend/app/models/redeem_code.py:1-15`），创建响应返回的 `codes`
// （`backend/app/schemas/credit.py:66-69`）之后系统里就不存在明文了。
// 所以这里刻意做了三件事：
//   1. 明文只留在组件内存 state —— 不写 localStorage、不进 URL query、
//      不塞进 react-query 缓存之外的任何持久化位置；
//   2. 复制 / 下载入口放在明文旁边，关掉前要求确认已保存；
//   3. 界面上把「离开后不再可见」写在显眼处，而不是等用户丢了码再说。

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Check, Copy, Download, Loader2 } from "lucide-react";
import { toast } from "sonner";

import { api } from "@/lib/api";
import { userErrorMessage } from "@/lib/api-error";
import { formatCreditsRaw } from "@/lib/credits";
import { saveBlobToDisk } from "@/lib/download";
import {
  EMPTY_REDEEM_BATCH_FORM,
  REDEEM_BATCH_LIMITS,
  buildRedeemCodesCsv,
  formatDateText,
  formatRedeemBatchPreview,
  hasRedeemBatchFormErrors,
  redeemBatchRequest,
  validateRedeemBatchForm,
  type RedeemBatchForm,
  type RedeemBatchFormErrors,
} from "@/lib/redeem-batch";
import type { RedeemBatchCreateResult } from "@/lib/types";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";

interface IssuedBatch {
  batchId: string;
  batchName: string;
  creditsPerCode: number;
  expiresText: string;
  codes: string[];
}

export interface RedeemBatchFormProps {
  /** 创建成功后回抛，页面用它把新批次标出来 / 刷新批次列表。 */
  onCreated?: (result: RedeemBatchCreateResult) => void;
}

const FIELDS: {
  key: keyof RedeemBatchForm;
  label: string;
  hint?: string;
}[] = [
  { key: "name", label: "批次名称", hint: `必填，最多 ${REDEEM_BATCH_LIMITS.nameMax} 个字符` },
  {
    key: "credits_per_code",
    label: "每码积分",
    hint: "用户兑换成功后到账的积分数，必须是正整数",
  },
  {
    key: "count",
    label: "生成数量",
    // 上限与后端同源：app/credits.py:47 的 max_codes_per_batch = 5000，
    // 服务层在 app/services/redeem_service.py:75-79 拒绝超量。
    hint: `1 到 ${REDEEM_BATCH_LIMITS.countMax} 张，超出会被服务端拒绝`,
  },
  {
    key: "expires_at",
    label: "有效期（留空为永久）",
    hint: "到期当天 23:59 之后这批码就兑不了",
  },
];

export function RedeemBatchForm({ onCreated }: RedeemBatchFormProps) {
  const qc = useQueryClient();
  const [form, setForm] = useState<RedeemBatchForm>(EMPTY_REDEEM_BATCH_FORM);
  const [errors, setErrors] = useState<RedeemBatchFormErrors>({});
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [issued, setIssued] = useState<IssuedBatch | null>(null);
  const [exported, setExported] = useState(false);
  const [ack, setAck] = useState(false);
  const [actionNote, setActionNote] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const createMut = useMutation({
    mutationFn: () => api.adminCreateRedeemBatch(redeemBatchRequest(form)),
    onSuccess: (result) => {
      setSubmitError(null);
      setIssued({
        batchId: result.batch.id,
        batchName: result.batch.name,
        creditsPerCode: result.batch.credits_per_code,
        expiresText: formatDateText(result.batch.expires_at),
        codes: result.codes,
      });
      setExported(false);
      setAck(false);
      setActionNote(null);
      setActionError(null);
      setForm(EMPTY_REDEEM_BATCH_FORM);
      setErrors({});
      qc.invalidateQueries({ queryKey: ["admin-redeem-batches"] });
      qc.invalidateQueries({ queryKey: ["admin-redeem-codes"] });
      toast.success(`已生成 ${result.codes.length} 个兑换码`);
      onCreated?.(result);
    },
    onError: (err) => {
      // 就地渲染：后端给的就是中文（如「单批最多生成 5000 个兑换码」）。
      setSubmitError(userErrorMessage(err));
    },
  });

  const setField = (key: keyof RedeemBatchForm, value: string) => {
    setForm((prev) => ({ ...prev, [key]: value }));
    setErrors((prev) => {
      if (!prev[key]) return prev;
      const next = { ...prev };
      delete next[key];
      return next;
    });
  };

  const submit = () => {
    const nextErrors = validateRedeemBatchForm(form);
    setErrors(nextErrors);
    setSubmitError(null);
    if (hasRedeemBatchFormErrors(nextErrors)) return;
    if (createMut.isPending) return;
    createMut.mutate();
  };

  const copyAll = async () => {
    const codes = issued?.codes ?? [];
    try {
      await navigator.clipboard.writeText(codes.join("\n"));
      setExported(true);
      setActionNote(`已复制 ${codes.length} 张到剪贴板`);
      setActionError(null);
    } catch {
      setActionError("浏览器拒绝了剪贴板访问，请改用「下载 CSV」。");
    }
  };

  const downloadCsv = () => {
    if (!issued) return;
    const csv = buildRedeemCodesCsv(issued.codes, {
      batchName: issued.batchName,
      creditsPerCode: issued.creditsPerCode,
      expiresText: issued.expiresText,
    });
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
    saveBlobToDisk(blob, `兑换码-${issued.batchName}-${issued.codes.length}张.csv`);
    setExported(true);
    setActionNote("CSV 已导出，请确认文件已在手上");
    setActionError(null);
  };

  const preview = formatRedeemBatchPreview(form);

  return (
    <Card>
      <CardHeader>
        <CardTitle>生成兑换码批次</CardTitle>
        <CardDescription>
          系统只保存兑换码的哈希，明文仅在生成成功这一次返回。生成后请立即复制或下载
          CSV —— 关闭下面的明文面板、刷新或离开本页之后，这批码再也无法在任何地方看到。
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-3 sm:grid-cols-2">
          {FIELDS.map((field) => (
            <div key={field.key} className={field.key === "name" ? "sm:col-span-2 space-y-1" : "space-y-1"}>
              <Label htmlFor={`redeem-${field.key}`} className="text-xs">
                {field.label}
              </Label>
              <Input
                id={`redeem-${field.key}`}
                value={form[field.key]}
                type={
                  field.key === "count" || field.key === "credits_per_code"
                    ? "number"
                    : field.key === "expires_at"
                      ? "date"
                      : "text"
                }
                min={field.key === "credits_per_code" ? REDEEM_BATCH_LIMITS.creditsMin : undefined}
                step={field.key === "count" || field.key === "credits_per_code" ? 1 : undefined}
                maxLength={field.key === "name" ? REDEEM_BATCH_LIMITS.nameMax : undefined}
                placeholder={field.key === "name" ? "2026 中秋活动" : undefined}
                onChange={(e) => setField(field.key, e.target.value)}
                aria-invalid={errors[field.key] ? true : undefined}
              />
              {errors[field.key] ? (
                <p className="text-xs text-destructive">{errors[field.key]}</p>
              ) : field.hint ? (
                <p className="text-xs text-muted-foreground">{field.hint}</p>
              ) : null}
            </div>
          ))}

          <div className="space-y-1 sm:col-span-2">
            <Label htmlFor="redeem-note" className="text-xs">
              备注
            </Label>
            <Textarea
              id="redeem-note"
              value={form.note}
              rows={2}
              placeholder="发给哪个渠道、活动规则等（选填）"
              onChange={(e) => setField("note", e.target.value)}
            />
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-3">
          <Button size="sm" onClick={submit} disabled={createMut.isPending}>
            {createMut.isPending ? (
              <>
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                生成中…
              </>
            ) : (
              "生成兑换码"
            )}
          </Button>
          {preview ? (
            <span className="text-xs text-muted-foreground tabular-nums">{preview}</span>
          ) : null}
        </div>

        {submitError ? (
          <p className="flex items-start gap-2 rounded-md border border-destructive/40 bg-destructive/10 p-2.5 text-sm text-destructive">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            <span>生成失败：{submitError}</span>
          </p>
        ) : null}

        {issued ? (
          <div className="space-y-3 rounded-lg border border-destructive/40 bg-destructive/5 p-3">
            <div className="space-y-1">
              <p className="flex items-center gap-2 text-sm font-medium">
                <AlertTriangle className="h-4 w-4 shrink-0 text-destructive" />
                批次「{issued.batchName}」的 {issued.codes.length} 张明文码
              </p>
              <p className="text-xs text-muted-foreground tabular-nums">
                每张 {formatCreditsRaw(issued.creditsPerCode)} 积分 ·{" "}
                {issued.expiresText === "永久"
                  ? "永久有效"
                  : `有效期至 ${issued.expiresText}`}{" "}
                · 合计 {formatCreditsRaw(issued.creditsPerCode * issued.codes.length)} 积分
              </p>
            </div>

            <p className="rounded-md border border-destructive/40 bg-destructive/10 p-2 text-xs text-destructive">
              明文只在这一次可见：关闭本面板或离开本页后<strong>无法再次查看或导出</strong>，
              只能整批作废后重新生成。请先复制或下载，再点「我已保存」。
            </p>

            <pre className="max-h-60 overflow-auto rounded-md border border-border bg-secondary/40 p-3 font-mono text-xs">
              {issued.codes.join("\n")}
            </pre>

            <div className="flex flex-wrap items-center gap-2">
              <Button variant="outline" size="sm" className="gap-1.5" onClick={() => void copyAll()}>
                <Copy className="h-3.5 w-3.5" />
                复制全部
              </Button>
              <Button variant="outline" size="sm" className="gap-1.5" onClick={downloadCsv}>
                <Download className="h-3.5 w-3.5" />
                下载 CSV
              </Button>
              <label className="ml-auto flex items-center gap-2 text-xs text-muted-foreground">
                <input
                  type="checkbox"
                  checked={ack}
                  onChange={(e) => setAck(e.target.checked)}
                  className="h-3.5 w-3.5"
                />
                我已另行保存这批码
              </label>
            </div>

            {actionNote ? <p className="text-xs text-muted-foreground">{actionNote}</p> : null}
            {actionError ? <p className="text-xs text-destructive">{actionError}</p> : null}

            <div className="flex items-center justify-end gap-2">
              <span className="mr-auto text-xs text-muted-foreground">
                {exported || ack ? "" : "请先复制或下载 CSV，或勾选上方的「我已另行保存这批码」"}
              </span>
              <Button
                variant="secondary"
                size="sm"
                className="gap-1.5"
                disabled={!exported && !ack}
                onClick={() => setIssued(null)}
              >
                <Check className="h-3.5 w-3.5" />
                我已保存，关闭明文
              </Button>
            </div>
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}
