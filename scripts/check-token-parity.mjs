#!/usr/bin/env node
// Cross-repo Atlas token parity check. Read-only: it reads two stylesheets and
// writes nothing.
//
// contract.md v1.1.1 makes the shared token table the single reference and
// requires parity across both products' frontends. Each repo's own test proves
// its bridge matches the contract table; nothing proved the two bridges match
// each other, so one could be edited with no failing check. This is that check.
//
// It holds token NAMES only, no colour values. Comparing the two bridges
// directly means no value can pass here without passing in both files, so this
// cannot rot into a third palette that merely matches a stale copy of itself.
//
// Usage: node scripts/check-token-parity.mjs   (from anywhere; paths are
// resolved from this file, not the cwd)
//
// Exit: 0 parity, 1 a parity failure, 2 the check could not run.

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

// The 18 contract tokens, in the two stylesheets' own `--atlas-*` naming. The
// contract table is 14 rows because each `state.*` row is a foreground on a
// background, which is two variables: 14 + 4 = 18. Same list as the
// `frontend/src/tokens.test.ts` in this repo, which checks one bridge against
// the transcribed values; this checks the two bridges against each other.
const TOKENS = [
  "--atlas-bg-canvas",
  "--atlas-bg-surface",
  "--atlas-fg-default",
  "--atlas-fg-muted",
  "--atlas-accent",
  "--atlas-link",
  "--atlas-border-divider",
  "--atlas-border-control",
  "--atlas-on-accent",
  "--atlas-focus-ring",
  "--atlas-success-fg",
  "--atlas-success-bg",
  // The contract's one named deviation, not contract-supplied values: the
  // accessible ink in both modes, and the supplied foreground kept as the
  // non-text indicator. Both products declare them, and the ERP dark badge
  // defect lived in exactly this pair, so parity has to cover it.
  "--atlas-success-text",
  "--atlas-success-indicator",
  "--atlas-warning-fg",
  "--atlas-warning-bg",
  "--atlas-danger-fg",
  "--atlas-danger-bg",
  "--atlas-info-fg",
  "--atlas-info-bg",
];

// Repo-local config, resolved against this file's directory. The two copies of
// this script are identical apart from these four lines.
const SELF = { name: "atlas-erp", bridge: "../frontend/src/index.css" };
const SIBLING = {
  name: "atlas-ecom",
  bridge: "../../atlas-ecom/frontend/src/styles/atlas-tokens.css",
};

const BASE = dirname(fileURLToPath(import.meta.url));
const MODES = ["light", "dark"];

/**
 * Every `--atlas-*` declaration in the `:root` and `.dark` rule bodies, as
 * `{light: [[name, value]], dark: [...]}`, in source order and with duplicates
 * kept. Comments go first so a colour quoted in prose is never read as a
 * declaration; a selector list such as `:root, .dark` feeds both modes, which
 * is what the cascade does.
 */
function readBridge(file) {
  const css = readFileSync(join(BASE, file), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
  const modes = { light: [], dark: [] };
  let i = 0;
  for (let open = css.indexOf("{"); open !== -1; open = css.indexOf("{", i)) {
    // The prelude starts after the last `;`, so an at-rule such as
    // `@custom-variant dark (...)` cannot be glued onto the selector after it.
    const prelude = css.slice(i, open);
    const selectors = prelude.slice(prelude.lastIndexOf(";") + 1).split(",").map((s) => s.trim());
    let depth = 0;
    let close = open;
    for (; close < css.length; close++) {
      if (css[close] === "{") depth++;
      else if (css[close] === "}" && --depth === 0) break;
    }
    if (selectors.every((s) => s === ":root" || s === ".dark")) {
      for (const selector of selectors) {
        const mode = selector === ":root" ? "light" : "dark";
        for (const [, name, value] of css
          .slice(open + 1, close)
          .matchAll(/(--atlas-[a-z-]+)\s*:\s*([^;]+);/g)) {
          modes[mode].push([name, value.trim().toLowerCase()]);
        }
      }
    }
    i = close + 1;
  }
  return modes;
}

const values = (bridge, mode, token) =>
  bridge[mode].filter(([name]) => name === token).map(([, value]) => value);

let bridges;
try {
  bridges = { local: readBridge(SELF.bridge), sibling: readBridge(SIBLING.bridge) };
} catch (error) {
  console.error(`token parity: cannot read a token bridge: ${error.message}`);
  console.error("  both repos must be checked out side by side in ../");
  process.exit(2);
}

const problems = [];
for (const token of TOKENS) {
  for (const mode of MODES) {
    const mine = values(bridges.local, mode, token);
    const theirs = values(bridges.sibling, mode, token);
    if (mine.length !== 1 || theirs.length !== 1) {
      problems.push(
        `${mode} ${token}\n` +
          `  ${SELF.name.padEnd(10)} ${mine.length === 0 ? "MISSING" : mine.join(" | ")}\n` +
          `  ${SIBLING.name.padEnd(10)} ${theirs.length === 0 ? "MISSING" : theirs.join(" | ")}` +
          (mine.length > 1 || theirs.length > 1 ? "\n  expected exactly one declaration per mode" : ""),
      );
    } else if (mine[0] !== theirs[0]) {
      problems.push(
        `${mode} ${token}\n` +
          `  ${SELF.name.padEnd(10)} ${mine[0]}\n` +
          `  ${SIBLING.name.padEnd(10)} ${theirs[0]}`,
      );
    }
  }
}

if (problems.length > 0) {
  console.error(`token parity: FAILED, ${problems.length} of ${TOKENS.length * MODES.length} checks\n`);
  console.error(problems.join("\n\n"));
  console.error(`\n${SELF.name} and ${SIBLING.name} share one token table. Fix both bridges or neither.`);
  process.exit(1);
}

console.log(
  `token parity: OK  ${TOKENS.length} contract tokens x ${MODES.length} modes, ` +
    `one declaration each, ${SELF.name} and ${SIBLING.name} identical`,
);
