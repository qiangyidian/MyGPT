"use client";

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Plus, Pencil, Plug, Trash2, RefreshCw, Power, PowerOff } from "lucide-react";

import { connectorsApi, ApiError } from "@/lib/api";
import { cn } from "@/lib/utils";
import type {
  Connector,
  ConnectorCreateInput,
  ProviderManifest,
} from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Badge } from "@/components/ui/badge";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

const QUERY_KEY = ["connectors"] as const;
const PROVIDERS_KEY = ["connector-providers"] as const;

const EMPTY: ConnectorCreateInput = {
  name: "",
  provider: "",
  credentials: {},
  oauth_scopes: [],
  command_or_url: "",
  transport: "",
  enabled: false,
  extra: null,
};

export default function ConnectorsPage() {
  const qc = useQueryClient();
  const { data: connectors = [], isLoading } = useQuery({
    queryKey: QUERY_KEY,
    queryFn: connectorsApi.list,
  });
  const { data: providers = [] } = useQuery({
    queryKey: PROVIDERS_KEY,
    queryFn: connectorsApi.listProviders,
  });

  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState<Connector | null>(null);
  const [form, setForm] = useState<ConnectorCreateInput>(EMPTY);
  const [credentialsJson, setCredentialsJson] = useState("{}");

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: QUERY_KEY });
  };

  const providerByName = useMemo(() => {
    const m = new Map<string, ProviderManifest>();
    for (const p of providers) m.set(p.name, p);
    return m;
  }, [providers]);

  const createMut = useMutation({
    mutationFn: (body: ConnectorCreateInput) => connectorsApi.create(body),
    onSuccess: () => {
      toast.success("连接器已创建");
      invalidate();
      setOpen(false);
    },
    onError: (e: ApiError) => toast.error(e.message),
  });

  const updateMut = useMutation({
    mutationFn: ({ id, body }: { id: string; body: { name: string; oauth_scopes?: string[] } }) =>
      connectorsApi.update(id, body),
    onSuccess: () => {
      toast.success("已保存");
      invalidate();
    },
    onError: (e: ApiError) => toast.error(e.message),
  });

  const toggleMut = useMutation({
    mutationFn: ({ id, enable }: { id: string; enable: boolean }) =>
      enable ? connectorsApi.activate(id) : connectorsApi.deactivate(id),
    onSuccess: () => {
      invalidate();
    },
    onError: (e: ApiError) => toast.error(e.message),
  });

  const deleteMut = useMutation({
    mutationFn: (id: string) => connectorsApi.delete(id),
    onSuccess: () => {
      toast.success("已删除");
      invalidate();
    },
    onError: (e: ApiError) => toast.error(e.message),
  });

  // Credential rotation (B: rotate API now wired): prompts for a new JSON
  // credential blob and swaps it server-side (Fernet-encrypted as usual).
  const rotateMut = useMutation({
    mutationFn: ({ id, credentials }: { id: string; credentials: Record<string, string> }) =>
      connectorsApi.rotate(id, credentials),
    onSuccess: () => {
      toast.success("凭证已轮换");
      invalidate();
    },
    onError: (e: ApiError) => toast.error(e.message),
  });

  function rotateCredentials(c: Connector) {
    const raw = window.prompt(
      `轮换「${c.name}」的凭证 — 输入新的凭证 JSON（如 {"api_key": "..."}）：`,
    );
    if (!raw?.trim()) return;
    try {
      const credentials = JSON.parse(raw);
      if (typeof credentials !== "object" || credentials === null || Array.isArray(credentials)) {
        toast.error("凭证必须是 JSON 对象");
        return;
      }
      rotateMut.mutate({ id: c.id, credentials });
    } catch {
      toast.error("凭证 JSON 解析失败");
    }
  }

  function openCreate() {
    const first = providers[0];
    setEditing(null);
    setForm({
      ...EMPTY,
      provider: first?.name ?? "",
      transport: first?.transport ?? "",
      command_or_url: first?.command_or_url ?? "",
      oauth_scopes: first?.required_scopes ?? [],
    });
    setCredentialsJson("{}");
    setOpen(true);
  }

  function openEdit(c: Connector) {
    setEditing(c);
    setForm({
      name: c.name,
      provider: c.provider,
      credentials: {},
      oauth_scopes: c.oauth_scopes,
      command_or_url: c.command_or_url,
      transport: c.transport,
      enabled: c.enabled,
      extra: c.extra,
    });
    setCredentialsJson("{}");
    setOpen(true);
  }

  function submit() {
    if (!form.name.trim() || !form.provider) {
      toast.error("请填写名称并选择 provider");
      return;
    }
    let credentials = form.credentials;
    if (!editing) {
      try {
        credentials = JSON.parse(credentialsJson || "{}");
      } catch {
        toast.error("凭证 JSON 解析失败");
        return;
      }
    }
    if (editing) {
      const scopes = (form.oauth_scopes ?? [])
        .map((s) => s.trim())
        .filter(Boolean);
      updateMut.mutate({
        id: editing.id,
        body: { name: form.name.trim(), oauth_scopes: scopes },
      });
    } else {
      createMut.mutate({ ...form, credentials });
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">连接器</h1>
          <p className="text-sm text-muted-foreground">
            管理第三方集成与凭证。凭证加密存储，仅显示掩码；所有变更都会记入审计日志。
          </p>
        </div>
        <Button onClick={openCreate} className="gap-2">
          <Plus className="h-4 w-4" /> 新建连接器
        </Button>
      </div>

      {isLoading ? (
        <p className="text-sm text-muted-foreground">加载中…</p>
      ) : connectors.length === 0 ? (
        <p className="text-sm text-muted-foreground">还没有连接器，点击右上角新建。</p>
      ) : (
        <div className="grid gap-3">
          {connectors.map((c) => {
            const manifest = providerByName.get(c.provider);
            return (
              <div
                key={c.id}
                className="flex flex-wrap items-center gap-3 rounded-lg border border-border bg-card p-4"
              >
                <div className="flex h-9 w-9 items-center justify-center rounded-md bg-secondary">
                  <Plug className="h-4 w-4" />
                </div>
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-medium">{c.name}</span>
                    <Badge variant="outline">{c.provider}</Badge>
                    <Badge variant="outline">{c.transport}</Badge>
                    {c.enabled ? (
                      <Badge variant="outline" className="text-green-600">
                        已启用
                      </Badge>
                    ) : (
                      <Badge variant="outline" className="text-muted-foreground">
                        已停用
                      </Badge>
                    )}
                  </div>
                  <div className="mt-0.5 truncate text-xs text-muted-foreground">
                    {c.command_or_url || manifest?.description || c.provider}
                  </div>
                  {c.last_used_at && (
                    <div className="text-xs text-muted-foreground">
                      最近使用：{new Date(c.last_used_at).toLocaleString()}
                    </div>
                  )}
                </div>
                <div className="flex items-center gap-2">
                  <Switch
                    checked={c.enabled}
                    onCheckedChange={(v) =>
                      toggleMut.mutate({ id: c.id, enable: v })
                    }
                    disabled={toggleMut.isPending}
                    aria-label={c.enabled ? "停用" : "启用"}
                  />
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() =>
                      toggleMut.mutate({ id: c.id, enable: !c.enabled })
                    }
                    disabled={toggleMut.isPending}
                    title={c.enabled ? "停用" : "启用"}
                  >
                    {c.enabled ? (
                      <PowerOff className="h-3.5 w-3.5" />
                    ) : (
                      <Power className="h-3.5 w-3.5" />
                    )}
                  </Button>
                  <Button variant="ghost" size="sm" onClick={() => openEdit(c)} title="编辑名称/授权范围">
                    <Pencil className="h-3.5 w-3.5" />
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => rotateCredentials(c)}
                    disabled={rotateMut.isPending}
                    title="轮换凭证"
                  >
                    <RefreshCw className={cn("h-3.5 w-3.5", rotateMut.isPending && "animate-spin")} />
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => {
                      if (confirm(`删除连接器「${c.name}」？`))
                        deleteMut.mutate(c.id);
                    }}
                  >
                    <Trash2 className="h-3.5 w-3.5 text-destructive" />
                  </Button>
                </div>
              </div>
            );
          })}
        </div>
      )}

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-h-[90vh] max-w-lg overflow-y-auto">
          <DialogHeader>
            <DialogTitle>{editing ? "编辑连接器" : "新建连接器"}</DialogTitle>
          </DialogHeader>
          <div className="grid gap-4 py-2">
            <div className="grid gap-1.5">
              <Label className="text-xs text-muted-foreground">名称</Label>
              <Input
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                placeholder="例如：我的 GitHub"
              />
            </div>
            <div className="grid grid-cols-2 gap-4">
              <div className="grid gap-1.5">
                <Label className="text-xs text-muted-foreground">Provider</Label>
                <Select
                  value={form.provider}
                  onValueChange={(v) => {
                    const m = providerByName.get(v);
                    setForm({
                      ...form,
                      provider: v,
                      transport: m?.transport ?? form.transport,
                      command_or_url: m?.command_or_url ?? form.command_or_url,
                      oauth_scopes: m?.required_scopes ?? form.oauth_scopes,
                    });
                  }}
                  disabled={!!editing}
                >
                  <SelectTrigger>
                    <SelectValue placeholder="选择 provider" />
                  </SelectTrigger>
                  <SelectContent>
                    {providers.map((p) => (
                      <SelectItem key={p.name} value={p.name}>
                        {p.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="grid gap-1.5">
                <Label className="text-xs text-muted-foreground">Transport</Label>
                <Input
                  value={form.transport ?? ""}
                  onChange={(e) =>
                    setForm({ ...form, transport: e.target.value })
                  }
                  disabled={!!editing}
                />
              </div>
            </div>
            {!editing && (
              <div className="grid gap-1.5">
                <Label className="text-xs text-muted-foreground">
                  凭证（JSON，创建后加密存储）
                </Label>
                <Textarea
                  value={credentialsJson}
                  onChange={(e) => setCredentialsJson(e.target.value)}
                  className="min-h-[90px] font-mono text-xs"
                  placeholder='{"token":"..."}'
                />
              </div>
            )}
            <div className="grid gap-1.5">
              <Label className="text-xs text-muted-foreground">命令 / URL</Label>
              <Input
                value={form.command_or_url ?? ""}
                onChange={(e) =>
                  setForm({ ...form, command_or_url: e.target.value })
                }
              />
            </div>
            <div className="grid gap-1.5">
              <Label className="text-xs text-muted-foreground">
                OAuth 授权范围（逗号分隔；预填 provider 必需项，可按需增删）
              </Label>
              <Input
                value={(form.oauth_scopes ?? []).join(", ")}
                onChange={(e) =>
                  setForm({
                    ...form,
                    oauth_scopes: e.target.value
                      .split(",")
                      .map((s) => s.trim())
                      .filter(Boolean),
                  })
                }
                placeholder="repo, read:org"
              />
              {(() => {
                const m = providerByName.get(form.provider);
                if (!m?.required_scopes?.length) return null;
                const missing = m.required_scopes.filter(
                  (s) => !(form.oauth_scopes ?? []).includes(s),
                );
                return missing.length ? (
                  <p className="text-[11px] text-amber-600">
                    缺少该 provider 的必需范围：{missing.join(", ")}（启用时会被拒绝）
                  </p>
                ) : null;
              })()}
            </div>
            <label className="flex items-center gap-2 text-sm">
              <Switch
                checked={!!form.enabled}
                onCheckedChange={(v) => setForm({ ...form, enabled: v })}
              />
              创建后立即启用
            </label>
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
