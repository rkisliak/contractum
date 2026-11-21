import { describe, expect, it } from "vitest";
import { fromDisplay, humanDuration, rangeHint, toDisplay } from "./units";

describe("settings units", () => {
  it("shows seconds as hours and minutes and days as they are", () => {
    expect(toDisplay(604800, "hours")).toBe("168");
    expect(toDisplay(43200, "minutes")).toBe("720");
    expect(toDisplay(5400, "hours")).toBe("1.5");
    expect(toDisplay(365, "days")).toBe("365");
    expect(toDisplay(5, "count")).toBe("5");
  });

  it("turns an input's text back into the stored number", () => {
    expect(fromDisplay("168", "hours")).toBe(604800);
    expect(fromDisplay("1.5", "hours")).toBe(5400);
    expect(fromDisplay(" 15 ", "minutes")).toBe(900);
    expect(fromDisplay("90", "days")).toBe(90);
    expect(fromDisplay("7", "count")).toBe(7);
  });

  it("round-trips every stored bound", () => {
    for (const [seconds, unit] of [
      [3600, "hours"],
      [7776000, "hours"],
      [900, "minutes"],
      [604800, "minutes"],
      [60, "minutes"],
      [86400, "minutes"],
    ] as const) {
      expect(fromDisplay(toDisplay(seconds, unit), unit)).toBe(seconds);
    }
  });

  it("refuses text that is not a plain number", () => {
    for (const text of ["", " ", "-1", "1e3", "1,5", "abc", "1.234", "0x10"]) {
      expect(fromDisplay(text, "hours")).toBeNull();
    }
    expect(fromDisplay("1.5", "days")).toBeNull();
    expect(fromDisplay("1.5", "count")).toBeNull();
    expect(fromDisplay("123456789", "count")).toBeNull();
  });

  it("describes a range and a duration", () => {
    expect(rangeHint(3600, 7776000, "hours")).toBe("1 to 2160 hours");
    expect(rangeHint(3, 20, "count")).toBe("3 to 20");
    expect(rangeHint(1, 3650, "days")).toBe("1 to 3650 days");
    expect(humanDuration(604800)).toBe("7 days");
    expect(humanDuration(86400)).toBe("1 day");
    expect(humanDuration(43200)).toBe("12 h");
    expect(humanDuration(900)).toBe("15 min");
    expect(humanDuration(90)).toBe("90 s");
  });
});
