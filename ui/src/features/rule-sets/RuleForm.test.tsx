// The rule form as a component: every field reports its edit, server problems show under the
// field they concern, and a checked rule lists each fixture's outcome.
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import type { CheckProblem } from "./check";
import { RuleForm } from "./RuleForm";
import { emptyForm, type RuleFormValues } from "./ruleValues";

function Harness({
  onValues,
  initial = emptyForm(),
}: {
  onValues: (values: RuleFormValues) => void;
  initial?: RuleFormValues;
}) {
  const [values, setValues] = useState(initial);
  return (
    <RuleForm
      values={values}
      onChange={(next) => {
        setValues(next);
        onValues(next);
      }}
    />
  );
}

describe("rule form", () => {
  it("reports every edited field", async () => {
    const user = userEvent.setup();
    const onValues = vi.fn();
    render(<Harness onValues={onValues} />);
    await user.type(screen.getByLabelText("Key"), "k");
    await user.type(screen.getByLabelText(/^Description/), "d");
    await user.clear(screen.getByLabelText("Target (JSONPath)"));
    await user.type(screen.getByLabelText("Target (JSONPath)"), "$.x");
    await user.type(screen.getByLabelText("Condition (CEL)"), "true");
    await user.selectOptions(screen.getByLabelText("Severity"), "info");
    await user.selectOptions(screen.getByLabelText("Match"), "any");
    await user.selectOptions(screen.getByLabelText("If none match"), "fail");
    await user.type(screen.getByLabelText("Message"), "m");
    await user.clear(screen.getByLabelText("Pass fixtures (JSON)"));
    await user.type(screen.getByLabelText("Pass fixtures (JSON)"), "[[]");
    await user.clear(screen.getByLabelText("Fail fixtures (JSON)"));
    await user.type(screen.getByLabelText("Fail fixtures (JSON)"), "[[]");
    expect(onValues).toHaveBeenLastCalledWith({
      key: "k",
      description: "d",
      target: "$.x",
      condition: "true",
      severity: "info",
      match: "any",
      on_empty: "fail",
      message: "m",
      pass: "[]",
      fail: "[]",
    });
  });

  it("is read-only without a change handler and shows problems by field", () => {
    const problems: CheckProblem[] = [
      { code: "target_invalid", detail: "unexpected token" },
      { code: "compile_error", detail: "Syntax error at 1:4" },
      {
        code: "fixture_misbehaved",
        detail: "evaluated to true",
        fixture_kind: "fail",
        fixture_index: 0,
      },
      { code: "scale_probe_failed", detail: null },
    ];
    render(
      <RuleForm
        values={{ ...emptyForm(), key: "k" }}
        problems={problems}
        readOnly
        keyLocked
      />,
    );
    expect(screen.getByLabelText("Key")).toHaveAttribute("readonly");
    expect(
      screen.getByText(
        "The key identifies the rule in reports; it cannot change once saved.",
      ),
    ).toBeVisible();
    const target = screen.getByRole("list", { name: "Target problems" });
    expect(within(target).getByText("unexpected token")).toBeVisible();
    const condition = screen.getByRole("list", { name: "Condition problems" });
    expect(within(condition).getByText("Syntax error at 1:4")).toBeVisible();
    // A misbehaving fixture is worded once; its raw detail is not repeated.
    const fixtures = screen.getByRole("list", { name: "Fixture problems" });
    expect(
      within(fixtures).getByText("Fail fixture 1: expected fail, got true"),
    ).toBeVisible();
    expect(within(fixtures).queryByText("evaluated to true")).toBeNull();
    const rule = screen.getByRole("list", { name: "Rule problems" });
    expect(
      within(rule).getByText("The rule does not scale to large contracts"),
    ).toBeVisible();
    expect(screen.getByLabelText("Severity")).toBeDisabled();
  });

  it("lists each checked fixture's outcome and skips text that is not a list", () => {
    const { rerender } = render(
      <RuleForm
        values={{
          ...emptyForm(),
          pass: '[{"document": {}}, {"document": {}}]',
          fail: '{"document": {}}',
        }}
        problems={[
          {
            code: "fixture_misbehaved",
            fixture_kind: "pass",
            fixture_index: 1,
          },
          { code: "fixture_too_large", fixture_kind: "pass" },
        ]}
        checked
      />,
    );
    const pass = screen.getByRole("list", { name: "pass fixture results" });
    expect(within(pass).getByText(/pass 1 ok/)).toBeVisible();
    expect(within(pass).getByText(/pass 2 failed/)).toBeVisible();
    expect(
      screen.queryByRole("list", { name: "fail fixture results" }),
    ).toBeNull();
    rerender(
      <RuleForm
        values={{ ...emptyForm(), pass: "not json", fail: "[]" }}
        checked
      />,
    );
    expect(screen.queryByRole("list", { name: /fixture results/ })).toBeNull();
  });
});
