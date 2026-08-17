import { describe, expect, it } from "vitest";

import { sourceUrl, THIRD_PARTY_NOTICES_URL } from "./legal";

describe("legal source links", () => {
  it("links a published image to its immutable source revision", () => {
    const revision = "61e263ba058c5815bfc011e2b6bca2321bb59b29";
    expect(sourceUrl("https://github.com/unixfg/radiacode/", revision)).toBe(
      `https://github.com/unixfg/radiacode/tree/${revision}`,
    );
  });

  it("falls back safely for local or invalid build metadata", () => {
    expect(sourceUrl(undefined, "main")).toBe("https://github.com/unixfg/radiacode");
    expect(sourceUrl("javascript:alert(1)", undefined)).toBe(
      "https://github.com/unixfg/radiacode",
    );
    expect(THIRD_PARTY_NOTICES_URL).toBe("/assets/THIRD_PARTY_NOTICES.md");
  });
});
