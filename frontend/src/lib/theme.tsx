import { createContext, useContext, useEffect, useMemo, useState } from "react";
import type * as React from "react";

export type ThemeMode = "light" | "dark" | "system";
export type ResolvedTheme = "light" | "dark";

/** The one storage key. `index.html` reads it too, before first paint. */
export const THEME_STORAGE_KEY = "atlas-erp-theme";

const MODES: readonly ThemeMode[] = ["light", "dark", "system"];

function storedMode(): ThemeMode {
  try {
    const value = localStorage.getItem(THEME_STORAGE_KEY);
    return MODES.find((mode) => mode === value) ?? "system";
  } catch {
    return "system";
  }
}

function systemTheme(): ResolvedTheme {
  // A browser without the query, or a test environment, falls back to light;
  // light is a fully tokenized page, so a missing answer is never untokenized.
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
    return "light";
  }
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

type ThemeContextValue = {
  mode: ThemeMode;
  resolved: ResolvedTheme;
  setMode: (mode: ThemeMode) => void;
};

const ThemeContext = createContext<ThemeContextValue | null>(null);

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const [mode, setMode] = useState<ThemeMode>(storedMode);
  // The class on <html> is the applied truth: index.html has already set it
  // from storage or the system before React mounted.
  const [resolved, setResolved] = useState<ResolvedTheme>(() =>
    document.documentElement.classList.contains("dark") ? "dark" : "light",
  );

  useEffect(() => {
    const next = mode === "system" ? systemTheme() : mode;
    setResolved(next);
    document.documentElement.classList.toggle("dark", next === "dark");
    if (mode !== "system") {
      try {
        localStorage.setItem(THEME_STORAGE_KEY, mode);
      } catch {
        // A console opened with storage blocked still switches for this session.
      }
    }
  }, [mode]);

  // Only "system" follows the operating system after the first render, so an
  // explicit choice is not overwritten by an OS change.
  useEffect(() => {
    if (mode !== "system" || typeof window.matchMedia !== "function") return;
    const query = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => setResolved(query.matches ? "dark" : "light");
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, [mode]);

  const value = useMemo<ThemeContextValue>(
    () => ({ mode, resolved, setMode }),
    [mode, resolved],
  );
  return <ThemeContext value={value}>{children}</ThemeContext>;
}

export function useTheme(): ThemeContextValue {
  const value = useContext(ThemeContext);
  if (!value) throw new Error("useTheme needs a ThemeProvider");
  return value;
}
