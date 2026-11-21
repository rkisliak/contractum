import { describe, expect, it } from "vitest";
import { isNotice, NOTICES } from "./notices";

describe("isNotice", () => {
  it.each([...NOTICES])("accepts %s", (value) => {
    expect(isNotice(value)).toBe(true);
  });

  it.each([
    ["Session-Expired"],
    ["session_expired"],
    ["<script>"],
    [""],
    [undefined],
    [null],
    [3],
  ])("rejects %j", (value) => {
    expect(isNotice(value)).toBe(false);
  });

  it("is exactly the four sign-in notices", () => {
    expect([...NOTICES]).toEqual([
      "password-changed",
      "session-expired",
      "signed-out-elsewhere",
      "rotation-restart",
    ]);
  });
});
