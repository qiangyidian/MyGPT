"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { ShieldCheck, Unlink } from "lucide-react";

import { api, ApiError } from "@/lib/api";
import { WechatLoginPanel } from "@/components/wechat-login-panel";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

const BINDING_KEY = ["wechat-binding"] as const;
const LOGIN_INFO_KEY = ["wechat-login-info"] as const;

/**
 * 账号安全 — currently just the WeChat binding.
 *
 * Binding is what makes scan login usable for an account that already exists.
 * Without it, an admin (or anyone who registered by email) who scans the
 * Official Account is handed a brand-new empty account instead of logging back
 * into their own.
 */
export default function AccountSettingsPage() {
  const qc = useQueryClient();
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);

  const { data: binding, isLoading } = useQuery({
    queryKey: BINDING_KEY,
    queryFn: api.fetchWechatBinding,
  });
  const { data: info } = useQuery({
    queryKey: LOGIN_INFO_KEY,
    queryFn: api.fetchWechatLoginInfo,
  });

  const bind = useMutation({
    mutationFn: (wechatCode: string) => api.bindWechat(wechatCode),
    onSuccess: (result) => {
      toast.success("已绑定微信，下次可直接扫码登录");
      setCode("");
      setError(null);
      qc.setQueryData(BINDING_KEY, result);
    },
    onError: (err) => setError(toMessage(err)),
  });

  const unbind = useMutation({
    mutationFn: api.unbindWechat,
    onSuccess: (result) => {
      toast.success("已解绑微信");
      qc.setQueryData(BINDING_KEY, result);
    },
    onError: (err) => toast.error(toMessage(err)),
  });

  const bound = Boolean(binding?.bound);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">账号安全</h1>
        <p className="text-sm text-muted-foreground">
          管理登录方式与账号绑定。
        </p>
      </div>

      <Card>
        <CardHeader>
          <div className="flex items-center justify-between gap-2">
            <div className="space-y-1">
              <CardTitle className="flex items-center gap-2 text-base">
                <ShieldCheck className="h-4 w-4" />
                微信公众号
              </CardTitle>
              <CardDescription>
                绑定后可直接用公众号验证码扫码登录，无需输入邮箱密码。
              </CardDescription>
            </div>
            <Badge variant={bound ? "default" : "secondary"}>
              {isLoading ? "加载中" : bound ? "已绑定" : "未绑定"}
            </Badge>
          </div>
        </CardHeader>
        <CardContent>
          {bound ? (
            <div className="space-y-3">
              {binding?.openid ? (
                <p className="text-sm text-muted-foreground">
                  当前绑定标识：<code className="text-xs">{binding.openid}</code>
                </p>
              ) : null}
              <Button
                type="button"
                variant="outline"
                className="gap-2"
                disabled={unbind.isPending}
                onClick={() => unbind.mutate()}
              >
                <Unlink className="h-4 w-4" />
                解绑微信
              </Button>
              <p className="text-xs text-muted-foreground">
                解绑后扫码将不再登入本账号。
              </p>
            </div>
          ) : (
            <WechatLoginPanel
              info={info ?? null}
              code={code}
              onCodeChange={(v) => {
                setCode(v);
                setError(null);
              }}
              onSubmit={() => {
                setError(null);
                bind.mutate(code);
              }}
              loading={bind.isPending}
              error={error}
              submitLabel="绑定微信"
            />
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function toMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  return "操作失败，请稍后重试";
}
