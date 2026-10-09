// Autocomplete for the Data > Query editor (a plain textarea, not Monaco).
//
// buildCompletions() is pure apart from the injected column lookup, so the
// rules can be tested without a browser. Each item says how many characters
// before the cursor it replaces: completing `c.cus` replaces only `cus`, so
// the `c.` qualifier stays.

// SQL keywords that can follow a table name but are NOT aliases.
const ALIAS_STOPWORDS = new Set([
  "ON", "WHERE", "AND", "OR", "SET", "LEFT", "RIGHT", "INNER", "OUTER",
  "CROSS", "FULL", "JOIN", "GROUP", "ORDER", "HAVING", "LIMIT", "UNION",
  "EXCEPT", "INTERSECT", "USING", "QUALIFY", "WINDOW", "OFFSET", "FETCH",
  "TABLESAMPLE", "AS", "NATURAL", "LATERAL", "PIVOT", "UNPIVOT", "ASOF",
  "POSITIONAL", "ANTI", "SEMI", "SELECT", "FROM", "RETURNING",
]);

const MAX_ITEMS = 12;

/**
 * Tables named after FROM/JOIN, with their alias if one is given.
 * An unqualified name (`FROM customers c`) resolves through `tables` when
 * exactly one schema has a table of that name.
 */
export function extractTableRefs(sqlText, tables) {
  const refs = [];
  // Names in any script, matching the word rule in buildCompletions.
  const re = /\b(?:FROM|JOIN)\s+((?:[\p{L}\p{N}_]+\.)?[\p{L}\p{N}_]+)(?:\s+(?:AS\s+)?([\p{L}\p{N}_]+))?/giu;
  let m;
  while ((m = re.exec(sqlText)) !== null) {
    let [schema, table] = m[1].includes(".") ? m[1].split(".") : [null, m[1]];
    if (!schema) {
      const hits = tables.filter((t) => t.name.toLowerCase() === table.toLowerCase());
      if (hits.length !== 1) continue;
      schema = hits[0].schema;
      table = hits[0].name;
    }
    const aliasTok = m[2];
    const alias = aliasTok && !ALIAS_STOPWORDS.has(aliasTok.toUpperCase()) ? aliasTok.toLowerCase() : null;
    refs.push({ schema, table, alias });
  }
  return refs;
}

/** The ref a `qualifier.` names: an alias, or the bare name of an unaliased table. */
function refForQualifier(qualifier, refs) {
  return (
    refs.find((r) => r.alias === qualifier) ||
    refs.find((r) => !r.alias && r.table.toLowerCase() === qualifier)
  );
}

/** True when the cursor sits inside a '...' string, a -- comment or a block comment. */
export function inStringOrComment(beforeCursor) {
  let state = null; // null | "'" | "--" | "/*"
  for (let i = 0; i < beforeCursor.length; i++) {
    const ch = beforeCursor[i];
    const next = beforeCursor[i + 1];
    if (state === "'") { if (ch === "'") state = null; }
    else if (state === "--") { if (ch === "\n") state = null; }
    else if (state === "/*") { if (ch === "*" && next === "/") { state = null; i++; } }
    else if (ch === "'") state = "'";
    else if (ch === "-" && next === "-") { state = "--"; i++; }
    else if (ch === "/" && next === "*") { state = "/*"; i++; }
  }
  return state !== null;
}

// Words that are already a whole piece of SQL. Typing one still shows the list
// (`order` is on the way to `order_id`), but Enter after it makes a new line
// instead of swapping it for a name that starts with it (`NULLS LAST` + Enter
// must not become `NULLS last_updated`). Tab, or picking with the arrows,
// still accepts.
const KEYWORDS = new Set([
  "SELECT", "FROM", "WHERE", "AND", "OR", "NOT", "IN", "IS", "NULL", "AS", "ON",
  "BY", "GROUP", "ORDER", "HAVING", "LIMIT", "OFFSET", "JOIN", "LEFT", "RIGHT",
  "INNER", "OUTER", "FULL", "CROSS", "UNION", "ALL", "DISTINCT", "CASE", "WHEN",
  "THEN", "ELSE", "END", "ASC", "DESC", "WITH", "LIKE", "ILIKE", "BETWEEN",
  "EXISTS", "TRUE", "FALSE", "QUALIFY", "USING", "OVER", "PARTITION", "CAST",
  "NULLS", "FIRST", "LAST", "SET", "INTO", "VALUES", "INTERVAL", "ROWS", "RANGE",
  "ROW", "PRECEDING", "FOLLOWING", "UNBOUNDED", "CURRENT", "FILTER", "WINDOW",
  "EXCEPT", "INTERSECT", "ANY", "SOME", "ASOF", "ANTI", "SEMI", "NATURAL",
  "LATERAL", "PIVOT", "UNPIVOT", "RECURSIVE", "EXCLUDE", "REPLACE", "INSERT",
  "UPDATE", "DELETE", "CREATE", "TABLE", "VIEW", "DROP", "ALTER", "DEFAULT",
  "FETCH", "NEXT", "ONLY", "TOP", "SAMPLE", "TABLESAMPLE", "RETURNING", "DESCRIBE",
  "SUMMARIZE", "EXPLAIN", "ANALYZE", "TRY_CAST", "COLLATE", "ESCAPE", "GLOB",
  "SIMILAR", "TO", "AT", "ZONE",
]);

function columnItems(cols, partial, replace, sourceLabel) {
  return cols
    .filter((c) => c.name.toLowerCase().startsWith(partial))
    .map((c) => ({
      label: c.name,
      detail: sourceLabel ? `${c.type}  ${sourceLabel}` : c.type,
      insert: c.name,
      kind: "column",
      replace,
    }));
}

const NAME = "[\\p{L}\\p{N}_]+";
// `FROM x` / `JOIN sch.x`: the word being typed names a table.
const TABLE_SLOT = new RegExp(`\\b(?:FROM|JOIN)\\s+${NAME}$`, "iu");
// `FROM sch.x c` / `JOIN x AS c`: the word being typed is the table's alias,
// a name the user is making up, so there is nothing to suggest.
const ALIAS_SLOT = new RegExp(`\\b(?:FROM|JOIN)\\s+(?:${NAME}\\.)?${NAME}\\s+(?:AS\\s+)?${NAME}$`, "iu");

/**
 * Suggestions for the token before `cursor`.
 * `getColumns(schema, table)` resolves to the table's columns ([{name, type}]).
 * Returns [] when there is nothing worth showing. Items carry `typed` (the
 * text they replace, to re-check it at accept time) and `soft` (the typed
 * word is already a keyword or alias, so Enter should not accept).
 */
export async function buildCompletions(value, cursor, tables, getColumns) {
  const beforeCursor = value.substring(0, cursor);
  if (inStringOrComment(beforeCursor)) return [];
  // Letters in any script: in `førs` the word is all four letters, not `rs`.
  const tokenMatch = beforeCursor.match(/[\p{L}\p{N}_.]+$/u);
  const token = tokenMatch ? tokenMatch[0] : "";
  if (!token || /^\d/.test(token)) return [];
  if (ALIAS_SLOT.test(beforeCursor)) return [];

  const refs = extractTableRefs(value, tables);
  const parts = token.toLowerCase().split(".");
  const partial = parts[parts.length - 1];
  let items = [];

  if (parts.length === 1) {
    const tableItems = tables
      .filter((t) =>
        t.schema.toLowerCase().startsWith(partial) ||
        t.name.toLowerCase().startsWith(partial)
      )
      .slice(0, 6)
      .map((t) => ({
        label: `${t.schema}.${t.name}`, detail: t.type || "", insert: `${t.schema}.${t.name}`,
        kind: "table", replace: token.length,
      }));
    if (TABLE_SLOT.test(beforeCursor)) {
      // After FROM/JOIN only a table fits: `JOIN cust` is `gold.customers`, not `customer_id`.
      items = tableItems;
    } else {
      // Elsewhere: columns of the tables in the query first, then table names.
      const seen = new Map();
      for (const r of refs) {
        const cols = await getColumns(r.schema, r.table);
        for (const it of columnItems(cols, partial, token.length, r.alias || r.table)) {
          const key = it.insert.toLowerCase();
          if (!seen.has(key)) seen.set(key, it);
        }
      }
      items = [...[...seen.values()].slice(0, 8), ...tableItems];
    }
  } else if (parts.length === 2) {
    const qualifier = parts[0];
    const ref = refForQualifier(qualifier, refs);
    if (ref) {
      items = columnItems(await getColumns(ref.schema, ref.table), partial, partial.length);
    } else {
      // schema.partial: complete the table name, keep "schema."
      items = tables
        .filter((t) => t.schema.toLowerCase() === qualifier && t.name.toLowerCase().startsWith(partial))
        .map((t) => ({
          label: t.name, detail: t.type || "", insert: t.name, kind: "table", replace: partial.length,
        }));
    }
  } else if (parts.length === 3) {
    // schema.table.partial
    const t = tables.find((x) => x.schema.toLowerCase() === parts[0] && x.name.toLowerCase() === parts[1]);
    if (t) items = columnItems(await getColumns(t.schema, t.name), partial, partial.length);
  }

  // A word that is already a complete name gets no list at all, so Enter after
  // `gold.orders` makes a new line instead of turning it into `gold.orders_archive`.
  const typed = (n) => beforeCursor.slice(cursor - n).toLowerCase();
  if (items.some((it) => it.insert.toLowerCase() === typed(it.replace))) return [];
  const soft = parts.length === 1 && (KEYWORDS.has(token.toUpperCase()) || refs.some((r) => r.alias === partial));
  return items.slice(0, MAX_ITEMS).map((it) => ({ ...it, typed: typed(it.replace), soft }));
}

/** Whether `item`, built for an earlier keystroke, still matches the text before `cursor`. */
export function completionFits(value, cursor, item) {
  return item.typed === undefined || value.slice(Math.max(0, cursor - item.replace), cursor).toLowerCase() === item.typed;
}

/** Apply `item` at `cursor`: returns the new text and caret position. */
export function applyCompletionText(value, cursor, item) {
  const start = Math.max(0, cursor - item.replace);
  const next = value.substring(0, start) + item.insert + value.substring(cursor);
  return { value: next, cursor: start + item.insert.length };
}

const MIRROR_PROPS = [
  "direction", "boxSizing", "width", "height", "overflowX", "overflowY",
  "borderTopWidth", "borderRightWidth", "borderBottomWidth", "borderLeftWidth", "borderStyle",
  "paddingTop", "paddingRight", "paddingBottom", "paddingLeft",
  "fontStyle", "fontVariant", "fontWeight", "fontStretch", "fontSize", "lineHeight", "fontFamily",
  "fontFeatureSettings", "fontVariantLigatures", "textAlign", "textTransform", "textIndent",
  "letterSpacing", "wordSpacing", "tabSize",
];

/**
 * Pixel position of character `position` inside a textarea, relative to the
 * textarea's top-left (ignoring its scroll). Measured with an off-screen copy
 * of the textarea that wraps text the same way.
 */
export function caretCoordinates(el, position) {
  const computed = window.getComputedStyle(el);
  const div = document.createElement("div");
  const s = div.style;
  for (const p of MIRROR_PROPS) s[p] = computed[p];
  s.position = "absolute";
  s.visibility = "hidden";
  s.top = "0";
  s.left = "-9999px";
  s.whiteSpace = "pre-wrap";
  s.overflowWrap = "break-word";
  s.overflow = "hidden";
  // The textarea's scrollbar takes width the copy doesn't have.
  s.width = `${el.clientWidth + (parseFloat(computed.borderLeftWidth) || 0) + (parseFloat(computed.borderRightWidth) || 0)}px`;
  div.textContent = el.value.substring(0, position);
  const span = document.createElement("span");
  span.textContent = el.value.substring(position) || ".";
  div.appendChild(span);
  document.body.appendChild(div);
  const lineHeight = parseFloat(computed.lineHeight) || (parseFloat(computed.fontSize) || 13) * 1.4;
  const coords = { top: span.offsetTop, left: span.offsetLeft, height: lineHeight };
  document.body.removeChild(div);
  return coords;
}
