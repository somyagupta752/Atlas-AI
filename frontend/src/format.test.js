import { describe, expect, it } from "vitest";
import { bestIndex, counts, metricValue, money, pct, tri } from "./format";

describe("format", () => {
  it("shows unknown as n/a, never zero", () => {
    expect(pct(null)).toBe("n/a");
    expect(pct(0)).toBe("0.0%");
    expect(money(undefined)).toBe("n/a");
    expect(metricValue({ name: "final_verified_rate", value: null })).toBe("n/a");
    expect(tri(null)).toBe("unknown");
    expect(tri(false)).toBe("no");
  });
  it("formats by metric kind and keeps the counts", () => {
    expect(metricValue({ name: "final_verified_rate", value: 0.5 })).toBe("50.0%");
    expect(metricValue({ name: "total_cost_usd", value: 0.1234 })).toBe("$0.1234");
    expect(counts({ numerator: 3, denominator: 8 })).toBe("3 / 8");
  });
  it("highlights a best value only when runs differ", () => {
    expect(bestIndex([{ value: 0.5 }, { value: 0.8 }], true)).toBe(1);
    expect(bestIndex([{ value: 3 }, { value: 2 }], false)).toBe(1);
    expect(bestIndex([{ value: 0.5 }, { value: 0.5 }], true)).toBeNull();
    expect(bestIndex([{ value: null }, { value: null }], true)).toBeNull();
    expect(bestIndex([{ value: null }, { value: 0.2 }], true)).toBe(1);
  });
});
