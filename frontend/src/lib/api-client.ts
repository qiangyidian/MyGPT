"use client";

import { getAccessToken, setAccessToken } from "./auth";

// Browser-visible API base. MUST be baked at build time for any non-local
// deployment (a build without NEXT_PUBLIC_API_BASE_URL once shipped to
// production and every request silently hit http://localhost:8000 — the user
// saw "Failed to fetch"). In the browser, a missing variable is a hard error
// instead of a silent localhost fallback; on the server (SSR/prerender) the
// fallback stays so static builds don't crash.
export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL ||
  (typeof window !== "undefined"
    ? (() => {
        throw new Error(
          "NEXT_PUBLIC_API_BASE_URL 未设置：请在前端构建时提供后端 API 地址"
        );
      })()
    : "http://localhost:8000");

export class ApiError extends Error {
  status: number;
  code: string;
  /**
   * Raw non-string server payload behind the failure — a pydantic validation
   * error array (`{"detail": [{loc, msg, type}]}`) or the envelope's
   * `details` (`{"details": {"errors": [...]}}`). Kept separate so
   * `@/lib/api-error` can render it in Chinese instead of stringifying an
   * array into `message`; never show it to a user verbatim.
   */
  detail?: unknown;
  constructor(
    status: number,
    code: string,
    message: string,
    detail?: unknown
  ) {
    super(message);
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

// These endpoints answer 401 with *credential* errors ("Invalid email or
// password", a bad scan code), not session expiry — running the refresh dance
// on them would mask the real message behind "会话已过期", so they bypass the
// retry below.
const CREDENTIAL_AUTH_PATHS = new Set([
  "/api/auth/login",
  "/api/auth/register",
  "/api/auth/login/wechat",
  // 改密 / 重置密码回 401 说的是「原密码不对」「账号已被禁用」，不是会话过期。
  // 少了这三条，/api/auth/password 的 401 会先跑一遍 refresh 再被改写成
  // 「会话已过期，请重新登录」—— 用户看不到真正的原因，还会被顺手换掉 token。
  "/api/auth/password",
  "/api/auth/password/forgot",
  "/api/auth/password/reset",
]);

let refreshing: Promise<boolean> | null = null;

export async function refreshAccessToken(): Promise<boolean> {
  if (refreshing) return refreshing;
  refreshing = (async () => {
    try {
      const res = await fetch(`${API_BASE}/api/auth/refresh`, {
        method: "POST",
        credentials: "include",
        // Bound the refresh so a hung backend can't lock up every concurrent
        // 401 retry — `refreshing` is a shared singleton (see below).
        signal: AbortSignal.timeout(15_000),
      });
      if (!res.ok) return false;
      const data = await res.json();
      setAccessToken(data.access_token);
      return true;
    } catch {
      return false;
    } finally {
      refreshing = null;
    }
  })();
  return refreshing;
}

export async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  opts: { raw?: boolean; headers?: Record<string, string>; signal?: AbortSignal } = {}
): Promise<T> {
  const doFetch = async (token: string | null): Promise<Response> => {
    const headers: Record<string, string> = { ...(opts.headers || {}) };
    if (body !== undefined && !(body instanceof FormData)) {
      headers["Content-Type"] = "application/json";
    }
    if (token) headers["Authorization"] = `Bearer ${token}`;
    return fetch(`${API_BASE}${path}`, {
      method,
      headers,
      credentials: "include",
      // Forward an optional caller signal so long-lived calls can be cancelled
      // (a hung backend otherwise leaves the promise pending until the browser's
      // own ~300s network timeout).
      signal: opts.signal,
      body:
        body === undefined
          ? undefined
          : body instanceof FormData
            ? body
            : JSON.stringify(body),
    });
  };

  let res: Response;
  try {
    res = await doFetch(getAccessToken());
  } catch (err) {
    if (err instanceof ApiError) throw err;
    // fetch() rejects with a bare TypeError on network failure; surface a
    // Chinese, actionable message instead of the browser's raw English.
    if (typeof navigator !== "undefined" && navigator.onLine === false) {
      throw new ApiError(0, "offline", "网络连接已断开，请检查网络后重试");
    }
    throw new ApiError(0, "network_error", "网络请求失败，请稍后重试");
  }

  if (res.status === 401 && !CREDENTIAL_AUTH_PATHS.has(path)) {
    const ok = await refreshAccessToken();
    if (ok) {
      try {
        res = await doFetch(getAccessToken());
      } catch {
        throw new ApiError(0, "network_error", "网络请求失败，请稍后重试");
      }
    } else {
      setAccessToken(null);
      throw new ApiError(401, "unauthorized", "会话已过期，请重新登录");
    }
  }

  if (!res.ok) {
    let code = "error";
    let message = res.statusText;
    let detail: unknown;
    try {
      const data = await res.json();
      code = data.code || code;
      // `message` must stay a string: FastAPI's `detail` is a pydantic error
      // ARRAY on unhandled 422s, and `@/lib/api-error` needs it intact rather
      // than "[object Object]" from an implicit toString().
      detail = data.detail ?? (typeof data.message === "string" ? data.message : data);
      message =
        typeof data.message === "string" && data.message
          ? data.message
          : typeof data.detail === "string" && data.detail
            ? data.detail
            : message;
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, code, message, detail);
  }
  if (opts.raw) return res as unknown as T;
  if (res.status === 204) return undefined as unknown as T;
  return (await res.json()) as T;
}

/** A paginated list request: server-side page size + zero-based skip. */
