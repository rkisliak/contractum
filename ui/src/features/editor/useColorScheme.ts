// The active colour scheme (prefers-color-scheme), kept live so the Monaco theme follows the OS.
import { useSyncExternalStore } from "react";
import type { EditorScheme } from "./monacoSetup";

const QUERY = "(prefers-color-scheme: dark)";

function subscribe(onChange: () => void): () => void {
  if (typeof window.matchMedia !== "function") return () => undefined;
  const media = window.matchMedia(QUERY);
  media.addEventListener("change", onChange);
  return () => {
    media.removeEventListener("change", onChange);
  };
}

function snapshot(): EditorScheme {
  if (typeof window.matchMedia !== "function") return "light";
  return window.matchMedia(QUERY).matches ? "dark" : "light";
}

export function useColorScheme(): EditorScheme {
  return useSyncExternalStore(subscribe, snapshot, () => "light");
}
