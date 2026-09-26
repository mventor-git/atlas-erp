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

/**
 * The contract's one named deviation, kept as its own table because it is not a
 * value the contract supplies: `state.success.text` is the accessible ink in
 * both modes, and the supplied foreground survives as the non-text indicator.
 * These are the tokens that deviation resolves to.
 */
const SUCCESS_ROLE: Record<string, [string, string]> = {
  "--atlas-success-text": ["#2d0000", "#2d0000"],
  "--atlas-success-indicator": ["#2a7c13", "#2d0000"],
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
  "--success-text": "--atlas-success-text",
  "--success-indicator": "--atlas-success-indicator",
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

/** WCAG 2.x relative luminance. Same formula as the sibling product's check. */
function luminance(hex: string): number {
  const channels = [1, 3, 5].map((offset) => Number.parseInt(hex.slice(offset, offset + 2), 16) / 255);
  const [red, green, blue] = channels.map((value) =>
    value <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4,
  );
  return 0.2126 * (red ?? 0) + 0.7152 * (green ?? 0) + 0.0722 * (blue ?? 0);
}

/** WCAG 2.x contrast ratio, order-independent. */
function contrast(a: string, b: string): number {
  const [high, low] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (high + 0.05) / (low + 0.05);
}

/**
 * Every declaration in every `:root` rule. The mapping block follows the token
 * block in source order, so merging them and letting the last write win is the
 * cascade, and it is what lets `resolve` start at a shadcn variable.
 */
function mapped(): Map<string, string> {
  const found = new Map<string, string>();
  for (const [, body] of CSS.matchAll(/:root\s*(?:,[^{]*)?\{([^}]*)\}/g)) {
    for (const [, name, value] of (body ?? "").matchAll(/(--[a-z-]+):\s*([^;]+);/g)) {
      found.set(name as string, (value as string).trim());
    }
  }
  return found;
}

/**
 * Follow a shadcn variable to the colour it actually paints with in one mode.
 *
 * This is the whole point of the assertions below. Asserting that
 * `--success-text` is *wired* to some token proves the wiring; it says nothing
 * about what a reader sees. Resolving the chain to the hex that mode declares
 * and measuring that is the only version of this check that can fail when the
 * page is unreadable.
 */
function resolve(name: string, mode: "light" | "dark"): string {
  const table = declared(mode === "light" ? LIGHT : DARK);
  const chain = mapped();
  let value = chain.get(name) ?? "";
  for (let hop = 0; hop < 4; hop++) {
    const next = /^var\((--[a-z-]+)\)$/.exec(value)?.[1];
    if (!next) break;
    value = table.get(next) ?? chain.get(next) ?? "";
  }
  return value;
}

function sourceFiles(directory: string): string[] {
  return readdirSync(directory).flatMap((entry) => {
    const path = join(directory, entry);
    if (statSync(path).isDirectory()) return sourceFiles(path);
    return /\.(ts|tsx|css|html)$/.test(entry) ? [path] : [];
  });
}

describe("token bridge", () => {
  it("declares the 18 contract tokens and the success role in the light block, once each", () => {
    const values = declared(LIGHT);
    const expected = { ...TOKENS, ...SUCCESS_ROLE };
    expect([...values.keys()].sort()).toEqual(Object.keys(expected).sort());
    for (const [name, [light]] of Object.entries(expected)) {
      expect(values.get(name), name).toBe(light);
    }
  });

  it("declares the 18 contract tokens and the success role in the dark block, once each", () => {
    const values = declared(DARK);
    const expected = { ...TOKENS, ...SUCCESS_ROLE };
    expect([...values.keys()].sort()).toEqual(Object.keys(expected).sort());
    for (const [name, [, dark]] of Object.entries(expected)) {
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

  it("keeps state.success text at AA on its own background in both modes", () => {
    // The contract's one deviation, checked as the outcome rather than the wiring.
    // The supplied success foreground is 3.38:1 on state.success.bg, so the text
    // token resolves to the accessible ink while the indicator keeps it. Reading
    // the resolved pair is what a reader actually gets; the previous version of
    // this test asserted the variable's wiring and stayed green on a 1.10:1 badge.
    for (const mode of ["light", "dark"] as const) {
      const foreground = resolve("--success-text", mode);
      const background = resolve("--success-bg", mode);
      const ratio = contrast(foreground, background);
      expect(foreground, `${mode}: --success-text resolved to nothing`).toMatch(/^#[0-9a-f]{6}$/);
      expect(
        ratio,
        `${mode}: state.success text ${foreground} on ${background} measures ${ratio.toFixed(2)}:1`,
      ).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("never paints state.success text with the body ink, which is 1.10:1 in dark", () => {
    // The exact regression, named so the failure says what happened: pointing
    // --success-text back at --atlas-fg-default is invisible on state.success.bg
    // in dark mode. In light the two are the same colour, so only dark separates
    // them, and this is the assertion that fails if the wiring is undone.
    expect(resolve("--success-text", "dark")).not.toBe(resolve("--foreground", "dark"));
    expect(CSS).toMatch(/--success-indicator:\s*var\(--atlas-success-indicator\)/);
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
