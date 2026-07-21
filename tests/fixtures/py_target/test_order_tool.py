"""The fixture repo's own suite -- green, and blind to the planted bug."""

from order_tool import find_orders

ORDERS = [
    {"id": 1, "status": "open"},
    {"id": 2, "status": "open"},
    {"id": 3, "status": "closed"},
]


def test_filters_by_status():
    assert [o["id"] for o in find_orders(ORDERS, "open", 10)] == [1, 2]
