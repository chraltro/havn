import { describe, it, expect } from "vitest";
import { api } from "./api";

describe("the API client surface", () => {
  it("ships no batch saveFiles helper", () => {
    // A rename writes every file it touches through POST /rename/apply, which
    // is all-or-none on the server. A second, unused way to write a batch of
    // files from the browser is an invitation to bypass that.
    expect("saveFiles" in api).toBe(false);
  });

  it("keeps the single-file save the editor uses", () => {
    expect(typeof api.saveFile).toBe("function");
  });

  it("keeps the rename endpoints", () => {
    expect(typeof api.planColumnRename).toBe("function");
    expect(typeof api.applyColumnRename).toBe("function");
  });
});
