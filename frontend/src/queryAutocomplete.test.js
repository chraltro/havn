import { describe, it, expect } from "vitest";
import { buildCompletions, applyCompletionText, extractTableRefs, completionFits } from "./queryAutocomplete";

const TABLES = [
  { schema: "landing", name: "customers", type: "table" },
  { schema: "landing", name: "orders", type: "table" },
  { schema: "gold", name: "customer_summary", type: "view" },
];
const COLS = {
  "landing.customers": [{ name: "customer_id", type: "INTEGER" }, { name: "name", type: "VARCHAR" }],
  "landing.orders": [{ name: "order_id", type: "INTEGER" }, { name: "customer_id", type: "INTEGER" }],
};
const getColumns = async (s, t) => COLS[`${s}.${t}`] || [];

async function complete(textWithCaret) {
  const cursor = textWithCaret.indexOf("|");
  const value = textWithCaret.replace("|", "");
  const items = await buildCompletions(value, cursor, TABLES, getColumns);
  return { value, cursor, items };
}

function accept(state, insert) {
  const item = state.items.find((i) => i.insert === insert);
  expect(item, `no suggestion ${insert}`).toBeTruthy();
  return applyCompletionText(state.value, state.cursor, item).value;
}

describe("query autocomplete", () => {
  it("keeps the alias when completing alias.column", async () => {
    const st = await complete("SELECT c.cus| FROM landing.customers c");
    expect(accept(st, "customer_id")).toBe("SELECT c.customer_id FROM landing.customers c");
  });

  it("keeps the alias right after the dot", async () => {
    const st = await complete("SELECT o.| FROM landing.orders AS o");
    expect(st.items.map((i) => i.insert)).toEqual(["order_id", "customer_id"]);
    expect(accept(st, "order_id")).toBe("SELECT o.order_id FROM landing.orders AS o");
  });

  it("completes schema.table without repeating the schema", async () => {
    const st = await complete("SELECT * FROM landing.cu|");
    expect(accept(st, "customers")).toBe("SELECT * FROM landing.customers");
  });

  it("completes schema.table.column", async () => {
    const st = await complete("SELECT landing.orders.ord| FROM landing.orders");
    expect(accept(st, "order_id")).toBe("SELECT landing.orders.order_id FROM landing.orders");
  });

  it("uses an unaliased table name as a qualifier", async () => {
    const st = await complete("SELECT orders.cu| FROM landing.orders");
    expect(accept(st, "customer_id")).toBe("SELECT orders.customer_id FROM landing.orders");
  });

  it("resolves unqualified table names that are unique", () => {
    expect(extractTableRefs("FROM customers c JOIN orders WHERE", TABLES)).toEqual([
      { schema: "landing", table: "customers", alias: "c" },
      { schema: "landing", table: "orders", alias: null },
    ]);
  });

  it("reads table names and aliases with Nordic letters whole", () => {
    expect(extractTableRefs("FROM gold.kunder_før kø WHERE", TABLES)).toEqual([
      { schema: "gold", table: "kunder_før", alias: "kø" },
    ]);
  });

  it("does not complete an alias being typed, and marks a declared alias soft", async () => {
    expect((await complete("SELECT * FROM landing.customers c|")).items).toEqual([]);
    expect((await complete("SELECT * FROM landing.customers AS cu|")).items).toEqual([]);
    const st = await complete("SELECT * FROM landing.orders o JOIN landing.customers c ON o|");
    expect(st.items.length).toBeGreaterThan(0); // `o` is also on the way to `order_id`
    expect(st.items.every((i) => i.soft)).toBe(true);
  });

  it("does not take a keyword for an alias", () => {
    expect(extractTableRefs("FROM landing.orders WHERE x", TABLES)[0].alias).toBeNull();
  });

  it("offers columns of the query's tables, then tables, for a plain word", async () => {
    const st = await complete("SELECT cu| FROM landing.customers");
    expect(st.items[0]).toMatchObject({ insert: "customer_id", kind: "column" });
    expect(st.items.some((i) => i.insert === "gold.customer_summary")).toBe(true);
  });

  it("does not suggest what is already typed, so Enter makes a new line", async () => {
    const st = await complete("SELECT * FROM landing.customers|");
    expect(st.items).toEqual([]);
  });

  it("stays quiet inside strings, comments and numbers", async () => {
    expect((await complete("SELECT * FROM landing.customers WHERE name = 'cu|")).items).toEqual([]);
    expect((await complete("-- landing.cu|")).items).toEqual([]);
    expect((await complete("SELECT * FROM landing.orders LIMIT 10|")).items).toEqual([]);
  });

  it("stays quiet inside block comments and multi-line strings", async () => {
    expect((await complete("/* landing.cu|")).items).toEqual([]);
    expect((await complete("SELECT 'abc\nlanding.cu|")).items).toEqual([]);
    expect((await complete("/* x */ SELECT * FROM landing.cu|")).items.length).toBeGreaterThan(0);
  });

  it("marks suggestions after a whole keyword soft, so Enter makes a new line", async () => {
    const st = await buildCompletions("WHERE a = 1 OR", 14, TABLES, getColumns);
    expect(st.length).toBeGreaterThan(0); // `or` is on the way to `orders`
    expect(st.every((i) => i.soft)).toBe(true);
    const cols = async () => [{ name: "last_updated", type: "TIMESTAMP" }];
    const nulls = await buildCompletions("SELECT * FROM landing.orders ORDER BY x NULLS LAST", 50, TABLES, cols);
    expect(nulls.every((i) => i.soft)).toBe(true);
  });

  it("does not mark an ordinary partial word soft", async () => {
    const st = await complete("SELECT cust| FROM landing.customers");
    expect(st.items[0]).toMatchObject({ insert: "customer_id", soft: false });
  });

  it("offers only tables right after FROM or JOIN", async () => {
    const st = await complete("SELECT * FROM landing.orders o JOIN cust|");
    expect(st.items.map((i) => i.insert)).toEqual(["landing.customers", "gold.customer_summary"]);
  });

  it("treats Nordic letters as part of the word", async () => {
    // With an ASCII-only word match the token would be "rs" and offer rs_total.
    const cols = async () => [{ name: "rs_total", type: "INTEGER" }, { name: "førsteår", type: "INTEGER" }];
    const text = "SELECT førs FROM landing.customers";
    const items = await buildCompletions(text, 11, TABLES, cols);
    expect(items.map((i) => i.insert)).toEqual(["førsteår"]);
    expect(applyCompletionText(text, 11, items[0]).value).toBe("SELECT førsteår FROM landing.customers");
  });

  it("shows nothing when the word is already a complete name, even if longer names exist", async () => {
    const tables = [...TABLES, { schema: "landing", name: "orders_archive" }];
    const st = await buildCompletions("FROM landing.orders", 19, tables, getColumns);
    expect(st).toEqual([]);
  });

  it("refuses a suggestion built for an earlier keystroke", async () => {
    const st = await complete("SELECT c.customer_i| FROM landing.customers c");
    const item = st.items.find((i) => i.insert === "customer_id");
    expect(completionFits(st.value, st.cursor, item)).toBe(true);
    const later = st.value.slice(0, st.cursor) + "d" + st.value.slice(st.cursor);
    expect(completionFits(later, st.cursor + 1, item)).toBe(false);
  });

  it("replaces only the typed part and keeps text after the cursor", () => {
    const item = { insert: "customer_id", replace: 3 };
    expect(applyCompletionText("c.cus, x", 5, item)).toEqual({ value: "c.customer_id, x", cursor: 13 });
  });
});
