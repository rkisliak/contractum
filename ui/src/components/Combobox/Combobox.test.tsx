// Combobox (quick 261119-r5g): the dropdown pattern, driven the way a user drives it. The test host
// owns the text and the chosen option like the real pickers do.
import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Dialog } from "@/components/Dialog";
import { Combobox, type ComboboxOption } from "./index";

const ALL: ComboboxOption[] = [
  {
    key: "a",
    label: "Alice Anders",
    detail: "alice@contoso.test",
    meta: "0b5c3f8e-1a2b-4c3d-8e9f-0123456789ab",
  },
  {
    key: "b",
    label: "Alicia Brandt",
    detail: "alicia@contoso.test",
    meta: "9a8b7c6d-1a2b-4c3d-8e9f-0123456789ab",
    note: "Already registered",
    disabled: true,
  },
  {
    key: "c",
    label: "Alina Cole",
    meta: "5f5f5f5f-1a2b-4c3d-8e9f-0123456789ab",
  },
];

function Host({
  onPick = () => undefined,
  options = ALL,
  min = 2,
}: {
  onPick?: (option: ComboboxOption) => void;
  options?: ComboboxOption[];
  min?: number;
}) {
  const [value, setValue] = useState("");
  const [chosen, setChosen] = useState<string | null>(null);
  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        onPick({ key: "submitted", label: "submitted" });
      }}
    >
      <Combobox
        id="people"
        label="Search people"
        helper="Type at least 2 characters."
        value={value}
        onValueChange={(next) => {
          setValue(next);
          setChosen(null);
        }}
        options={value.length >= min ? options : []}
        selectedKey={chosen}
        onSelect={(option) => {
          setValue(option.label);
          setChosen(option.key);
          onPick(option);
        }}
      />
    </form>
  );
}

const box = () => screen.getByRole("combobox", { name: "Search people" });

describe("Combobox", () => {
  it("is a collapsed combobox with list autocomplete until it has something to show", () => {
    render(<Host />);
    expect(box()).toHaveAttribute("aria-autocomplete", "list");
    expect(box()).toHaveAttribute("aria-expanded", "false");
    expect(box()).not.toHaveAttribute("aria-controls");
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("drops the list down after 2 characters, not before", async () => {
    const actor = userEvent.setup();
    render(<Host />);
    await actor.type(box(), "a");
    expect(screen.queryByRole("listbox")).toBeNull();
    await actor.type(box(), "l");
    const list = screen.getByRole("listbox", { name: "Search people results" });
    expect(box()).toHaveAttribute("aria-expanded", "true");
    expect(box()).toHaveAttribute("aria-controls", list.id);
    expect(within(list).getAllByRole("option")).toHaveLength(3);
    // The name is what the option shows: the display name, the second line and any note.
    const alice = within(list).getByRole("option", { name: /^Alice Anders/ });
    expect(alice).toHaveAccessibleName(
      "Alice Anders alice@contoso.test 0b5c3f8e-1a2b-4c3d-8e9f-0123456789ab",
    );
    expect(
      within(list).getByRole("option", { name: /^Alicia Brandt/ }),
    ).toHaveAttribute("aria-disabled", "true");
    expect(
      screen.getByText("Already registered", { selector: "span" }),
    ).toBeVisible();
    expect(screen.getByText("3 results available")).toBeInTheDocument();
  });

  it("moves with the arrows, skipping disabled options, and Enter picks and closes", async () => {
    const actor = userEvent.setup();
    const picked = vi.fn();
    render(<Host onPick={picked} />);
    await actor.type(box(), "ali");
    const options = screen.getAllByRole("option");
    await actor.keyboard("{ArrowDown}");
    expect(box()).toHaveAttribute("aria-activedescendant", options[0]?.id);
    await actor.keyboard("{ArrowDown}");
    // The second option is disabled: the third is next.
    expect(box()).toHaveAttribute("aria-activedescendant", options[2]?.id);
    await actor.keyboard("{ArrowDown}");
    expect(box()).toHaveAttribute("aria-activedescendant", options[0]?.id);
    await actor.keyboard("{ArrowUp}");
    expect(box()).toHaveAttribute("aria-activedescendant", options[2]?.id);
    await actor.keyboard("{Enter}");
    expect(picked).toHaveBeenCalledTimes(1);
    expect(picked.mock.calls[0]?.[0]).toMatchObject({ key: "c" });
    expect(box()).toHaveValue("Alina Cole");
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(box()).toHaveAttribute("aria-expanded", "false");
  });

  it("does not submit the surrounding form when Enter picks", async () => {
    const actor = userEvent.setup();
    const picked = vi.fn();
    render(<Host onPick={picked} />);
    await actor.type(box(), "ali");
    await actor.keyboard("{ArrowDown}{Enter}");
    expect(
      picked.mock.calls.map(([option]) => (option as ComboboxOption).key),
    ).toEqual(["a"]);
  });

  it("picks with a click and ignores a click on a disabled option", async () => {
    const actor = userEvent.setup();
    const picked = vi.fn();
    render(<Host onPick={picked} />);
    await actor.type(box(), "ali");
    await actor.click(screen.getByRole("option", { name: /^Alicia Brandt/ }));
    expect(picked).not.toHaveBeenCalled();
    expect(screen.getByRole("listbox")).toBeVisible();
    await actor.click(screen.getByRole("option", { name: /^Alice Anders/ }));
    expect(picked).toHaveBeenCalledTimes(1);
    expect(box()).toHaveValue("Alice Anders");
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("marks the chosen option, and typing again reopens the list", async () => {
    const actor = userEvent.setup();
    render(<Host />);
    await actor.type(box(), "ali");
    await actor.click(screen.getByRole("option", { name: /^Alice Anders/ }));
    await actor.click(box());
    expect(
      screen.getByRole("option", { name: /^Alice Anders/ }),
    ).toHaveAttribute("aria-selected", "true");
    await actor.keyboard("{Escape}");
    expect(screen.queryByRole("listbox")).toBeNull();
    await actor.type(box(), "x");
    expect(screen.getByRole("listbox")).toBeVisible();
    expect(
      screen.getByRole("option", { name: /^Alice Anders/ }),
    ).toHaveAttribute("aria-selected", "false");
  });

  it("closes on Escape and reopens by typing", async () => {
    const actor = userEvent.setup();
    render(<Host />);
    await actor.type(box(), "ali");
    expect(screen.getByRole("listbox")).toBeVisible();
    await actor.keyboard("{Escape}");
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(box()).toHaveValue("ali");
    await actor.type(box(), "c");
    expect(screen.getByRole("listbox")).toBeVisible();
  });

  it("closes when focus leaves", async () => {
    const actor = userEvent.setup();
    render(<Host />);
    await actor.type(box(), "ali");
    await actor.tab();
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("clears the text with the clear button and keeps the focus in the box", async () => {
    const actor = userEvent.setup();
    render(<Host />);
    expect(screen.queryByRole("button", { name: "Clear search" })).toBeNull();
    await actor.type(box(), "ali");
    await actor.click(screen.getByRole("button", { name: "Clear search" }));
    expect(box()).toHaveValue("");
    expect(box()).toHaveFocus();
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(screen.queryByRole("button", { name: "Clear search" })).toBeNull();
  });

  it("shows a status line in the popup when there are no options", async () => {
    const actor = userEvent.setup();
    function Empty() {
      const [value, setValue] = useState("");
      return (
        <Combobox
          id="empty"
          label="Search things"
          value={value}
          onValueChange={setValue}
          options={[]}
          onSelect={() => undefined}
          status={{ text: "No things match." }}
        />
      );
    }
    render(<Empty />);
    const input = screen.getByRole("combobox", { name: "Search things" });
    await actor.type(input, "zz");
    expect(screen.getByText("No things match.")).toBeVisible();
    expect(screen.getByText("No things match.")).toHaveAttribute(
      "role",
      "status",
    );
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(input).toHaveAttribute("aria-expanded", "false");
  });

  it("closes only itself on Escape inside a Dialog", async () => {
    const actor = userEvent.setup();
    render(
      <Dialog open onOpenChange={() => undefined} title="Pick someone">
        <Host />
      </Dialog>,
    );
    await actor.type(box(), "ali");
    const dialog = screen.getByRole("dialog", { name: "Pick someone" });
    // The popup lives inside the dialog so it is not behind the modal layer.
    expect(within(dialog).getByRole("listbox")).toBeVisible();
    await actor.keyboard("{Escape}");
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(screen.getByRole("dialog", { name: "Pick someone" })).toBeVisible();
  });

  it("takes the last option on ArrowUp, and Enter without an active option submits the form", async () => {
    const actor = userEvent.setup();
    const picked = vi.fn();
    const plain: ComboboxOption[] = [
      { key: "x", label: "Xavier" },
      { key: "y", label: "Yara" },
    ];
    render(<Host onPick={picked} options={plain} />);
    await actor.type(box(), "xa");
    // An option with only a label has no second line: its name is the label alone.
    expect(screen.getByRole("option", { name: "Xavier" })).toBeVisible();
    await actor.keyboard("{Enter}");
    expect(
      picked.mock.calls.map(([option]) => option as ComboboxOption),
    ).toEqual([{ key: "submitted", label: "submitted" }]);
    await actor.keyboard("{ArrowUp}");
    expect(box()).toHaveAttribute(
      "aria-activedescendant",
      screen.getByRole("option", { name: "Yara" }).id,
    );
  });

  it("ignores the arrows when every option is disabled, when there is no list, and while composing", async () => {
    const actor = userEvent.setup();
    const locked: ComboboxOption[] = [
      { key: "l", label: "Locked", note: "Already registered", disabled: true },
    ];
    const { unmount } = render(<Host options={locked} />);
    await actor.type(box(), "lo");
    await actor.keyboard("{ArrowDown}");
    expect(box()).not.toHaveAttribute("aria-activedescendant");
    unmount();

    render(<Host min={5} />);
    await actor.type(box(), "al");
    await actor.keyboard("{ArrowDown}");
    expect(box()).toHaveAttribute("aria-expanded", "false");
    await actor.type(box(), "ice");
    fireEvent.keyDown(box(), { key: "ArrowDown", isComposing: true });
    expect(box()).not.toHaveAttribute("aria-activedescendant");
  });

  it("announces a single result and shows a busy search with a spinner", async () => {
    const actor = userEvent.setup();
    function Searching({ options }: { options: ComboboxOption[] }) {
      const [value, setValue] = useState("");
      return (
        <Combobox
          id="search"
          label="Search people"
          value={value}
          onValueChange={setValue}
          options={value === "" ? [] : options}
          onSelect={() => undefined}
          status={{ text: "Searching…", busy: true }}
        />
      );
    }
    render(
      <Searching
        options={[{ key: "d", label: "Dana", detail: "dana@contoso.test" }]}
      />,
    );
    await actor.type(box(), "da");
    expect(screen.getByText("1 result available")).toBeInTheDocument();
    expect(
      screen.getByRole("option", { name: "Dana dana@contoso.test" }),
    ).toBeVisible();
    const searching = screen.getByText("Searching…");
    expect(searching.closest("div")?.querySelector("svg")).not.toBeNull();
  });
});

describe("Combobox popup placement", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  function rectAt(top: number, bottom: number): DOMRect {
    return {
      top,
      bottom,
      left: 16,
      right: 316,
      width: 300,
      height: bottom - top,
      x: 16,
      y: top,
      toJSON: () => ({}),
    };
  }

  it.each([
    // Little room below, much above: the popup opens upwards, anchored by its bottom edge.
    [700, 736, { bottom: "72px", top: "", maxHeight: "320px" }],
    // No room on either side: it stays below at the minimum height.
    [10, 750, { bottom: "", top: "754px", maxHeight: "96px" }],
  ])(
    "with the input at %i to %ipx it places the popup at %o",
    async (top, bottom, expected) => {
      vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue(
        rectAt(top, bottom),
      );
      const actor = userEvent.setup();
      render(<Host />);
      await actor.type(box(), "ali");
      const popup = screen.getByRole("listbox").parentElement;
      expect(popup?.style.bottom).toBe(expected.bottom);
      expect(popup?.style.top).toBe(expected.top);
      expect(popup?.style.maxHeight).toBe(expected.maxHeight);
      expect(popup?.style.width).toBe("300px");
    },
  );
});
