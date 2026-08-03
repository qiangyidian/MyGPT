/**
 * Pure navigation/URL helpers.
 *
 * Centralised here so that no page duplicates query-param parsing and so that
 * every "return to" / "next" target is funneled through the same open-redirect
 * guard (`sanitizeInternalPath`).
 *
 * These functions are intentionally side-effect free and SSR-safe (they never
 * touch `window`), which keeps them unit-testable in vitest's node environment.
 */

/** The query-string key used to persist the active conversation on the home URL. */
export const CONVERSATION_PARAM = "conversation";

/** The query-string key carrying the "return to chat" target across pages. */
export const RETURN_TO_PARAM = "returnTo";

/** The query-string key carrying the post-login destination. */
export const NEXT_PARAM = "next";

/**
 * Validate that `value` is a safe in-app absolute path (begins with a single
 * leading `/`). Returns the cleaned path, or `null` for anything that could be
 * used as an open redirect or scheme-based navigation.
 *
 * Accepted: `/`, `/?conversation=abc`, `/knowledge-bases?id=1`.
 * Rejected (→ null): `https://example.com`, `//example.com` (protocol-relative),
 * `\\example.com`, `javascript:alert(1)`, empty/non-string values, and any path
 * whose pathname segment contains a `:` (scheme) or backslash.
 */
export function sanitizeInternalPath(value: string | null | undefined): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  if (!trimmed) return null;
  // Must be an absolute in-app path (single leading slash).
  if (!trimmed.startsWith("/")) return null;
  // Reject protocol-relative (`//host`) and backslash variants.
  if (trimmed.startsWith("//") || trimmed.startsWith("/\\")) return null;
  // Reject embedded control characters used to smuggle schemes.
  if (/[\t\r\n\0]/.test(trimmed)) return null;
  // The pathname portion (before the first `?` or `#`) must not contain a colon
  // (would imply a scheme) or a backslash.
  const segEnd = trimmed.search(/[?#]/);
  const pathPart = segEnd === -1 ? trimmed : trimmed.slice(0, segEnd);
  if (/[:\\]/.test(pathPart)) return null;
  return trimmed;
}

/**
 * Build the "returnTo" target for the *current* location, e.g. to remember the
 * chat session a user is leaving. `pathname` is sanitised; `searchParams` is
 * appended verbatim so existing query state survives.
 *
 *   buildReturnTo("/", new URLSearchParams("conversation=abc"))
 *   // => "/?conversation=abc"
 */
export function buildReturnTo(pathname: string, searchParams: URLSearchParams): string {
  const safePath = sanitizeInternalPath(pathname) ?? "/";
  const qs = searchParams.toString();
  if (!qs) return safePath;
  const sep = safePath.includes("?") ? "&" : "?";
  return `${safePath}${sep}${qs}`;
}

/**
 * Append a sanitised `returnTo` value to `pathname` as a query parameter.
 * Invalid/empty `returnTo` is dropped (no param added).
 *
 *   withReturnTo("/knowledge-bases", "/?conversation=abc")
 *   // => "/knowledge-bases?returnTo=%2F%3Fconversation%3Dabc"
 */
export function withReturnTo(pathname: string, returnTo?: string | null): string {
  const safePath = sanitizeInternalPath(pathname) ?? "/";
  const params = new URLSearchParams();
  const safeReturn = returnTo ? sanitizeInternalPath(returnTo) : null;
  if (safeReturn) params.set(RETURN_TO_PARAM, safeReturn);
  const qs = params.toString();
  if (!qs) return safePath;
  const sep = safePath.includes("?") ? "&" : "?";
  return `${safePath}${sep}${qs}`;
}

/**
 * Resolve a safe destination from the current query string, preferring
 * `returnTo` then `next`. Falls back to `fallback` (default `/`) when neither is
 * a valid internal path. Used by "返回对话" buttons and post-login redirects.
 */
export function resolveReturnTo(searchParams: URLSearchParams, fallback = "/"): string {
  const fromReturnTo = sanitizeInternalPath(searchParams.get(RETURN_TO_PARAM));
  if (fromReturnTo) return fromReturnTo;
  const fromNext = sanitizeInternalPath(searchParams.get(NEXT_PARAM));
  if (fromNext) return fromNext;
  return fallback;
}

/**
 * Resolve the **chat home** target for a "返回对话" button. "返回对话" semantically
 * means "go back to the chat home", so this ALWAYS returns the home route —
 * never a sub-page or a syntactically-valid-but-nonexistent path (which would
 * 404). If the `returnTo`/`next` target is itself the home route, its
 * `conversation` param is preserved so the user lands back in the same chat;
 * otherwise it falls back to a bare `/`.
 *
 *   resolveChatHome(URLSearchParams("returnTo=/?conversation=abc")) // "/?conversation=abc"
 *   resolveChatHome(URLSearchParams("returnTo=/knowledge-bases"))   // "/"
 *   resolveChatHome(URLSearchParams("returnTo=https://evil"))       // "/"
 *   resolveChatHome(new URLSearchParams())                          // "/"
 */
export function resolveChatHome(searchParams: URLSearchParams): string {
  const target =
    sanitizeInternalPath(searchParams.get(RETURN_TO_PARAM)) ??
    sanitizeInternalPath(searchParams.get(NEXT_PARAM));
  if (target) {
    try {
      // Dummy base: target is always absolute (sanitised to start with "/").
      const u = new URL(target, "http://localhost");
      if (u.pathname === "/" || u.pathname === "") {
        const conv = u.searchParams.get(CONVERSATION_PARAM);
        if (conv) {
          return `/?${new URLSearchParams({ [CONVERSATION_PARAM]: conv }).toString()}`;
        }
      }
    } catch {
      // Fall through to bare home.
    }
  }
  return "/";
}

/**
 * Build a login URL that carries an optional post-login destination via `next`.
 * Invalid or root destinations collapse to a bare `/login`.
 *
 *   buildLoginUrl("/?conversation=abc") // => "/login?next=%2F%3Fconversation%3Dabc"
 */
export function buildLoginUrl(returnTo?: string | null): string {
  const safe = returnTo ? sanitizeInternalPath(returnTo) : null;
  if (!safe || safe === "/") return "/login";
  const params = new URLSearchParams();
  params.set(NEXT_PARAM, safe);
  return `/login?${params.toString()}`;
}

/** Read the conversation id from a query string, or null if absent/blank. */
export function getConversationIdFromSearch(searchParams: URLSearchParams): string | null {
  const value = searchParams.get(CONVERSATION_PARAM);
  if (!value) return null;
  const trimmed = value.trim();
  return trimmed || null;
}

/**
 * Produce the home URL (`/`) with `conversation` set to `conversationId` (or
 * removed when null), preserving all other query parameters.
 *
 *   withConversationParam(new URLSearchParams("foo=1"), "abc")
 *   // => "/?foo=1&conversation=abc"
 */
export function withConversationParam(
  searchParams: URLSearchParams,
  conversationId: string | null
): string {
  const params = new URLSearchParams(searchParams.toString());
  if (conversationId) {
    params.set(CONVERSATION_PARAM, conversationId);
  } else {
    params.delete(CONVERSATION_PARAM);
  }
  const qs = params.toString();
  return qs ? `/?${qs}` : "/";
}

/**
 * Remove the `conversation` parameter while preserving all other query params.
 *
 *   stripConversationParam(new URLSearchParams("conversation=abc&foo=1"))
 *   // => "/?foo=1"
 */
export function stripConversationParam(searchParams: URLSearchParams): string {
  return withConversationParam(searchParams, null);
}

/**
 * True when `referrer` is non-empty and same-origin with `origin` — i.e. the
 * previous navigation came from within this app, so `router.back()` is safe.
 * Used to stop a "返回上一页" button from bouncing users out to an external
 * referrer (history.length alone counts external entries, per the HTML spec).
 */
export function isInAppReferrer(
  referrer: string | null | undefined,
  origin: string | null | undefined
): boolean {
  if (!referrer || !origin) return false;
  try {
    // Parse the referrer as an ABSOLUTE URL (document.referrer always is one).
    // Passing `origin` as a base would let relative fragments resolve against it
    // and pass spuriously; a bare parse throws for non-absolute input.
    return new URL(referrer).origin === origin;
  } catch {
    return false;
  }
}
