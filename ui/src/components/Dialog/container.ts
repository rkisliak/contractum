// The open dialog's content node, in its own module so poppers (Tooltip, Combobox, Menu) can read
// it without importing the Dialog component, which itself renders a Tooltip (no import cycle).
import { createContext, use } from "react";

export const DialogContentNode = createContext<HTMLElement | null>(null);

/**
 * The open dialog's content node (null outside a dialog). A popper opened inside a dialog
 * portals into it (Radix `container`) so it is not hidden behind the modal layer (C-24).
 */
export function useDialogContainer(): HTMLElement | null {
  return use(DialogContentNode);
}
