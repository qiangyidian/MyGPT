import { redirect } from "next/navigation";

import { RETURN_TO_PARAM, sanitizeInternalPath } from "@/lib/navigation";

/**
 * `/settings` index. There is no settings landing page — the canonical entry is
 * `/settings/models`. Redirect on the server (no client effect, no flash),
 * forwarding a validated `returnTo` so the "返回对话" target survives.
 *
 * Server component: `redirect()` throws `NEXT_REDIRECT` during render.
 */
export default function SettingsIndexPage({
  searchParams,
}: {
  searchParams?: Record<string, string | string[]>;
}) {
  const raw = searchParams?.[RETURN_TO_PARAM];
  const value = Array.isArray(raw) ? raw[0] : raw;
  const safeReturnTo = sanitizeInternalPath(value);

  if (safeReturnTo) {
    const qs = new URLSearchParams({ [RETURN_TO_PARAM]: safeReturnTo }).toString();
    redirect(`/settings/models?${qs}`);
  }
  redirect("/settings/models");
}
