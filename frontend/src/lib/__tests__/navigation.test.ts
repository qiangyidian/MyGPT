import { describe, it, expect } from "vitest";
import {
  CONVERSATION_PARAM,
  RETURN_TO_PARAM,
  NEXT_PARAM,
  sanitizeInternalPath,
  buildReturnTo,
  withReturnTo,
  resolveReturnTo,
  buildLoginUrl,
  getConversationIdFromSearch,
  withConversationParam,
  stripConversationParam,
  isInAppReferrer,
  resolveChatHome,
} from "@/lib/navigation";

const sp = (s: string) => new URLSearchParams(s);

describe("sanitizeInternalPath", () => {
  it("accepts the bare home path", () => {
    expect(sanitizeInternalPath("/")).toBe("/");
  });

  it("accepts a path with a conversation query", () => {
    expect(sanitizeInternalPath("/?conversation=abc")).toBe("/?conversation=abc");
  });

  it("accepts a deeper path with query params", () => {
    expect(sanitizeInternalPath("/knowledge-bases?id=1")).toBe("/knowledge-bases?id=1");
  });

  it("accepts a settings sub-path", () => {
    expect(sanitizeInternalPath("/settings/models")).toBe("/settings/models");
  });

  it("rejects absolute https URLs", () => {
    expect(sanitizeInternalPath("https://example.com")).toBeNull();
  });

  it("rejects absolute http URLs", () => {
    expect(sanitizeInternalPath("http://example.com")).toBeNull();
  });

  it("rejects protocol-relative URLs", () => {
    expect(sanitizeInternalPath("//example.com")).toBeNull();
  });

  it("rejects javascript: scheme", () => {
    expect(sanitizeInternalPath("javascript:alert(1)")).toBeNull();
  });

  it("rejects backslash protocol-relative URLs", () => {
    expect(sanitizeInternalPath("/\\example.com")).toBeNull();
    expect(sanitizeInternalPath("\\\\example.com")).toBeNull();
  });

  it("rejects empty / null / undefined / whitespace", () => {
    expect(sanitizeInternalPath("")).toBeNull();
    expect(sanitizeInternalPath(null)).toBeNull();
    expect(sanitizeInternalPath(undefined)).toBeNull();
    expect(sanitizeInternalPath("   ")).toBeNull();
  });

  it("rejects control-character smuggling", () => {
    expect(sanitizeInternalPath("/\tjavascript:alert(1)")).toBeNull();
    expect(sanitizeInternalPath("/\nfoo")).toBeNull();
  });

  it("allows colons inside query values (only pathname is checked)", () => {
    expect(sanitizeInternalPath("/?u=http://evil")).toBe("/?u=http://evil");
  });
});

describe("buildReturnTo", () => {
  it("joins pathname and search params", () => {
    expect(buildReturnTo("/", sp("conversation=abc"))).toBe("/?conversation=abc");
  });

  it("returns the bare path when there are no params", () => {
    expect(buildReturnTo("/", sp(""))).toBe("/");
  });

  it("sanitises a malicious pathname back to root", () => {
    expect(buildReturnTo("//evil.com", sp(""))).toBe("/");
  });

  it("preserves multiple params", () => {
    expect(buildReturnTo("/", sp("conversation=abc&tab=2"))).toBe("/?conversation=abc&tab=2");
  });
});

describe("withReturnTo", () => {
  it("appends a returnTo param, URL-encoded", () => {
    const out = withReturnTo("/knowledge-bases", "/?conversation=abc");
    const parsed = new URLSearchParams(out.split("?")[1] ?? "");
    expect(out.split("?")[0]).toBe("/knowledge-bases");
    expect(parsed.get(RETURN_TO_PARAM)).toBe("/?conversation=abc");
  });

  it("omits the param when returnTo is invalid", () => {
    expect(withReturnTo("/knowledge-bases", "https://evil.com")).toBe("/knowledge-bases");
    expect(withReturnTo("/knowledge-bases", null)).toBe("/knowledge-bases");
    expect(withReturnTo("/knowledge-bases", undefined)).toBe("/knowledge-bases");
  });

  it("sanitises a malicious destination pathname back to root", () => {
    // "//evil.com" is not a valid internal path → collapses to "/", but the
    // valid returnTo ("/") is still carried through.
    const out = withReturnTo("//evil.com", "/");
    expect(out.startsWith("/")).toBe(true);
    expect(out).not.toContain("evil");
    expect(new URLSearchParams(out.split("?")[1] ?? "").get(RETURN_TO_PARAM)).toBe("/");
  });

  it("preserves an existing query on the pathname", () => {
    const out = withReturnTo("/knowledge-bases?tab=1", "/?conversation=abc");
    const parsed = new URLSearchParams(out.split("?")[1] ?? "");
    // two params now: tab + returnTo
    expect(parsed.get("tab")).toBe("1");
    expect(parsed.get(RETURN_TO_PARAM)).toBe("/?conversation=abc");
  });
});

describe("resolveReturnTo", () => {
  it("prefers returnTo", () => {
    expect(resolveReturnTo(sp("returnTo=/&next=/admin"))).toBe("/");
  });

  it("falls back to next when returnTo absent/invalid", () => {
    expect(resolveReturnTo(sp("next=/admin"))).toBe("/admin");
    expect(resolveReturnTo(sp("returnTo=https://evil&next=/admin"))).toBe("/admin");
  });

  it("falls back to default when neither is valid", () => {
    expect(resolveReturnTo(sp(""))).toBe("/");
    expect(resolveReturnTo(sp("returnTo=https://evil"), "/?conversation=x")).toBe(
      "/?conversation=x"
    );
  });
});

describe("buildLoginUrl", () => {
  it("encodes a valid destination into next", () => {
    const out = buildLoginUrl("/?conversation=abc");
    expect(out.startsWith("/login?next=")).toBe(true);
    const parsed = new URLSearchParams(out.split("?")[1] ?? "");
    expect(parsed.get(NEXT_PARAM)).toBe("/?conversation=abc");
  });

  it("collapses root / invalid destinations to bare /login", () => {
    expect(buildLoginUrl("/")).toBe("/login");
    expect(buildLoginUrl("https://evil.com")).toBe("/login");
    expect(buildLoginUrl(null)).toBe("/login");
    expect(buildLoginUrl(undefined)).toBe("/login");
  });
});

describe("conversation param helpers", () => {
  it("getConversationIdFromSearch reads the value", () => {
    expect(getConversationIdFromSearch(sp("conversation=abc"))).toBe("abc");
    expect(getConversationIdFromSearch(sp(""))).toBeNull();
    expect(getConversationIdFromSearch(sp("conversation="))).toBeNull();
  });

  it("exposes the canonical key name", () => {
    expect(CONVERSATION_PARAM).toBe("conversation");
  });

  it("withConversationParam sets the id and preserves other params", () => {
    const out = withConversationParam(sp("foo=1"), "abc");
    const parsed = new URLSearchParams(out.split("?")[1] ?? "");
    expect(parsed.get("foo")).toBe("1");
    expect(parsed.get(CONVERSATION_PARAM)).toBe("abc");
  });

  it("withConversationParam with null id clears it", () => {
    const out = withConversationParam(sp("conversation=abc&foo=1"), null);
    const parsed = new URLSearchParams(out.split("?")[1] ?? "");
    expect(parsed.has(CONVERSATION_PARAM)).toBe(false);
    expect(parsed.get("foo")).toBe("1");
  });

  it("withConversationParam yields bare '/' when no params remain", () => {
    expect(withConversationParam(sp(""), "abc")).toBe("/?conversation=abc");
    expect(withConversationParam(sp("conversation=abc"), null)).toBe("/");
  });

  it("stripConversationParam removes only conversation, keeping others", () => {
    const out = stripConversationParam(sp("conversation=abc&foo=1&bar=2"));
    const parsed = new URLSearchParams(out.split("?")[1] ?? "");
    expect(parsed.has(CONVERSATION_PARAM)).toBe(false);
    expect(parsed.get("foo")).toBe("1");
    expect(parsed.get("bar")).toBe("2");
  });
});

describe("isInAppReferrer", () => {
  const ORIGIN = "https://app.example.com";
  it("accepts a same-origin absolute referrer", () => {
    expect(isInAppReferrer("https://app.example.com/knowledge-bases", ORIGIN)).toBe(true);
  });
  it("rejects a cross-origin referrer", () => {
    expect(isInAppReferrer("https://google.com/", ORIGIN)).toBe(false);
    expect(isInAppReferrer("http://app.example.com/", ORIGIN)).toBe(false); // scheme mismatch
  });
  it("rejects empty / null referrer (cannot prove in-app)", () => {
    expect(isInAppReferrer("", ORIGIN)).toBe(false);
    expect(isInAppReferrer(null, ORIGIN)).toBe(false);
    expect(isInAppReferrer(undefined, ORIGIN)).toBe(false);
  });
  it("rejects when origin is missing", () => {
    expect(isInAppReferrer("https://app.example.com/", "")).toBe(false);
  });
  it("rejects malformed referrer without throwing", () => {
    expect(isInAppReferrer("javascript:alert(1)", ORIGIN)).toBe(false);
    expect(isInAppReferrer("not-a-url", ORIGIN)).toBe(false);
  });
});

describe("resolveChatHome", () => {
  it("always returns the home route (never a 404-prone sub-page)", () => {
    // A non-home returnTo collapses to bare home instead of /knowledge-bases.
    expect(resolveChatHome(sp("returnTo=/knowledge-bases"))).toBe("/");
    expect(resolveChatHome(sp("returnTo=/admin"))).toBe("/");
    expect(resolveChatHome(sp("returnTo=/settings/models"))).toBe("/");
  });
  it("preserves the conversation when returnTo is home-based", () => {
    expect(resolveChatHome(sp("returnTo=/?conversation=abc"))).toBe("/?conversation=abc");
    expect(resolveChatHome(sp("next=/?conversation=xyz"))).toBe("/?conversation=xyz");
  });
  it("falls back to bare home with no/invalid params", () => {
    expect(resolveChatHome(sp(""))).toBe("/");
    expect(resolveChatHome(sp("returnTo=https://evil.com"))).toBe("/");
    expect(resolveChatHome(sp("returnTo=//evil.com"))).toBe("/");
    expect(resolveChatHome(sp("returnTo=/"))).toBe("/");
  });
  it("ignores a conversation on a non-home returnTo", () => {
    // /knowledge-bases?conversation=abc is NOT home → bare "/".
    expect(resolveChatHome(sp("returnTo=/knowledge-bases?conversation=abc"))).toBe("/");
  });
});
