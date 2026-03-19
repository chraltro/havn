const SCHEMA_ORDER = { landing: 0, bronze: 1, silver: 2, gold: 3 };
export const schemaWeight = (name) => SCHEMA_ORDER[(name || '').toLowerCase()] ?? 99;
export const schemaCompare = (a, b) => {
  const wa = schemaWeight(a), wb = schemaWeight(b);
  if (wa !== wb) return wa - wb;
  return a.localeCompare(b);
};
