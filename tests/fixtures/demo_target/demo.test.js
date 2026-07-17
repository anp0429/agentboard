import { describe, expect, test } from "vitest";
import { findOrders } from "./order_tool.js";

const ORDERS = [
  { id: 1, status: "open" },
  { id: 2, status: "open" },
  { id: 3, status: "closed" },
];

describe("order tool", () => {
  test("filters by status", () => {
    expect(findOrders(ORDERS, "open", 10).map((o) => o.id)).toEqual([1, 2]);
  });
});
