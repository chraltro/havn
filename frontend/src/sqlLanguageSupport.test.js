import { describe, it, expect, vi, beforeEach } from "vitest";
import { ensureSqlLanguageSupport, _resetSqlLanguageSupportForTests } from "./sqlLanguageSupport";

beforeEach(() => _resetSqlLanguageSupportForTests());

describe("ensureSqlLanguageSupport", () => {
  it("loads the Editor module once, however many editors mount", async () => {
    const load = vi.fn(async () => ({}));
    await Promise.all([ensureSqlLanguageSupport(load), ensureSqlLanguageSupport(load)]);
    await ensureSqlLanguageSupport(load);
    expect(load).toHaveBeenCalledTimes(1);
  });

  it("tries again on the next mount after a failed fetch, and never rejects", async () => {
    const load = vi.fn()
      .mockRejectedValueOnce(new TypeError("Failed to fetch dynamically imported module"))
      .mockResolvedValueOnce({});
    await expect(ensureSqlLanguageSupport(load)).resolves.toBeUndefined();
    await ensureSqlLanguageSupport(load);
    expect(load).toHaveBeenCalledTimes(2);
  });
});
