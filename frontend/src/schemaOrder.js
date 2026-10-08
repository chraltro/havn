const SCHEMA_ORDER = { landing: 0, bronze: 1, silver: 2, gold: 3 };
export const schemaWeight = (name) => SCHEMA_ORDER[(name || '').toLowerCase()] ?? 99;
export const schemaCompare = (a, b) => {
  const wa = schemaWeight(a), wb = schemaWeight(b);
  if (wa !== wb) return wa - wb;
  return a.localeCompare(b);
};

// Schemas that hold introspection / bookkeeping rather than user data.
// They stay visible (so users can browse them) but are dimmed and start
// collapsed: _havn (havn metadata), main (DuckDB default empty schema),
// information_schema (SQL standard catalog), and any catalog name that
// starts with __ (DuckLake's underlying metadata catalog).
export function isSystemSchema(name) {
  if (!name) return false;
  if (name === "_havn" || name === "main") return true;
  if (name === "information_schema") return true;
  if (name.startsWith("__")) return true;
  if (name.includes(".")) {
    return name.split(".").some((p) => isSystemSchema(p));
  }
  return false;
}
