import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * contract.md v1.1.1 token table, and the v1.2.0 required shadcn mapping.
 *
 * These are transcribed here on purpose: a frontend-only test that read the
 * same values from the same stylesheet it is checking would prove nothing. If a
 * token value changes in the contract, this table and `src/index.css` change
 * together or this suite fails.
 */
const TOKENS: Record<string, [string, string]> = {
  "--atlas-bg-canvas": ["#f7f2eb", "#41444b"],
  "--atlas-bg-surface": ["#eae2d6", "#52575d"],
  "--atlas-fg-default": ["#2d0000", "#dfd8c8"],
  "--atlas-fg-muted": ["#6a2f2f", "#b7b3a9"],
  "--atlas-accent": ["#8b9a6e", "#cabfab"],
  "--atlas-link": ["#2d0000", "#dfd8c8"],
  "--atlas-border-divider": ["#eeeeee", "#52575d"],
  "--atlas-border-control": ["#757d6f", "#9aa394"],
  "--atlas-on-accent": ["#2d0000", "#41444b"],
  "--atlas-focus-ring": ["#2d0000", "#dfd8c8"],
  "--atlas-success-fg": ["#2a7c13", "#2d0000"],
  "--atlas-success-bg": ["#c7d3c0", "#c7d3c0"],
  "--atlas-warning-fg": ["#2d0000", "#2d0000"],
  "--atlas-warning-bg": ["#c8a96b", "#c8a96b"],
  "--atlas-danger-fg": ["#6d0808", "#2d0000"],
  "--atlas-danger-bg": ["#ffdada", "#ffdada"],
  "--atlas-info-fg": ["#2d0000", "#2d0000"],
  "--atlas-info-bg": ["#fbe6c2", "#fbe6c2"],
};

/** The mapping contract.md v1.2.0 fixes; a component may only use these. */
const MAPPING: Record<string, string> = {
  "--background": "--atlas-bg-canvas",
  "--foreground": "--atlas-fg-default",
  "--card": "--atlas-bg-surface",
  "--primary": "--atlas-accent",
  "--primary-foreground": "--atlas-on-accent",
  "--muted-foreground": "--atlas-fg-muted",
  "--border": "--atlas-border-divider",
  "--input": "--atlas-border-control",
  "--ring": "--atlas-focus-ring",
  "--link": "--atlas-link",
  "--success-bg": "--atlas-success-bg",
  "--success-indicator": "--atlas-success-fg",
  "--warning-bg": "--atlas-warning-bg",
  "--warning-text": "--atlas-warning-fg",
  "--danger-bg": "--atlas-danger-bg",
  "--danger-text": "--atlas-danger-fg",
  "--info-bg": "--atlas-info-bg",
  "--info-text": "--atlas-info-fg",
};

const SRC = import.meta.dirname;
const CSS = readFileSync(join(SRC, "index.css"), "utf8");

/**
 * The whole rule for a selector, so the no-raw-colour check can remove it. The
 * selector may be one entry of a selector list, and `.dark` also appears in the
 * `@custom-variant` line, so this matches a selector only where it opens a rule.
 */
function block(selector: string): string {
  const pattern = new RegExp(
    `${selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\s*(?:,[^{]*)?\\{[^}]*\\}`,
  );
  const found = CSS.match(pattern);
  expect(found, `missing ${selector} rule`).not.toBeNull();
  return found?.[0] ?? "";
}

const LIGHT = block(":root");
const DARK = block(".dark");

function declared(css: string): Map<string, string> {
  return new Map(
    [...css.matchAll(/(--atlas-[a-z-]+):\s*(#[0-9a-fA-F]{3,8})/g)].map((match) => [
      match[1] as string,
      (match[2] as string).toLowerCase(),
    ]),
  );
}

function sourceFiles(directory: string): string[] {
  return readdirSync(directory).flatMap((entry) => {
    const path = join(directory, entry);
    if (statSync(path).isDirectory()) return sourceFiles(path);
    return /\.(ts|tsx|css|html)$/.test(entry) ? [path] : [];
  });
}

describe("token bridge", () => {
  it("declares all 18 contract tokens in the light block, once each", () => {
    const values = declared(LIGHT);
    expect([...values.keys()].sort()).toEqual(Object.keys(TOKENS).sort());
    for (const [name, [light]] of Object.entries(TOKENS)) {
      expect(values.get(name), name).toBe(light);
    }
  });

  it("declares all 18 contract tokens in the dark block, once each", () => {
    const values = declared(DARK);
    expect([...values.keys()].sort()).toEqual(Object.keys(TOKENS).sort());
    for (const [name, [, dark]] of Object.entries(TOKENS)) {
      expect(values.get(name), name).toBe(dark);
    }
  });

  it("maps every required shadcn variable onto its Atlas token", () => {
    const declaredVars = new Map(
      [...CSS.matchAll(/^\s{2}(--[a-z-]+):\s*var\((--atlas-[a-z-]+)\);/gm)].map((m) => [
        m[1] as string,
        m[2] as string,
      ]),
    );
    for (const [shadcnVar, token] of Object.entries(MAPPING)) {
      expect(declaredVars.get(shadcnVar), shadcnVar).toBe(token);
    }
  });

  it("keeps state.success text on the accessible ink, not the 3.38:1 foreground", () => {
    // The contract's one deviation: normal text on state.success.bg uses the
    // body ink, and the supplied foreground stays the non-text indicator.
    expect(CSS).toMatch(/--success-text:\s*var\(--atlas-fg-default\)/);
    expect(CSS).toMatch(/--success-indicator:\s*var\(--atlas-success-fg\)/);
  });

  it("makes the muted token and control border surface-safe", () => {
    const rule = block('[data-surface="true"]');
    expect(rule).toMatch(/--muted-foreground:\s*var\(--atlas-fg-default\)/);
    expect(rule).toMatch(/--border:\s*var\(--atlas-border-control\)/);
    expect(rule).toMatch(/--muted:\s*var\(--atlas-bg-canvas\)/);
  });

  it("gives a menu or listbox row a highlight that differs from its own surface", () => {
    // Radix marks the highlighted row with data-highlighted, not :focus, and a
    // fill equal to the surface it sits on is not a visible focus.
    const popup = block('[data-slot="select-content"]');
    expect(popup).toMatch(/--muted:\s*var\(--atlas-bg-canvas\)/);
    for (const file of ["select.tsx", "dropdown-menu.tsx"]) {
      const text = readFileSync(join(SRC, "components", "ui", file), "utf8");
      expect(text, file).toMatch(/data-\[highlighted\]:bg-muted/);
    }
  });

  it("drives the focus ring and the link underline from a token", () => {
    expect(CSS).toMatch(/:focus-visible\s*\{[^}]*outline:\s*2px solid var\(--ring\)/);
    expect(CSS).toMatch(/a\s*\{[^}]*color:\s*var\(--link\)[^}]*text-decoration:\s*underline/);
  });

  it("exposes each mapped variable as a Tailwind colour", () => {
    for (const shadcnVar of Object.keys(MAPPING)) {
      expect(CSS, shadcnVar).toMatch(
        new RegExp(`--color-${shadcnVar.slice(2)}:\\s*var\\(${shadcnVar}\\)`),
      );
    }
  });
});

describe("no raw colour outside the token stylesheet", () => {
  const HEX = /#[0-9a-fA-F]{3,8}\b/;
  const RGB = /\b(?:rgb|rgba|hsl|hsla|oklch|color)\(/;

  it("keeps every hex and colour function in the two mode blocks", () => {
    const outside = CSS.replace(LIGHT, "").replace(DARK, "");
    expect(outside, "index.css carries a colour outside the token blocks").not.toMatch(HEX);
    expect(outside).not.toMatch(RGB);
  });

  it("keeps every source file free of a raw colour", () => {
    const offenders = sourceFiles(SRC)
      .filter((path) => !path.endsWith("index.css"))
      // This file transcribes the contract's own values to check them, so it is
      // the one place outside index.css a hex may appear.
      .filter((path) => !/\.test\.tsx?$/.test(path))
      .filter((path) => {
        const text = readFileSync(path, "utf8");
        return HEX.test(text) || RGB.test(text);
      })
      .map((path) => path.slice(SRC.length + 1));
    expect(offenders).toEqual([]);
  });

  it("keeps index.html free of a raw colour", () => {
    const html = readFileSync(join(SRC, "..", "index.html"), "utf8");
    expect(html).not.toMatch(HEX);
    expect(html).not.toMatch(RGB);
  });
});
