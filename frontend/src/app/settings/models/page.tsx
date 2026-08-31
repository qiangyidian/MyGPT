"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Plus, Pencil, Trash2, FlaskConical, Zap, Bot } from "lucide-react";

import { api, ApiError } from "@/lib/api";
import type { ModelConfig, ModelConfigInput } from "@/lib/types";
import {
  parseOptionalNumber,
  parseOptionalPositiveInteger,
  normalizeParallelTools,
  validateModelConfigNumbers,
} from "@/lib/model-config-validation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

const QUERY_KEY = ["models"] as const;

const EMPTY: ModelConfigInput = {
  name: "",
  provider: "openai-compatible",
  api_base_url: "https://api.openai.com/v1",
  api_key: "",
  model_name: "",
  embedding_model_name: "",
  supports_stream: true,
  supports_tools: false,
  supports_parallel_tools: false,
  supports_vision: false,
  supports_audio_input: false,
  supports_audio_output: false,
  supports_image_generation: false,
  supports_structured_output: false,
  supports_reasoning_effort: false,
  output_token_parameter: "max_tokens",
  is_embedding: false,
  temperature: 0.7,
  top_p: 1,
  // A code-capable chat model needs headroom for long answers; a small
  // max_tokens shows the "输出达到长度上限" truncation banner on long agent runs.
  max_tokens: 8192,
  max_context_tokens: 131072,
};

export default function ModelsPage() {
  const qc = useQueryClient();
  const { data: models = [], isLoading } = useQuery({
    queryKey: QUERY_KEY,
    queryFn: api.listModels,
  });

  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState<ModelConfig | null>(null);
  const [form, setForm] = useState<ModelConfigInput>(EMPTY);

  const invalidate = () => qc.invalidateQueries({ queryKey: QUERY_KEY });

  const createMut = useMutation({
    mutationFn: (body: ModelConfigInput) => api.createModel(body),
    onSuccess: () => {
      toast.success("模型已创建");
      invalidate();
      setOpen(false);
    },
    onError: (e: ApiError) => toast.error(e.message),
  });

  const updateMut = useMutation({
    mutationFn: ({ id, body }: { id: string; body: ModelConfigInput }) =>
      api.updateModel(id, body),
    onSuccess: () => {
      toast.success("已保存");
      invalidate();
      setOpen(false);
    },
    onError: (e: ApiError) => toast.error(e.message),
  });

  const deleteMut = useMutation({
    mutationFn: (id: string) => api.deleteModel(id),
    onSuccess: () => {
      toast.success("已删除");
      invalidate();
    },
    onError: (e: ApiError) => toast.error(e.message),
  });

  const testMut = useMutation({
    mutationFn: (id: string) => api.testModel(id),
    onSuccess: (res) => {
      if (res.ok) toast.success(`连接成功 · ${res.latency_ms}ms`);
      else toast.error(`连接失败：${res.error ?? "未知错误"}`);
    },
    onError: (e: ApiError) => toast.error(e.message),
  });

  function openCreate() {
    setEditing(null);
    setForm(EMPTY);
    setOpen(true);
  }

  function openEdit(m: ModelConfig) {
    setEditing(m);
    setForm({
      name: m.name,
      provider: m.provider,
      api_base_url: m.api_base_url,
      api_key: "", // write-only; leave blank to keep existing
      model_name: m.model_name,
      embedding_model_name: m.embedding_model_name ?? "",
      supports_stream: m.supports_stream,
      supports_tools: m.supports_tools,
      supports_parallel_tools: normalizeParallelTools(
        m.supports_tools,
        m.supports_parallel_tools,
      ),
      supports_vision: m.supports_vision,
      supports_audio_input: m.supports_audio_input,
      supports_audio_output: m.supports_audio_output,
      supports_image_generation: m.supports_image_generation,
      supports_structured_output: m.supports_structured_output,
      supports_reasoning_effort: m.supports_reasoning_effort,
      output_token_parameter: m.output_token_parameter,
      is_embedding: m.is_embedding,
      temperature: m.temperature,
      top_p: m.top_p,
      max_tokens: m.max_tokens,
      max_context_tokens: m.max_context_tokens,
    });
    setOpen(true);
  }

  function submit() {
    if (!form.name.trim() || !form.model_name.trim() || !form.api_base_url.trim()) {
      toast.error("请填写名称、Base URL 和模型名");
      return;
    }
    const numericError = validateModelConfigNumbers(form);
    if (numericError) {
      toast.error(numericError);
      return;
    }
    const normalized = {
      ...form,
      supports_parallel_tools: normalizeParallelTools(
        !!form.supports_tools,
        !!form.supports_parallel_tools,
      ),
    };
    if (editing) updateMut.mutate({ id: editing.id, body: normalized });
    else createMut.mutate(normalized);
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">模型配置</h1>
          <p className="text-sm text-muted-foreground">
            管理可用的对话 / 向量模型。API Key 加密存储，仅显示掩码。
          </p>
        </div>
        <Button onClick={openCreate} className="gap-2">
          <Plus className="h-4 w-4" /> 新建模型
        </Button>
      </div>

      {isLoading ? (
        <p className="text-sm text-muted-foreground">加载中…</p>
      ) : models.length === 0 ? (
        <p className="text-sm text-muted-foreground">还没有模型，点击右上角新建。</p>
      ) : (
        <div className="grid gap-3">
          {models.map((m) => (
            <div
              key={m.id}
              className="flex flex-wrap items-center gap-3 rounded-lg border border-border bg-card p-4"
            >
              <div className="flex h-9 w-9 items-center justify-center rounded-md bg-secondary">
                <Bot className="h-4 w-4" />
              </div>
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="font-medium">{m.name}</span>
                  {m.user_id === null && <Badge variant="secondary">系统</Badge>}
                  {m.is_embedding ? (
                    <Badge variant="outline">向量</Badge>
                  ) : (
                    <Badge variant="outline">对话</Badge>
                  )}
                  {m.supports_stream && (
                    <Badge variant="outline" className="gap-1">
                      <Zap className="h-3 w-3" /> 流式
                    </Badge>
                  )}
                  {m.supports_tools && <Badge variant="outline">工具</Badge>}
                  {m.supports_vision && <Badge variant="outline">视觉</Badge>}
                  {m.supports_structured_output && <Badge variant="outline">结构化输出</Badge>}
                </div>
                <div className="mt-0.5 truncate text-xs text-muted-foreground">
                  {m.provider} · {m.model_name} · {m.api_base_url}
                </div>
                <div className="text-xs text-muted-foreground">
                  Key: {m.has_key ? m.api_key_masked : "未设置"}
                </div>
              </div>
              <div className="flex items-center gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  className="gap-1"
                  disabled={testMut.isPending}
                  onClick={() => testMut.mutate(m.id)}
                >
                  <FlaskConical className="h-3.5 w-3.5" /> 测试
                </Button>
                <Button variant="ghost" size="sm" onClick={() => openEdit(m)}>
                  <Pencil className="h-3.5 w-3.5" />
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => {
                    if (confirm(`删除模型「${m.name}」？`)) deleteMut.mutate(m.id);
                  }}
                >
                  <Trash2 className="h-3.5 w-3.5 text-destructive" />
                </Button>
              </div>
            </div>
          ))}
        </div>
      )}

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogTrigger className="hidden" />
        <DialogContent className="max-h-[90vh] max-w-lg overflow-y-auto">
          <DialogHeader>
            <DialogTitle>{editing ? "编辑模型" : "新建模型"}</DialogTitle>
          </DialogHeader>
          <div className="grid gap-4 py-2">
            <Field label="名称">
              <Input
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                placeholder="例如：GPT-4o"
              />
            </Field>
            <div className="grid grid-cols-2 gap-4">
              <Field label="Provider">
                <Select
                  value={form.provider}
                  onValueChange={(v) => {
                    // 切换 provider 时带入各家默认端点，减少手填出错。
                    const defaults: Record<string, { url: string; model: string }> = {
                      "openai-compatible": { url: "https://api.openai.com/v1", model: "gpt-4o" },
                      anthropic: { url: "https://api.anthropic.com", model: "claude-sonnet-4-6" },
                      mock: { url: "", model: "mock-model" },
                    };
                    const d = defaults[v];
                    setForm({
                      ...form,
                      provider: v,
                      api_base_url: d?.url ?? form.api_base_url,
                      model_name: form.model_name || d?.model || "",
                    });
                  }}
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="openai-compatible">openai-compatible</SelectItem>
                    <SelectItem value="anthropic">anthropic (Claude 原生)</SelectItem>
                    <SelectItem value="mock">mock</SelectItem>
                  </SelectContent>
                </Select>
              </Field>
              <Field label="模型名">
                <Input
                  value={form.model_name}
                  onChange={(e) => setForm({ ...form, model_name: e.target.value })}
                  placeholder="例如：gpt-4o"
                />
              </Field>
            </div>
            <Field label="API Base URL">
              <Input
                value={form.api_base_url}
                onChange={(e) => setForm({ ...form, api_base_url: e.target.value })}
                placeholder={
                  form.provider === "anthropic"
                    ? "https://api.anthropic.com"
                    : "https://api.openai.com/v1"
                }
              />
            </Field>
            <Field
              label={
                editing
                  ? `API Key（留空保留现有：${editing.api_key_masked}）`
                  : "API Key"
              }
            >
              <Input
                type="password"
                value={form.api_key ?? ""}
                onChange={(e) => setForm({ ...form, api_key: e.target.value })}
                placeholder="sk-..."
              />
            </Field>
            <Field label="Embedding 模型名（可选）">
              <Input
                value={form.embedding_model_name ?? ""}
                onChange={(e) =>
                  setForm({ ...form, embedding_model_name: e.target.value })
                }
                placeholder="例如：text-embedding-3-small"
              />
            </Field>
            <div className="grid grid-cols-2 gap-4">
              <Field label="temperature">
                <Input
                  type="number"
                  step="0.1"
                  value={form.temperature ?? ""}
                  onChange={(e) =>
                    setForm({ ...form, temperature: parseOptionalNumber(e.target.value) })
                  }
                />
              </Field>
              <Field label="top_p">
                <Input
                  type="number"
                  step="0.05"
                  value={form.top_p ?? ""}
                  onChange={(e) =>
                    setForm({ ...form, top_p: parseOptionalNumber(e.target.value) })
                  }
                />
              </Field>
              <Field label="上下文 Token 上限">
                <Input
                  type="number"
                  min={1}
                  value={form.max_context_tokens ?? ""}
                  onChange={(e) =>
                    setForm({
                      ...form,
                      max_context_tokens: parseOptionalPositiveInteger(e.target.value),
                    })
                  }
                />
              </Field>
              <Field label="max_tokens">
                <Input
                  type="number"
                  min={1}
                  value={form.max_tokens ?? ""}
                  onChange={(e) =>
                    setForm({
                      ...form,
                      max_tokens: parseOptionalPositiveInteger(e.target.value),
                    })
                  }
                />
              </Field>
            </div>
            <Field label="输出 Token 参数">
              <Select
                value={form.output_token_parameter ?? "max_tokens"}
                onValueChange={(v: "max_tokens" | "max_completion_tokens") =>
                  setForm({ ...form, output_token_parameter: v })
                }
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="max_tokens">max_tokens</SelectItem>
                  <SelectItem value="max_completion_tokens">
                    max_completion_tokens
                  </SelectItem>
                </SelectContent>
              </Select>
            </Field>
            <div className="flex flex-wrap gap-6 pt-1">
              <Toggle
                label="流式输出"
                checked={!!form.supports_stream}
                onChange={(v) => setForm({ ...form, supports_stream: v })}
              />
              <Toggle
                label="支持工具调用"
                checked={!!form.supports_tools}
                onChange={(v) => setForm({
                  ...form,
                  supports_tools: v,
                  supports_parallel_tools: normalizeParallelTools(
                    v,
                    !!form.supports_parallel_tools,
                  ),
                })}
              />
              <Toggle
                label="支持并行工具"
                checked={!!form.supports_parallel_tools}
                onChange={(v) => setForm({ ...form, supports_parallel_tools: v })}
                disabled={!form.supports_tools}
              />
              <Toggle
                label="支持视觉（图片输入）"
                checked={!!form.supports_vision}
                onChange={(v) => setForm({ ...form, supports_vision: v })}
              />
              <Toggle
                label="支持音频输入"
                checked={!!form.supports_audio_input}
                onChange={(v) => setForm({ ...form, supports_audio_input: v })}
              />
              <Toggle
                label="支持音频输出"
                checked={!!form.supports_audio_output}
                onChange={(v) => setForm({ ...form, supports_audio_output: v })}
              />
              <Toggle
                label="支持图像生成"
                checked={!!form.supports_image_generation}
                onChange={(v) => setForm({ ...form, supports_image_generation: v })}
              />
              <Toggle
                label="支持结构化输出"
                checked={!!form.supports_structured_output}
                onChange={(v) => setForm({ ...form, supports_structured_output: v })}
              />
              <Toggle
                label="支持推理强度"
                checked={!!form.supports_reasoning_effort}
                onChange={(v) => setForm({ ...form, supports_reasoning_effort: v })}
              />
              <Toggle
                label="作为向量模型"
                checked={!!form.is_embedding}
                onChange={(v) => setForm({ ...form, is_embedding: v })}
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setOpen(false)}>
              取消
            </Button>
            <Button
              onClick={submit}
              disabled={createMut.isPending || updateMut.isPending}
            >
              {editing ? "保存" : "创建"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid gap-1.5">
      <Label className="text-xs text-muted-foreground">{label}</Label>
      {children}
    </div>
  );
}

function Toggle({
  label,
  checked,
  onChange,
  disabled = false,
}: {
  label: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <label
      className={`flex items-center gap-2 text-sm ${
        disabled ? "cursor-not-allowed opacity-50" : "cursor-pointer"
      }`}
    >
      <Switch checked={checked} onCheckedChange={onChange} disabled={disabled} />
      {label}
    </label>
  );
}
