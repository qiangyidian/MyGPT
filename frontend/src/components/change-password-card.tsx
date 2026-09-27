"use client";

import { useState, type FormEvent } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { KeyRound, Loader2 } from "lucide-react";
import { toast } from "sonner";

import { api, ApiError } from "@/lib/api";
import { userErrorMessage } from "@/lib/api-error";
// 规则常量与中文文案的唯一来源（后端 `app/core/security.py` 的抄本）。
import { passwordProblem, passwordPolicySummary } from "@/lib/password-policy";
import { AUTH_QUERY_KEY } from "@/hooks/useAuth";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { PasswordInput } from "@/components/ui/password-input";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

/** 只把服务端能给出的额外信息翻成中文（策略类 400 后端本来也是中文）。 */
function errorText(err: unknown): string {
  // 这两个状态在本页有专门语义（限流窗口 / 原密码错误），其余交给统一映射。
  if (err instanceof ApiError) {
    if (err.status === 429) return "修改过于频繁，请 1 分钟后再试";
    if (err.status === 401) return err.message || "原密码不正确";
  }
  return userErrorMessage(err);
}

/**
 * 修改密码。
 *
 * 两件事必须说清楚，界面上就不能省：
 *  1. 改密会让这个账号**所有已登录的设备**掉线（后端 bump token_version 并把
 *     当前 access/refresh 拉黑），只有本次请求换回的新会话还活着；
 *  2. 公众号扫码自动注册的账号从来没设过密码，这类账号「原密码」可以留空 ——
 *     其余账号留空会被服务端拒绝。
 *
 * 密码只进请求体，绝不进 URL（无 query string、无日志泄漏面）。
 */
export function ChangePasswordCard() {
  const qc = useQueryClient();
  const [oldPwd, setOldPwd] = useState("");
  const [newPwd, setNewPwd] = useState("");
  const [confirmPwd, setConfirmPwd] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const clear = () => {
    setOldPwd("");
    setNewPwd("");
    setConfirmPwd("");
  };

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);

    const problem = passwordProblem(newPwd);
    if (problem) {
      setError(problem);
      return;
    }
    if (newPwd !== confirmPwd) {
      setError("两次输入的新密码不一致");
      return;
    }
    if (oldPwd && oldPwd === newPwd) {
      setError("新密码与原密码相同，请换一个");
      return;
    }
    setPending(true);
    try {
      await api.changePassword(oldPwd || null, newPwd);
      // token 已经换新的（旧的被拉黑）：让 useAuth 重新读一次当前用户，
      // 免得界面上挂着旧会话的缓存。
      qc.invalidateQueries({ queryKey: AUTH_QUERY_KEY });
      clear();
      toast.success("密码已修改，其他设备需要用新密码重新登录");
    } catch (err) {
      setError(errorText(err));
    } finally {
      setPending(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <div className="space-y-1">
          <CardTitle className="flex items-center gap-2 text-base">
            <KeyRound className="h-4 w-4" />
            密码
          </CardTitle>
          <CardDescription>
            {passwordPolicySummary()}。修改成功后其它设备的登录状态会全部失效。
          </CardDescription>
        </div>
      </CardHeader>
      <CardContent>
        <form onSubmit={submit} className="max-w-md space-y-4">
          <div className="space-y-2">
            <Label htmlFor="cp-old">原密码</Label>
            <PasswordInput
              id="cp-old"
              autoComplete="current-password"
              placeholder="公众号扫码注册且未设过密码可留空"
              value={oldPwd}
              onChange={(e) => {
                setOldPwd(e.target.value);
                setError(null);
              }}
              disabled={pending}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="cp-new">新密码</Label>
            <PasswordInput
              id="cp-new"
              autoComplete="new-password"
              placeholder={passwordPolicySummary()}
              value={newPwd}
              onChange={(e) => {
                setNewPwd(e.target.value);
                setError(null);
              }}
              disabled={pending}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="cp-confirm">确认新密码</Label>
            <PasswordInput
              id="cp-confirm"
              autoComplete="new-password"
              placeholder="再次输入新密码"
              value={confirmPwd}
              onChange={(e) => {
                setConfirmPwd(e.target.value);
                setError(null);
              }}
              disabled={pending}
            />
          </div>

          {error && (
            <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
              {error}
            </div>
          )}

          <div className="flex items-center gap-2">
            <Button type="submit" disabled={pending || !newPwd || !confirmPwd}>
              {pending && <Loader2 className="animate-spin" />}
              {pending ? "提交中…" : "修改密码"}
            </Button>
            <Button
              type="button"
              variant="ghost"
              disabled={pending}
              onClick={() => {
                clear();
                setError(null);
              }}
            >
              清空
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}
