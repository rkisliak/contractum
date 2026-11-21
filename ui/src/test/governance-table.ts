// Table helpers shared by the governance and admin screen tests.
import { within } from "@testing-library/react";
import type userEvent from "@testing-library/user-event";

/** Clicks a sortable column header and returns the row headers' text in display order. */
export async function sortRowsBy(
  actor: ReturnType<typeof userEvent.setup>,
  table: HTMLElement,
  header: string,
): Promise<(string | null)[]> {
  await actor.click(within(table).getByRole("button", { name: header }));
  return within(table)
    .getAllByRole("rowheader")
    .map((cell) => cell.textContent);
}
