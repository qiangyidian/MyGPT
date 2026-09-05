"use client";

import { useState } from "react";
import { Loader2, Trash2 } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { PasswordInput } from "@/components/ui/password-input";
import { ApiError } from "@/lib/api";

interface DeleteAccountDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onDeleted: () => void;
  /** Calls the API and clears local session state. */
  deleteAccount: (password: string) => Promise<void>;
}

/**
 * 账号注销 confirmation: password re-authentication + explicit danger
 * copy. irreversible by design, so the confirm button stays disabled until
 * a password is entered, and the destructive action is the only red element.
 */
export function DeleteAccountDialog({
  open,
  onOpenChange,
  onDeleted,
  deleteAccount,
}: DeleteAccountDialogProps) {
  const [password, setPassword] = useState("");
  const [pending, setPending] = useState(false);

  const submit = async () => {
    if (!password || pending) return;
    setPending(true);
    try {
      await deleteAccount(password);
      onOpenChange(false);
      onDeleted();
    } catch (err) {
      const message =
        err instanceof ApiError ? err.message : "注销失败，请稍后重试";
      toast.error(message);
    } finally {
      setPending(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 text-destructive">
            <Trash2 className="h-4 w-4" />
            注销账号
          </DialogTitle>
          <DialogDescription>
            此操作不可恢复。你的所有会话、消息、上传文件、知识库和记忆将被永久删除，
            账号信息将被匿名化。请输入登录密码确认。
          </DialogDescription>
        </DialogHeader>
        <PasswordInput
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="登录密码"
          autoComplete="current-password"
          onKeyDown={(e) => {
            if (e.key === "Enter") void submit();
          }}
        />
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)} disabled={pending}>
            取消
          </Button>
          <Button
            variant="destructive"
            onClick={() => void submit()}
            disabled={!password || pending}
          >
            {pending && <Loader2 className="mr-1 h-4 w-4 animate-spin" />}
            永久注销
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
