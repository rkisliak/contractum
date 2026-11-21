// Combobox (quick 261119-r5g, D-01): a text input with a results list attached under it, the WAI-ARIA
// 1.2 combobox with a listbox popup. The input keeps focus the whole time: arrows move
// `aria-activedescendant`, Enter picks, Escape closes the list (and only the list: a Dialog around it
// stays open), typing reopens it, the clear button resets. The caller owns the text, the options and
// the chosen key; it decides what a pick does (usually: put the display name in the box and keep the
// record). The popup is rendered in a portal (into the open Dialog when there is one, C-24), positioned
// from the input's box, so a Dialog's scrolling body never clips it.
import { Check, X } from "lucide-react";
import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import type { KeyboardEvent } from "react";
import { createPortal } from "react-dom";
import { IconButton } from "@/components/Button";
import { useDialogContainer } from "@/components/Dialog";
import {
  FieldShell,
  FormField,
  useFormFieldControl,
  type ControlSize,
} from "@/components/form";
import { Spinner } from "@/components/states";
import { VisuallyHidden } from "@/components/VisuallyHidden";
import { cx } from "@/lib/cx";
import styles from "./Combobox.module.css";

export type ComboboxOption = {
  /** Stable and unique among the options. */
  key: string;
  /** Line 1: the name. */
  label: string;
  /** Line 2: secondary text (email, UPN, application ID, type). */
  detail?: string;
  /** Line 2, after the detail: the object ID, in the normal font. */
  meta?: string;
  /** Line 3: why the option cannot be picked ("Already registered"). */
  note?: string;
  disabled?: boolean;
};

export type ComboboxStatus = {
  text: string;
  /** Show the spinner (a search in flight). */
  busy?: boolean;
};

export type ComboboxProps = {
  id: string;
  label: string;
  helper?: string;
  error?: string;
  /** The text in the input. */
  value: string;
  /** The user typed or cleared the text; picking an option does not call this. */
  onValueChange: (value: string) => void;
  options: ComboboxOption[];
  /** The chosen option's key: marked `aria-selected` with a check. */
  selectedKey?: string | null;
  onSelect: (option: ComboboxOption) => void;
  /** The clear button was used (called after `onValueChange("")`). */
  onClear?: () => void;
  /** The listbox's accessible name; defaults to "{label} results". */
  listLabel?: string;
  /** A line under the options in the popup: searching, no matches, "type more to narrow". */
  status?: ComboboxStatus | null;
  maxLength?: number;
  placeholder?: string;
  controlSize?: ControlSize;
  clearLabel?: string;
};

type Placement = {
  left: number;
  width: number;
  maxHeight: number;
  top?: number;
  bottom?: number;
};

const GAP = 4;
const EDGE = 8;
const MAX_POPUP = 320;
/** Below this much room under the input the popup opens above it, if there is more room there. */
const MIN_BELOW = 160;

function place(anchor: HTMLElement): Placement {
  const rect = anchor.getBoundingClientRect();
  const below = window.innerHeight - rect.bottom - GAP - EDGE;
  const above = rect.top - GAP - EDGE;
  const up = below < MIN_BELOW && above > below;
  return {
    left: rect.left,
    width: rect.width,
    maxHeight: Math.max(96, Math.min(MAX_POPUP, up ? above : below)),
    ...(up
      ? { bottom: window.innerHeight - rect.top + GAP }
      : { top: rect.bottom + GAP }),
  };
}

function ComboboxControl({
  value,
  onValueChange,
  options,
  selectedKey,
  onSelect,
  onClear,
  status,
  maxLength,
  placeholder,
  controlSize,
  clearLabel,
  listLabel,
  label,
}: Omit<ComboboxProps, "id" | "helper" | "error">) {
  const wiring = useFormFieldControl();
  const listId = useId();
  const optionPrefix = useId();
  const container = useDialogContainer();
  const inputRef = useRef<HTMLInputElement>(null);
  const anchorRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [activeKey, setActiveKey] = useState<string | null>(null);
  const [placement, setPlacement] = useState<Placement | null>(null);

  const hasList = options.length > 0;
  const listOpen = open && hasList;
  const popupOpen = open && (hasList || Boolean(status));
  const activeIndex = options.findIndex(
    (option) => option.key === activeKey && !option.disabled,
  );

  const close = useCallback(() => {
    setOpen(false);
    setActiveKey(null);
  }, []);

  // Position from the input's box, before paint, and again on scroll and resize.
  useLayoutEffect(() => {
    if (!popupOpen || anchorRef.current === null) {
      setPlacement(null);
      return;
    }
    const anchor = anchorRef.current;
    const update = () => {
      setPlacement(place(anchor));
    };
    update();
    window.addEventListener("resize", update);
    window.addEventListener("scroll", update, true);
    return () => {
      window.removeEventListener("resize", update);
      window.removeEventListener("scroll", update, true);
    };
  }, [popupOpen, options.length]);

  // Escape closes the list and nothing else. Registered on the window in the capture phase, ahead of
  // the Dialog's own Escape handling, so a Dialog around the field stays open.
  useEffect(() => {
    if (!popupOpen) return;
    const onKey = (event: globalThis.KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.stopPropagation();
      close();
    };
    window.addEventListener("keydown", onKey, true);
    return () => {
      window.removeEventListener("keydown", onKey, true);
    };
  }, [popupOpen, close]);

  // Keep the keyboard-active option in view.
  useEffect(() => {
    if (activeIndex < 0) return;
    const node = listRef.current?.children[activeIndex];
    // jsdom has no scrollIntoView.
    // eslint-disable-next-line @typescript-eslint/no-unnecessary-condition
    node?.scrollIntoView?.({ block: "nearest" });
  }, [activeIndex]);

  function pick(option: ComboboxOption) {
    if (option.disabled) return;
    onSelect(option);
    close();
  }

  function move(delta: 1 | -1) {
    const enabled = options.filter((option) => !option.disabled);
    if (enabled.length === 0) return;
    const at = enabled.findIndex((option) => option.key === activeKey);
    const next =
      at === -1
        ? delta > 0
          ? 0
          : enabled.length - 1
        : (at + delta + enabled.length) % enabled.length;
    setActiveKey(enabled[next]?.key ?? null);
  }

  function onKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.nativeEvent.isComposing) return;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      if (!hasList) return;
      event.preventDefault();
      setOpen(true);
      move(event.key === "ArrowDown" ? 1 : -1);
    } else if (event.key === "Enter") {
      const active = options[activeIndex];
      if (listOpen && active) {
        // Picking must not submit the form around the field.
        event.preventDefault();
        pick(active);
      }
    }
  }

  const fixed = placement ?? { left: 0, width: 0, maxHeight: 0 };
  const popup = popupOpen ? (
    // The input keeps focus: a press on the popup (an option, the scrollbar) must not blur it.
    // eslint-disable-next-line jsx-a11y/no-static-element-interactions
    <div
      className={styles.popup}
      style={{
        left: fixed.left,
        width: fixed.width,
        top: fixed.top,
        bottom: fixed.bottom,
        maxHeight: fixed.maxHeight,
        visibility: placement ? undefined : "hidden",
      }}
      onMouseDown={(event) => {
        event.preventDefault();
      }}
    >
      {hasList ? (
        <div
          ref={listRef}
          id={listId}
          role="listbox"
          aria-label={listLabel ?? `${label} results`}
          className={styles.list}
        >
          {options.map((option, index) => {
            const base = `${optionPrefix}-${String(index)}`;
            const chosen = option.key === selectedKey;
            return (
              // Keyboard operation lives on the combobox input (aria-activedescendant); the
              // options are never focused, so the click handler needs no key handler of its own.
              // eslint-disable-next-line jsx-a11y/click-events-have-key-events
              <div
                key={option.key}
                id={base}
                role="option"
                tabIndex={-1}
                aria-selected={chosen}
                aria-disabled={option.disabled ? true : undefined}
                data-active={index === activeIndex ? "" : undefined}
                className={cx(
                  styles.option,
                  option.disabled && styles.disabled,
                )}
                onClick={() => {
                  pick(option);
                }}
              >
                <span className={styles.first}>
                  <span className={styles.name}>{option.label}</span>
                  {chosen ? (
                    <Check
                      className={styles.check}
                      size={16}
                      strokeWidth={2}
                      aria-hidden="true"
                    />
                  ) : null}
                </span>
                {option.detail || option.meta ? (
                  <span className={styles.second}>
                    {option.detail ? (
                      <span className={styles.detail}>{option.detail}</span>
                    ) : null}
                    {/* A space between the parts: the accessible name reads "email id", not "emailid". */}
                    {option.detail && option.meta ? " " : null}
                    {option.meta ? (
                      <span className={styles.meta}>{option.meta}</span>
                    ) : null}
                  </span>
                ) : null}
                {option.note ? (
                  <span className={styles.note}>{option.note}</span>
                ) : null}
              </div>
            );
          })}
        </div>
      ) : null}
      {status ? (
        status.busy ? (
          <div className={styles.status}>
            <Spinner label={status.text} />
          </div>
        ) : (
          <div role="status" className={styles.status}>
            {status.text}
          </div>
        )
      ) : null}
    </div>
  ) : null;

  return (
    <div ref={anchorRef} className={styles.anchor}>
      <FieldShell controlSize={controlSize}>
        <input
          ref={inputRef}
          type="text"
          role="combobox"
          id={wiring.id}
          aria-describedby={wiring["aria-describedby"]}
          aria-invalid={wiring["aria-invalid"]}
          aria-autocomplete="list"
          aria-haspopup="listbox"
          aria-expanded={listOpen}
          aria-controls={listOpen ? listId : undefined}
          aria-activedescendant={
            listOpen && activeIndex >= 0
              ? `${optionPrefix}-${String(activeIndex)}`
              : undefined
          }
          autoComplete="off"
          spellCheck={false}
          maxLength={maxLength}
          placeholder={placeholder}
          value={value}
          className={cx(styles.input, value !== "" && styles.inputWithClear)}
          onChange={(event) => {
            onValueChange(event.target.value);
            setOpen(true);
            setActiveKey(null);
          }}
          onClick={() => {
            if (hasList) setOpen(true);
          }}
          onKeyDown={onKeyDown}
          onBlur={close}
        />
        {value !== "" ? (
          <span className={styles.trailing}>
            <IconButton
              size={32}
              icon={X}
              aria-label={clearLabel ?? "Clear search"}
              onClick={() => {
                onValueChange("");
                onClear?.();
                close();
                inputRef.current?.focus();
              }}
            />
          </span>
        ) : null}
      </FieldShell>
      <VisuallyHidden role="status" aria-live="polite">
        {listOpen
          ? `${String(options.length)} result${options.length === 1 ? "" : "s"} available`
          : ""}
      </VisuallyHidden>
      {popup ? createPortal(popup, container ?? document.body) : null}
    </div>
  );
}

export function Combobox({ id, helper, error, ...rest }: ComboboxProps) {
  return (
    <FormField id={id} label={rest.label} helper={helper} error={error}>
      <ComboboxControl {...rest} />
    </FormField>
  );
}
