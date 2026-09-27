import { redirect } from "next/navigation";

/**
 * Legacy /knowledge-bases[/id] → /settings/knowledge-bases[/id].
 *
 * KB pages moved under /settings to share its layout (sidebar stays mounted,
 * navigation between settings sections stops feeling like a full-page jump).
 * The old paths were reachable from bookmarks / browser history, so they
 * redirect, preserving the query string (returnTo etc.).
 */
export default async function LegacyKnowledgeBasesRedirect({
  params,
  searchParams,
}: {
  params: Promise<{ slug?: string[] }>;
  searchParams?: Promise<Record<string, string | string[] | undefined>>;
}) {
  const [{ slug: routeSlug }, resolvedSearchParams] = await Promise.all([
    params,
    searchParams ?? Promise.resolve({}),
  ]);
  const slug = routeSlug?.length ? `/${routeSlug.join("/")}` : "";
  const qs = Object.keys(resolvedSearchParams).length
    ? `?${new URLSearchParams(
        Object.entries(resolvedSearchParams).flatMap(([k, v]) =>
          Array.isArray(v) ? v.map((vv) => [k, vv] as [string, string]) : v != null ? [[k, v] as [string, string]] : []
        )
      ).toString()}`
    : "";
  redirect(`/settings/knowledge-bases${slug}${qs}`);
}
