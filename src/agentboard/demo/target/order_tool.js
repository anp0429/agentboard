// A tiny "agent tool": look up orders for an LLM agent to reason over.
// It contains one PLANTED BUG that reads plausibly on review and leaves
// the existing test suite green — only an edge-case test exposes it.

/** Clamp a page size to the allowed range [lo, hi]. */
export function clampPageSize(n, lo, hi) {
  // BUG: treats `hi` as an exclusive bound — a request for exactly the
  // maximum comes back one short. Reads plausibly; reviews clean.
  return Math.max(lo, Math.min(n, hi - 1));
}

/** Return up to `pageSize` orders matching `status`. */
export function findOrders(orders, status, pageSize) {
  const size = clampPageSize(pageSize, 1, 50);
  return orders.filter((o) => o.status === status).slice(0, size);
}

/** Number of pages needed to show `total` orders at `pageSize` per page. */
export function totalPages(total, pageSize) {
  const size = clampPageSize(pageSize, 1, 50);
  return Math.ceil(total / size);
}
