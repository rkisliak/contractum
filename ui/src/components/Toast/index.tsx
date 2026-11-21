// Toast (UI-SPEC 5.6, D-UI-08-26): success and neutral confirmations on Radix Toast, top-right,
// a polite live region labelled "Notifications", 6 s auto-dismiss paused on hover or focus, at
// most three visible (the oldest leaves first). Errors never use a toast; they stay in flow.
import * as RadixToast from "@radix-ui/react-toast";
import { CircleCheck, Info, X } from "lucide-react";
import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { VisuallyHidden } from "@/components/VisuallyHidden";
import { cx } from "@/lib/cx";
import styles from "./Toast.module.css";

export type ToastInput = {
  title: string;
  description?: string;
  intent?: "success" | "neutral";
};

type ToastItem = ToastInput & { id: number; open: boolean };

const MAX_VISIBLE = 3;
export const TOAST_DURATION_MS = 6000;

const ToastContext = createContext<((toast: ToastInput) => void) | null>(null);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const nextId = useRef(1);

  const show = useCallback((toast: ToastInput) => {
    const id = nextId.current++;
    setItems((current) =>
      [
        ...current.filter((item) => item.open),
        { ...toast, id, open: true },
      ].slice(-MAX_VISIBLE),
    );
  }, []);

  const close = useCallback((id: number) => {
    setItems((current) =>
      current.map((item) => (item.id === id ? { ...item, open: false } : item)),
    );
  }, []);

  const value = useMemo(() => show, [show]);

  return (
    <ToastContext.Provider value={value}>
      <RadixToast.Provider
        label="Notifications"
        duration={TOAST_DURATION_MS}
        swipeDirection="right"
      >
        {children}
        {items.map((item) => {
          const intent = item.intent ?? "success";
          const Icon = intent === "success" ? CircleCheck : Info;
          return (
            <RadixToast.Root
              key={item.id}
              open={item.open}
              onOpenChange={(open) => {
                if (!open) close(item.id);
              }}
              className={cx(
                styles.toast,
                intent === "success" ? styles.success : styles.neutral,
              )}
            >
              <Icon
                className={styles.icon}
                size={16}
                strokeWidth={2}
                aria-hidden="true"
              />
              <div className={styles.content}>
                <RadixToast.Title className={styles.title}>
                  <VisuallyHidden>
                    {intent === "success" ? "Success" : "Note"}:{" "}
                  </VisuallyHidden>
                  {item.title}
                </RadixToast.Title>
                {item.description ? (
                  <RadixToast.Description className={styles.description}>
                    {item.description}
                  </RadixToast.Description>
                ) : null}
              </div>
              <RadixToast.Close
                className={styles.close}
                aria-label="Dismiss notification"
              >
                <X size={16} strokeWidth={2} aria-hidden="true" />
              </RadixToast.Close>
            </RadixToast.Root>
          );
        })}
        <RadixToast.Viewport className={styles.viewport} aria-live="polite" />
      </RadixToast.Provider>
    </ToastContext.Provider>
  );
}

/** Shows a toast; a no-op outside the provider so isolated component tests need no wrapper. */
export function useToast(): (toast: ToastInput) => void {
  return useContext(ToastContext) ?? noop;
}

function noop(): void {
  return undefined;
}
