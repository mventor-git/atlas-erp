import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { THEME_STORAGE_KEY, ThemeProvider, useTheme } from "@/lib/theme";

/** The mode is one class on <html>; the button label reports what resolved. */
function Probe() {
  const { mode, resolved, setMode } = useTheme();
  return (
    <div>
      <p data-testid="mode">{mode}</p>
      <p data-testid="resolved">{resolved}</p>
      <button onClick={() => setMode("dark")}>go dark</button>
      <button onClick={() => setMode("light")}>go light</button>
      <button onClick={() => setMode("system")}>go system</button>
    </div>
  );
}

const isDark = () => document.documentElement.classList.contains("dark");

function renderProbe() {
  return render(
    <ThemeProvider>
      <Probe />
    </ThemeProvider>,
  );
}

describe("theme", () => {
  it("defaults to light when nothing is stored", async () => {
    renderProbe();
    expect(await screen.findByTestId("resolved")).toHaveProperty("textContent", "light");
    expect(isDark()).toBe(false);
  });

  it("applies the dark class for an explicit dark choice", async () => {
    renderProbe();
    fireEvent.click(screen.getByRole("button", { name: "go dark" }));

    expect(await screen.findByTestId("resolved")).toHaveProperty("textContent", "dark");
    expect(isDark()).toBe(true);
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");
  });

  it("removes the dark class for an explicit light choice", async () => {
    renderProbe();
    fireEvent.click(screen.getByRole("button", { name: "go dark" }));
    fireEvent.click(screen.getByRole("button", { name: "go light" }));

    expect(await screen.findByTestId("resolved")).toHaveProperty("textContent", "light");
    expect(isDark()).toBe(false);
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe("light");
  });

  it("restores a persisted choice on the next mount", async () => {
    localStorage.setItem(THEME_STORAGE_KEY, "dark");
    document.documentElement.classList.add("dark");
    renderProbe();

    expect(await screen.findByTestId("resolved")).toHaveProperty("textContent", "dark");
    expect(isDark()).toBe(true);
  });

  it("follows the system only while the mode is system", async () => {
    const listeners = new Set<(event: MediaQueryListEvent) => void>();
    let matches = true;
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: query.includes("dark") ? matches : false,
      addEventListener: (_: string, listener: (event: MediaQueryListEvent) => void) =>
        listeners.add(listener),
      removeEventListener: (_: string, listener: (event: MediaQueryListEvent) => void) =>
        listeners.delete(listener),
    }));

    renderProbe();
    expect(await screen.findByTestId("resolved")).toHaveProperty("textContent", "dark");
    expect(isDark()).toBe(true);

    // A system change does not overwrite an explicit choice.
    fireEvent.click(screen.getByRole("button", { name: "go light" }));
    expect(isDark()).toBe(false);

    matches = true;
    for (const listener of listeners) listener(new Event("change") as MediaQueryListEvent);
    expect(isDark()).toBe(false);
  });

  it("ignores an unusable stored value instead of rendering untokenized", async () => {
    localStorage.setItem(THEME_STORAGE_KEY, "chartreuse");
    renderProbe();

    expect(await screen.findByTestId("mode")).toHaveProperty("textContent", "system");
    expect(isDark()).toBe(false);
  });
});
