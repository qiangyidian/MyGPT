/**
 * Route-level loading UI. Next.js app router shows this inside the nearest
 * Suspense boundary while a route segment's data/code is loading, so navigation
 * between pages shows a centered spinner instead of a blank frame.
 */
export default function Loading() {
  return (
    <div className="flex h-full min-h-[40vh] items-center justify-center">
      <div
        className="h-6 w-6 animate-spin rounded-full border-2 border-muted border-t-primary"
        role="status"
        aria-label="加载中"
      />
    </div>
  );
}
