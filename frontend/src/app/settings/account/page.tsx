"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { ShieldCheck, Unlink } from "lucide-react";

import { api } from "@/lib/api";
import { userErrorMessage } from "@/lib/api-error";
import { ChangePasswordCard } from "@/components/change-password-card";
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
 * 账号安全 — 密码 + WeChat binding.
 *
 * 改密放在这里而不是只放登录页：已登录用户（尤其是公众号扫码进来的）需要一个
 * 明确的地方换掉口令，并且要知道换完之后其它设备会掉线。后端已经有
 * ``POST /api/auth/password``（校验原密码 + bump token_version），此前只是没有
 * 任何表单调用它。
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

      <ChangePasswordCard />
    </div>
  );
}

// 统一中文映射：不再把 ApiError 的原始 message（可能是英文）直接显示出来。
function toMessage(err: unknown): string {
  return userErrorMessage(err);
}
