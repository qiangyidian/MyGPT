import { redirect } from "next/navigation";

/**
 * Legacy /knowledge-bases[/id] → /settings/knowledge-bases[/id].
 *
 * KB pages moved under /settings to share its layout (sidebar stays mounted,
 * navigation between settings sections stops feeling like a full-page jump).
 * The old paths were reachable from bookmarks / browser history, so they
 * redirect, preserving the query string (returnTo etc.).
 */
export default function LegacyKnowledgeBasesRedirect({
  params,
  searchParams,
}: {
  params: { slug?: string[] };
  searchParams?: Record<string, string | string[] | undefined>;
}) {
  const slug = params.slug?.length ? `/${params.slug.join("/")}` : "";
  const qs = searchParams
    ? `?${new URLSearchParams(
        Object.entries(searchParams).flatMap(([k, v]) =>
          Array.isArray(v) ? v.map((vv) => [k, vv] as [string, string]) : v != null ? [[k, v] as [string, string]] : []
        )
      ).toString()}`
    : "";
  redirect(`/settings/knowledge-bases${slug}${qs}`);
}
