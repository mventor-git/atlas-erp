# Atlas ERP operator console

The operator console frontend: a separate Vite + React 19 + TypeScript build that
reads the Atlas ERP Python API and writes nothing. It satisfies
`contract.md` v1.2.0 acceptance gate 6.

The Python package in the parent directory is untouched and remains the only
owner of the data. This build has its own dependencies, its own lockfile, and its
own output directory, so the Node surface stays out of the Python import and
test path.

## Requirements

- Node.js 20.19+ or 22.12+ (developed on Node 24.19.0 / npm 12.0.2)
- The ERP console server running, for the API to answer

## Commands

```bash
npm install        # install dependencies (writes package-lock.json)
npm run dev        # Vite dev server, http://localhost:5173
npm run build      # typecheck with tsc -b, then production build into dist/
npm run preview    # serve the built output
npm run typecheck  # tsc -b only
npm test           # Vitest, one run
npm run lint       # oxlint, warnings denied
```

## The API proxy

`npm run dev` proxies `/api` to `http://127.0.0.1:4311` (see the `server.proxy`
block in `vite.config.ts`), so the browser sees a single origin and the console
server can stay bound to loopback. The proxy is a dev-server convenience only.

The console reads three read-only routes and sends no other request:

| Route | Payload |
| --- | --- |
| `GET /api/health` | `atlas_erp.web.health_payload` |
| `GET /api/catalog` | `atlas_erp.demo_catalog.DemoCatalog.to_dict` |
| `GET /api/audit` | `atlas_erp.business.AuditSnapshot.to_dict` |

Start the API in a second terminal from the repository root:

```bash
python -m atlas_erp.web     # 127.0.0.1:4311, override with ATLAS_ERP_WEB_PORT
```

Without that process every panel shows a failure with a retry, which is the
expected behaviour: there is no bundled fixture and no mock data in the app.

`npm run build` produces static files. Nothing serves them for you; the built
`dist/` is a separate deployment decision, and the API still has to be reachable
at whatever origin you serve it from (the `/api` prefix has to keep working, or
`src/lib/api.ts` has to name an absolute URL).

## Design tokens

`src/index.css` is the token bridge and the **only** file in this build allowed
to contain a raw colour value. It declares the 18 `--atlas-*` tokens from
`contract.md` v1.1.1 once per mode, then maps the shadcn/Tailwind variables onto
them as `contract.md` v1.2.0 requires. Components use semantic classes and
variables only.

- Mode is one class on `<html>`: `.dark` selects the dark block, and
  `index.html` applies the persisted or system mode before first paint so the
  page never flashes.
- `fg.muted` and `border.control` are derived against `bg.canvas`. Inside a card
  or tile (`data-surface="true"`) the bridge re-points `--muted-foreground` and
  `--border` so secondary text and control borders stay readable on a surface.
- `state.success` is the one documented deviation: the supplied light foreground
  on `state.success.bg` measures 3.38:1, so `--success-text` uses the accessible
  ink and `--success-indicator` keeps the supplied foreground for the non-text
  marker.

`src/tokens.test.ts` transcribes the contract's table and fails on a changed
value, a missing mapping, or a raw colour anywhere outside those two blocks.

## shadcn/ui components

`src/components/ui/` holds shadcn/ui components copied into this product's
source tree, in the shape the shadcn CLI writes, on the unified `radix-ui`
package. They are owned here, not consumed as a runtime package, so a shadcn
upgrade or a local edit is applied deliberately per product — the same cost both
Atlas products now carry.

`components.json` is present so `npx shadcn add <component>` works in this tree.
The CLI was not run to generate these files; they were authored to its output
shape. Run it and diff before trusting that claim.

## Tests

Vitest with jsdom and Testing Library. Coverage is deliberately narrow:

- `src/lib/api.test.ts` — payload parsing and the fetch failure modes
- `src/components/console/console.test.tsx` — header, banner, health, KPI tiles,
  catalogue, purchasing, activity, plus loading, error, retry, and empty states,
  and a check that the console never sends anything but a GET
- `src/lib/theme.test.tsx` — light/dark/system, the `.dark` class, persistence
- `src/tokens.test.ts` — the token table, the shadcn mapping, and the no-raw-colour
  rule

## Known limits

- The API exposes per-variant stock totals and item-level audit stock, but no
  per-warehouse split, so the Stock tab shows the two views the API does supply
  and lists the fulfilment centres without inventing a distribution between them.
  A real per-warehouse matrix needs `DemoCatalog.to_dict()` to carry `stock` per
  variant per warehouse, which is a Python change.
- Everything on the page is demo fixture and process-local state, gone when the
  Python process exits.
- The catalogue images are remote Unsplash placeholder links with no committed
  files, licences, or attribution.
