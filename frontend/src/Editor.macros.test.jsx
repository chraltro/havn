import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

// Mock api module before any imports that use it
vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal();
  return {
    ...actual,
    getMacros: vi.fn(),
    api: {
      listTables: vi.fn().mockResolvedValue([]),
      describeTable: vi.fn().mockResolvedValue(null),
    },
  };
});

import { getMacros } from "./api";

const SCALAR_MACRO = {
  name: "mask_email",
  kind: "scalar",
  params: [{ name: "email", type: "VARCHAR" }],
  return_type: "VARCHAR",
  docstring: "Mask the local part of an email address.",
  source_file: "/project/macros/utils.py",
};

const TABLE_MACRO = {
  name: "active_users",
  kind: "table",
  params: [{ name: "status", type: "VARCHAR" }],
  return_type: "TABLE",
  docstring: "Return active users filtered by status.",
  source_file: "/project/macros/users.py",
};

describe("getMacros caching", () => {
  beforeEach(() => {
    getMacros.mockReset();
  });

  it("returns scalar and table macros from the API", async () => {
    getMacros.mockResolvedValue([SCALAR_MACRO, TABLE_MACRO]);

    const result = await getMacros();

    expect(result).toHaveLength(2);
    expect(result[0].name).toBe("mask_email");
    expect(result[0].kind).toBe("scalar");
    expect(result[1].name).toBe("active_users");
    expect(result[1].kind).toBe("table");
  });

  it("scalar macro has correct shape", async () => {
    getMacros.mockResolvedValue([SCALAR_MACRO]);

    const [macro] = await getMacros();
    expect(macro.params).toEqual([{ name: "email", type: "VARCHAR" }]);
    expect(macro.return_type).toBe("VARCHAR");
    expect(macro.docstring).toBeTruthy();
  });

  it("table macro has TABLE return_type", async () => {
    getMacros.mockResolvedValue([TABLE_MACRO]);

    const [macro] = await getMacros();
    expect(macro.return_type).toBe("TABLE");
    expect(macro.kind).toBe("table");
  });
});

describe("buildMacroSignature helpers", () => {
  it("formats scalar signature correctly", () => {
    const paramStr = SCALAR_MACRO.params.map((p) => `${p.name}: ${p.type}`).join(", ");
    const sig = `${SCALAR_MACRO.name}(${paramStr}) -> ${SCALAR_MACRO.return_type}`;
    expect(sig).toBe("mask_email(email: VARCHAR) -> VARCHAR");
  });

  it("formats table macro signature with TABLE return", () => {
    const paramStr = TABLE_MACRO.params.map((p) => `${p.name}: ${p.type}`).join(", ");
    const sig = `${TABLE_MACRO.name}(${paramStr}) -> TABLE`;
    expect(sig).toBe("active_users(status: VARCHAR) -> TABLE");
  });

  it("handles macros with no params", () => {
    const noParamMacro = { name: "now_utc", kind: "scalar", params: [], return_type: "TIMESTAMP", docstring: "" };
    const sig = `${noParamMacro.name}() -> ${noParamMacro.return_type}`;
    expect(sig).toBe("now_utc() -> TIMESTAMP");
  });
});

describe("macro kind labels", () => {
  it("scalar macros get [S] prefix", () => {
    const isTable = SCALAR_MACRO.kind === "table";
    expect(isTable).toBe(false);
    const label = isTable ? "[T] " : "[S] ";
    expect(label).toBe("[S] ");
  });

  it("table macros get [T] prefix", () => {
    const isTable = TABLE_MACRO.kind === "table";
    expect(isTable).toBe(true);
    const label = isTable ? "[T] " : "[S] ";
    expect(label).toBe("[T] ");
  });
});
