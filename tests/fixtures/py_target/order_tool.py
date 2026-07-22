"""A tiny "agent tool": look up orders for an LLM agent to reason over.

The Python twin of the packaged JS demo target (src/agentboard/demo/target).
It contains one PLANTED BUG that reads plausibly on review and leaves the
existing test suite green -- only an edge-case test exposes it.
"""


def clamp_page_size(n, lo, hi):
    """Clamp a page size to the allowed range [lo, hi]."""
    # BUG: treats `hi` as an exclusive bound -- a request for exactly the
    # maximum comes back one short. Reads plausibly; reviews clean.
    return max(lo, min(n, hi - 1))


def find_orders(orders, status, page_size):
    """Return up to `page_size` orders matching `status`."""
    size = clamp_page_size(page_size, 1, 50)
    return [o for o in orders if o["status"] == status][:size]


def total_pages(total, page_size):
    """Number of pages needed to show `total` orders at `page_size` per page."""
    size = clamp_page_size(page_size, 1, 50)
    return -(-total // size)
