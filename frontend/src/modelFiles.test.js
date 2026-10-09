import { describe, expect, it } from "vitest";
import {
  isModelFile,
  isPythonModelPath,
  isPythonModelSource,
  modelNameFromPath,
  pythonModelTemplate,
} from "./modelFiles";

describe("modelFiles", () => {
  it("recognises Python model sources", () => {
    expect(isPythonModelSource("from havn import model\n\n@model(materialized='table')\ndef x(db):\n  pass\n")).toBe(true);
    expect(isPythonModelSource("import havn\n@havn.model\ndef x(db):\n  pass\n")).toBe(true);
    expect(isPythonModelSource("def model(ref):\n  return ref('a.b')\n")).toBe(true);
    expect(isPythonModelSource("FACTOR = 2\ndef helper():\n  return FACTOR\n")).toBe(false);
  });

  it("treats only non-helper .py files under transform/ as model paths", () => {
    expect(isPythonModelPath("transform/silver/x.py")).toBe(true);
    expect(isPythonModelPath("transform\\silver\\x.py")).toBe(true);
    expect(isPythonModelPath("transform/silver/_helpers.py")).toBe(false);
    expect(isPythonModelPath("ingest/x.py")).toBe(false);
  });

  it("decides model files by extension and content", () => {
    expect(isModelFile("transform/silver/x.sql")).toBe(true);
    expect(isModelFile("transform/silver/x.py", "def model(db):\n  pass\n")).toBe(true);
    expect(isModelFile("transform/silver/utils.py", "X = 1\n")).toBe(false);
    expect(isModelFile("transform/silver/utils.py")).toBe(true);
    expect(isModelFile("ingest/load.py", "def model(db): pass")).toBe(false);
    expect(isModelFile(null)).toBe(false);
  });

  it("derives the conventional model name", () => {
    expect(modelNameFromPath("transform/silver/x.py")).toBe("silver.x");
    expect(modelNameFromPath("transform/gold/y.sql")).toBe("gold.y");
  });

  it("writes a template that is itself a model", () => {
    const text = pythonModelTemplate("transform/gold/fresh.py");
    expect(text).toContain("def fresh(db, ref):");
    expect(isPythonModelSource(text)).toBe(true);
  });
});
