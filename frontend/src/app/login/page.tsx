"use client";

import { useEffect, useState, type FormEvent } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { toast } from "sonner";
import { Loader2, MessageSquare } from "lucide-react";

import { api, ApiError } from "@/lib/api";
import { resolveReturnTo } from "@/lib/navigation";
import { useAuth } from "@/hooks/useAuth";
import { NavSuspense } from "@/components/navigation/page-loading";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui/tabs";

type Mode = "login" | "register";

export default function LoginPage() {
  return (
    <NavSuspense>
      <LoginForm />
    </NavSuspense>
  );
}

function LoginForm() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { user, isLoading: authLoading } = useAuth();
  const [mode, setMode] = useState<Mode>("login");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Shared fields
  const [email, setEmail] = useState("admin@example.com");
  const [password, setPassword] = useState("changeme123");

  // Register-only fields
  const [username, setUsername] = useState("");
  const [confirm, setConfirm] = useState("");

  // Destination after a successful login/register: a validated `next` param, or "/".
  const next = resolveReturnTo(searchParams, "/");

  // Already authenticated? Skip straight to the destination.
  useEffect(() => {
    if (authLoading || !user) return;
    router.replace(next);
  }, [authLoading, user, next, router]);

  function resetError() {
    if (error) setError(null);
  }

  async function handleLogin(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (!email.trim() || !password) {
      setError("请输入邮箱和密码");
      return;
    }
    setLoading(true);
    try {
      const { user } = await api.login(email.trim(), password);
      toast.success(`欢迎回来，${user.username}`);
      router.replace(next);
    } catch (err) {
      setError(toMessage(err));
    } finally {
      setLoading(false);
    }
  }

  async function handleRegister(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (!email.trim() || !username.trim() || !password) {
      setError("请填写邮箱、用户名和密码");
      return;
    }
    if (password.length < 6) {
      setError("密码至少 6 位");
      return;
    }
    if (password !== confirm) {
      setError("两次输入的密码不一致");
      return;
    }
    setLoading(true);
    try {
      await api.register(email.trim(), username.trim(), password);
      // Auto-login after register for smoother UX.
      const { user } = await api.login(email.trim(), password);
      toast.success(`注册成功，欢迎 ${user.username}`);
      router.replace(next);
    } catch (err) {
      setError(toMessage(err));
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-gradient-to-b from-muted/40 to-background px-4 py-10">
      <div className="w-full max-w-md">
        <div className="mb-6 flex flex-col items-center gap-2 text-center">
          <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-primary text-primary-foreground">
            <MessageSquare className="h-6 w-6" />
          </div>
          <h1 className="text-2xl font-semibold tracking-tight">AI 对话平台</h1>
          <p className="text-sm text-muted-foreground">
            登录或注册以开始你的对话
          </p>
        </div>

        <Card>
          <CardHeader className="space-y-1 pb-4">
            <CardTitle className="text-xl">账号</CardTitle>
            <CardDescription>
              选择登录已有账号，或注册一个新账号
            </CardDescription>
          </CardHeader>
          <CardContent className="pt-0">
            <Tabs
              value={mode}
              onValueChange={(v) => {
                setMode(v as Mode);
                resetError();
              }}
            >
              <TabsList className="grid w-full grid-cols-2">
                <TabsTrigger value="login">登录</TabsTrigger>
                <TabsTrigger value="register">注册</TabsTrigger>
              </TabsList>

              {/* ---------------- Login ---------------- */}
              <TabsContent value="login" className="mt-4">
                <form onSubmit={handleLogin} className="space-y-4">
                  <div className="space-y-2">
                    <Label htmlFor="login-email">邮箱</Label>
                    <Input
                      id="login-email"
                      type="email"
                      autoComplete="email"
                      placeholder="you@example.com"
                      value={email}
                      onChange={(e) => {
                        setEmail(e.target.value);
                        resetError();
                      }}
                      disabled={loading}
                    />
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="login-password">密码</Label>
                    <Input
                      id="login-password"
                      type="password"
                      autoComplete="current-password"
                      placeholder="••••••"
                      value={password}
                      onChange={(e) => {
                        setPassword(e.target.value);
                        resetError();
                      }}
                      disabled={loading}
                    />
                  </div>

                  {error && <ErrorBox message={error} />}

                  <Button type="submit" className="w-full" disabled={loading}>
                    {loading && <Loader2 className="animate-spin" />}
                    登录
                  </Button>
                </form>
              </TabsContent>

              {/* ---------------- Register ---------------- */}
              <TabsContent value="register" className="mt-4">
                <form onSubmit={handleRegister} className="space-y-4">
                  <div className="space-y-2">
                    <Label htmlFor="reg-email">邮箱</Label>
                    <Input
                      id="reg-email"
                      type="email"
                      autoComplete="email"
                      placeholder="you@example.com"
                      value={email}
                      onChange={(e) => {
                        setEmail(e.target.value);
                        resetError();
                      }}
                      disabled={loading}
                    />
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="reg-username">用户名</Label>
                    <Input
                      id="reg-username"
                      autoComplete="username"
                      placeholder="your-name"
                      value={username}
                      onChange={(e) => {
                        setUsername(e.target.value);
                        resetError();
                      }}
                      disabled={loading}
                    />
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="reg-password">密码</Label>
                    <Input
                      id="reg-password"
                      type="password"
                      autoComplete="new-password"
                      placeholder="至少 6 位"
                      value={password}
                      onChange={(e) => {
                        setPassword(e.target.value);
                        resetError();
                      }}
                      disabled={loading}
                    />
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="reg-confirm">确认密码</Label>
                    <Input
                      id="reg-confirm"
                      type="password"
                      autoComplete="new-password"
                      placeholder="再次输入密码"
                      value={confirm}
                      onChange={(e) => {
                        setConfirm(e.target.value);
                        resetError();
                      }}
                      disabled={loading}
                    />
                  </div>

                  {error && <ErrorBox message={error} />}

                  <Button type="submit" className="w-full" disabled={loading}>
                    {loading && <Loader2 className="animate-spin" />}
                    注册并登录
                  </Button>
                </form>
              </TabsContent>
            </Tabs>
          </CardContent>
          <CardFooter className="flex flex-col gap-2 border-t bg-muted/30 py-3">
            <p className="text-center text-xs text-muted-foreground">
              演示管理员账号：{" "}
              <span className="font-medium text-foreground">
                admin@example.com / changeme123
              </span>
            </p>
          </CardFooter>
        </Card>
      </div>
    </div>
  );
}

function ErrorBox({ message }: { message: string }) {
  return (
    <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
      {message}
    </div>
  );
}

function toMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  return "操作失败，请稍后重试";
}
