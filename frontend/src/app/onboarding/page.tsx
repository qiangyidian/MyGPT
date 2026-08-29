"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { api, ApiError } from "@/lib/api";
import { buildLoginUrl } from "@/lib/navigation";
import {
  clearOnboardingSkipped,
  markOnboardingSkipped,
} from "@/lib/onboarding-preference";
import { useAuth } from "@/hooks/useAuth";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

/**
 * First-run setup: a brand-new deploy boots with only the Mock provider, so an
 * admin must wire a real OpenAI-compatible endpoint before the product has any
 * value. This wizard collects base_url + key + chat model (+ optional embedding
 * model) and creates the ModelConfig rows via the existing admin API — no .env
 * edit / reboot required. Gated to admins; non-admins are bounced to /.
 */
export default function OnboardingPage() {
  const router = useRouter();
  const qc = useQueryClient();
  const { user, isLoading } = useAuth();

  const [baseUrl, setBaseUrl] = useState("https://api.openai.com/v1");
  const [apiKey, setApiKey] = useState("");
  const [chatModel, setChatModel] = useState("gpt-4o-mini");
  const [embedModel, setEmbedModel] = useState("text-embedding-3-small");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (isLoading) return;
    if (!user) {
      // Carry /onboarding as the post-login destination.
      router.replace(buildLoginUrl("/onboarding"));
    } else if (user.role !== "admin") {
      router.replace("/");
    }
  }, [isLoading, user, router]);

  const submit = async () => {
    if (!apiKey.trim() || !baseUrl.trim() || !chatModel.trim()) {
      toast.error("请填写 API 地址、密钥和聊天模型名");
      return;
    }
    setBusy(true);
    try {
      // Create first, then run the REAL connectivity test — a bad key fails
      // here (with a clear message) instead of on the first chat turn.
      const created = await api.createModel({
        name: chatModel,
        provider: "openai-compatible",
        api_base_url: baseUrl.trim(),
        api_key: apiKey.trim(),
        model_name: chatModel.trim(),
        supports_stream: true,
        supports_tools: true,
      });
      // Real connectivity probe: fail loudly on a bad key/URL/model name.
      const test = await api.testModel(created.id);
      if (!test.ok) {
        toast.error("连接测试失败", {
          description: `${test.error ?? "未知错误"}。模型配置已保存，可稍后在「设置 → 模型配置」中修正。`,
          duration: 8000,
        });
      } else {
        toast.success(`连接成功 · ${test.latency_ms}ms`);
      }
      if (embedModel.trim()) {
        await api.createModel({
          name: `${embedModel} (embedding)`,
          provider: "openai-compatible",
          api_base_url: baseUrl.trim(),
          api_key: apiKey.trim(),
          model_name: embedModel.trim(),
          is_embedding: true,
          supports_stream: false,
        });
      }
      await qc.invalidateQueries({ queryKey: ["models"] });
      // A real model now exists — clear any prior "skip" so AppShell's gate is
      // based on model state alone going forward.
      if (user) clearOnboardingSkipped(user.id);
      toast.success("模型已配置，正在进入对话…");
      router.replace("/");
    } catch (e) {
      toast.error("保存失败", { description: e instanceof ApiError ? e.message : undefined });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-background p-4">
      <div className="w-full max-w-md space-y-5 rounded-xl border border-border bg-card p-6 shadow-sm">
        <div className="space-y-1">
          <h1 className="text-lg font-semibold">配置你的模型</h1>
          <p className="text-sm text-muted-foreground">
            连接一个 OpenAI 兼容接口，即可开始真实对话。也可稍后在「管理」中添加更多模型。
          </p>
        </div>

        <div className="space-y-3">
          <label className="block space-y-1">
            <span className="text-xs font-medium text-muted-foreground">API 地址 (Base URL)</span>
            <Input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="https://api.openai.com/v1" />
          </label>
          <label className="block space-y-1">
            <span className="text-xs font-medium text-muted-foreground">API 密钥</span>
            <Input type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)} placeholder="sk-..." />
          </label>
          <label className="block space-y-1">
            <span className="text-xs font-medium text-muted-foreground">聊天模型</span>
            <Input value={chatModel} onChange={(e) => setChatModel(e.target.value)} placeholder="gpt-4o-mini" />
          </label>
          <label className="block space-y-1">
            <span className="text-xs font-medium text-muted-foreground">嵌入模型（可选，知识库需要）</span>
            <Input value={embedModel} onChange={(e) => setEmbedModel(e.target.value)} placeholder="text-embedding-3-small" />
          </label>
        </div>

        <div className="flex items-center justify-between gap-2">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              // Record the dismissal so AppShell no longer bounces us back here.
              if (user) markOnboardingSkipped(user.id);
              router.replace("/");
            }}
            disabled={busy}
          >
            稍后配置（用 Mock 体验）
          </Button>
          <Button onClick={submit} disabled={busy}>
            {busy ? "保存中…" : "保存并开始"}
          </Button>
        </div>
      </div>
    </div>
  );
}
