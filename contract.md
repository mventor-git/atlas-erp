# Atlas ERP Contract

## Metadata

- Product: Atlas ERP
- Contract version: 1.3.0
- Status: ACTIVE
- Date: 2026-09-26
- Approval date: 2026-09-26

## Vision

Atlas ERP is a general-purpose, standalone ERP application. It is a
cluster-of-plugins that can perform as a complete application without a
connected commerce channel. Its baseline capabilities are useful on their own
and remain available when no peer is connected.

## Origin

Atlas ERP was lifted out of Atlas HQ because it can perform as a standalone
application. It is an independent product boundary, not a mode or optional
module of Atlas HQ. The intended implementation is Python, but no Python
implementation is included in this first version.

## Legacy knowledge transfer

The legacy `ecom-erp` project is the evidence base for this contract, not its
authority. It is a read-only reference: this product takes no code, schema,
route, or UI from it.

- Adopt: the invariants, requirements, and tests a legacy area proved. Each is
  restated here as a contract of this product and, where it is a rule, as a test
  that runs in this repository.
- Redesign: the mechanism behind each adopted requirement is designed inside
  `core -> cluster -> plugin -> module` in this product, against this
  product's own database and console. A legacy implementation is never carried
  over.
- Drop: legacy accidents. Competing sources of truth, unnormalized order
  payloads, mutable money, status-only settlement, per-process caches, and
  inline migration hacks are known defects and are not inherited.
- No source, schema, route, or UI copy is permitted, and no legacy table is
  read, imported, or migrated. Legacy data is still not imported.
- Where a legacy behavior is ambiguous, this contract decides and the legacy
  repository is cited as evidence for the requirement only.

## Goals

- Provide a useful ERP application with no connected peer required.
- Support the baseline masterdata, inventory, purchasing, sales, finance, and
  admin clusters.
- Give operators a local console for the product's own capabilities.
- Embed the shared Atlas Connect protocol as an additive, optional capability.
- Keep data ownership and cross-application authority explicit.

## Non-goals

- Requires no connected commerce channel.
- Defines no storefront, cart, checkout, or payment capability.
- Does not import legacy SQLite data.
- Does not assume that the other application is Atlas Ecom.

## Users

- Operators who manage purchasing, stock, sales, and finance workflows.
- Administrators who configure clusters, plugins, modules, and users.
- Integrators who connect approved capabilities without taking ownership of
  local data.

## Architecture

The product hierarchy is:

`core -> cluster -> plugin -> module`

The v1 baseline clusters are `masterdata`, `inventory`, `purchasing`, `sales`,
`finance`, and `admin`. Atlas ERP has its own console and embeds the vendored
`connect/SPEC.md`. Each cluster must be able to operate as part of the
standalone application; Atlas Connect is additive and is never a required
runtime dependency.

Connect is share-only by default. Adoption or import of data owned by another
application is a separate, explicit operation.

## Evolution map

This map names candidate modules only. It adds no cluster, no product
boundary, and no dependency, and a candidate is not approved scope.

| Cluster | Candidate modules | Status |
| --- | --- | --- |
| `finance` | `journal` (balanced entries, posted-only truth), `period` (open and
  close, fail-closed while closed), `reconciliation` (operational state against
  posted journals) | candidate |
| `inventory` | `movement` (ledger of every quantity change), `valuation` (cost
  captured per movement, never restated), `replay` (rebuild state from
  movements) | candidate |
| `purchasing` | `numbering` (document sequences per document kind), `import`
  (external purchase data into purchasing) | candidate |
| pricing, operations | price lists, markup, and cost policy; picking, packing, and
  delivery | candidate decision, not active scope |

The requirements behind these candidates are the ones adopted from legacy
knowledge; the mechanisms are not. Nothing in this table is approved scope until
an amendment promotes it, and a promoted module still ships only against the
transfer gate below.

## Design tokens

Presentation uses a shared Atlas UI token set, exposed as named tokens or CSS
variables. The token names and values are the same on every Atlas product
surface, so a surface moved between products keeps its appearance. The table
below is the single shared reference: one name, one meaning, one value per
mode, in both products.

| Token | Role | Light | Dark |
| --- | --- | --- | --- |
| `bg.canvas` | Page background | `#F7F2EB` | `#41444B` |
| `bg.surface` | Card/surface | `#EAE2D6` | `#52575D` |
| `fg.default` | Body text | `#2D0000` | `#DFD8C8` |
| `fg.muted` | Muted text | `#6A2F2F` (derived) | `#B7B3A9` (derived) |
| `accent.default` | Accent | `#8B9A6E` | `#CABFAB` |
| `link.default` | Link | `#2D0000` + underline | `#DFD8C8` + underline |
| `border.divider` | Divider | `#EEEEEE` | `#52575D` |
| `border.control` | Control border | `#757D6F` | `#9AA394` (derived) |
| `onAccent.default` | Text on accent | `#2D0000` | `#41444B` |
| `focus.ring` | Focus ring | `#2D0000` | `#DFD8C8` |
| `state.success` | Success | `#2A7C13` on `#C7D3C0` | `#2D0000` on `#C7D3C0` |
| `state.warning` | Warning | `#2D0000` on `#C8A96B` | `#2D0000` on `#C8A96B` |
| `state.danger` | Danger | `#6D0808` on `#FFDADA` | `#2D0000` on `#FFDADA` |
| `state.info` | Info | `#2D0000` on `#FBE6C2` | `#2D0000` on `#FBE6C2` |

### Token mapping

- `accent.default` is an accent and is never body text on `bg.canvas`; body
  text is `fg.default` only.
- `link.default` carries an underline and does not rely on color alone.
- `border.divider` is decorative and low-contrast by design; it separates
  content and is never a text color.
- `focus.ring` must stay visible on the surface it is drawn on, and is
  applied to every keyboard-focusable control.
- Each `state.*` token is a foreground-on-background pair and is used as
  supplied.
- Tokens are overridable, and overriding one must not change what any value
  means to the data.
- Token parity is an acceptance requirement, checked across both products'
  frontends: the single shared token table above is the reference, each
  product's bridge resolves to it in both modes, no raw hex value exists
  outside a product's token bridge, and a parity failure blocks the change
  rather than being reported after the fact.

### Component foundation

shadcn/ui (Radix/Base UI + Tailwind) is the preferred component foundation
because it consumes CSS variables and preserves markup ownership; it is
replaceable and the token contract is authoritative.

### Contrast

- The target is WCAG AA in both light and dark mode.
- Three values are derived rather than supplied, and are the ones the contrast
  check returned: light `fg.muted` `#6A2F2F` (9.15:1 on `#F7F2EB`), dark
  `fg.muted` `#B7B3A9` (4.66:1 on `#41444B`), and dark `border.control`
  `#9AA394` (3.73:1 on `#41444B`). Every other value is as supplied.
- Caveat: the light `state.success` pair `#2A7C13` on `#C7D3C0` measures
  3.38:1. It is preserved as supplied and is for non-text and large-text use
  only; for normal text on that background, pair it with `#2D0000` instead.

## UI implementation direction

The approved frontend direction for the operator console is React 19 +
TypeScript + Vite + Tailwind CSS v4 + shadcn/ui on Radix UI primitives.
shadcn components are copied into this product's source tree and owned here,
not consumed as a shared runtime package, so a product never depends on
another product's components to build.

- The console is a separate frontend build with its own dependencies and build
  output. The existing Python API and its routes are unchanged, `atlas_erp`
  keeps sole ownership of its database, and the frontend reads that API as its
  only data source.
- The v1.1.1 token table above remains the semantic authority. shadcn and
  Tailwind variables are a derived mapping onto that table, not a second
  source of colour truth, and no component file contains a raw hex value.
- The required mapping is fixed, so the two products stay visually identical
  on shared surfaces:

| shadcn / Tailwind variable | Shared token |
| --- | --- |
| `--background` | `--atlas-bg-canvas` (`bg.canvas`) |
| `--foreground` | `--atlas-fg-default` (`fg.default`) |
| `--card` | `--atlas-bg-surface` (`bg.surface`) |
| `--primary` | `--atlas-accent` (`accent.default`) |
| `--primary-foreground` | `--atlas-on-accent` (`onAccent.default`) |
| `--muted-foreground` | `--atlas-fg-muted` (`fg.muted`), canvas use only |
| `--border` | `--atlas-border-divider` (`border.divider`) |
| `--input` | `--atlas-border-control` (`border.control`) |
| `--ring` | `--atlas-focus-ring` (`focus.ring`) |

- The four shadcn state variables (`--success`, `--warning`, `--danger`,
  `--info`) resolve to the `state.*` token pairs above, as a foreground on a
  background. Within `state.success`, `state.success.text` uses the accessible
  ink `#2D0000` in both modes, because the supplied light foreground
  `#2A7C13` on `#C7D3C0` is 3.38:1 and is not a normal-text pair, and
  `state.success.indicator` is the non-text marker carrying the supplied
  foreground.
- Light and dark mode, `:focus-visible` behavior driven by `focus.ring`,
  keyboard operability of every interactive component, and WCAG AA in both
  modes are required, not optional.
- Adopting shadcn/ui is an implementation direction for the frontend only. It
  is not permission to rewrite the backend, to move domain logic into the
  browser, or to share a database with another product.

## Data

- Atlas ERP owns its PostgreSQL database, named `atlas_erp`.
- It does not share tables with another application.
- Local business data and domain state remain in the local database.
- A durable local outbox is used for protocol events that need delivery to a
  connected peer.
- Peers exchange data through the protocol; they do not read or write each
  other's tables directly.

### Cross-product ownership

- Atlas ERP owns sale recognition, stock truth, and journals. What was sold, at
  what quantity, and what it is worth in the books are decided here.
- A connected commerce product owns the order, payment, and fulfilment intent.
  It submits intent; it does not decide recognition, quantity, or value.
- Stock arriving from a peer is a proposal or a read, never an authoritative
  write. A peer's local quantity is a sellable projection, not inventory
  authority, and stock stays correct with no peer connected.
- An owner change is a contract amendment, not an implementation detail.

## Acceptance gates

1. **Standalone business path:** purchasing leads to stock, a manual sale, and a
   general-ledger entry without a connected commerce channel.
2. **Console:** the local console can operate the baseline capabilities.
3. **Connect conformance:** pairing, authority exchange, snapshot plus ordered
   deltas, proposals, and degradation/resynchronization behave according to
   the vendored protocol specification.
4. **Authority:** the default is share-only, and any single-master adoption or
   import is explicit and unambiguous.
5. **Design tokens:** the operator console is styled from the shared token
   table above in both light and dark mode. Every background, surface, body
   and muted text color, accent, link, divider, control border, on-accent
   color, focus ring, and state pair resolves to the named token rather than to
   a hard-coded color, and text and controls meet WCAG AA in both modes. The
   component foundation behind the markup is not fixed by this gate.
6. **Operator console frontend:** the operator console is a separate Vite +
   React + TypeScript frontend build that uses the approved shadcn/ui and
   Tailwind CSS v4 direction, resolves the shared token table above through the
   required shadcn variable mapping in both light and dark mode with no raw hex
   value in a component, and reads every value it displays from the Python API,
   which stays its only data source.

7. **Legacy transfer:** a capability whose requirement was adopted from legacy
   knowledge ships only with all of: a public seam naming what it exposes, a
   stated invariant, a named owning cluster, explicit failure semantics, a real
   test that runs, and defined UI states for loading, empty, error, and success.
   An absent, skipped, or stubbed test is not evidence for the gate.

## Risks and unknowns

- The exact module boundaries and APIs for the baseline clusters are not yet
  implemented.
- Authority changes and explicit adoption need an auditable policy before
  implementation.
- Partial protocol rollout must be detected by the version handshake without
  making Atlas ERP dependent on a peer.
- Operational behavior during prolonged disconnection and resynchronization
  needs validation with representative data volumes.
- The current demo console demonstrates the shared tokens and light/dark
  behavior at the token level, but it is a server-rendered demo with no build
  step, so it does not satisfy the new separate Vite/React/shadcn frontend gate
  until it is migrated.
- The light `state.success` pair `#2A7C13` on `#C7D3C0` is 3.38:1 and is not a
  normal-text pair; normal text on that background has to use `#2D0000`.
- The approved console direction adds a Node build surface to a product whose
  Python package deliberately has no build step and no runtime third-party
  import on its standalone paths. A lockfile, a dependency tree, and generated
  build output have to be kept out of the standalone import and test path.
- shadcn components are generated into this product's source tree and into the
  sibling product's tree separately, so the same component exists twice and can
  drift. A shadcn upgrade or a local component edit has to be applied
  deliberately per product, and there is no shared package to upgrade once.
- Token parity between the two products is checked by
  `scripts/check-token-parity.mjs`, which resolves both shadcn variable
  mappings to the shared token values in both modes and is wired to the
  `tokens:parity` script, so a mapping edited in one product and not the other
  fails that check instead of drifting silently. The risk stays open because
  no automated run invokes the check and it compares the two bridges to each
  other rather than to the token table above.

## Amendment history

| Version | Date | Change | Status |
| --- | --- | --- | --- |
| 1.0.0 | 2026-09-25 | Initial approved standalone ERP contract, baseline clusters, database ownership, and embedded connect defaults. | ACTIVE |
| 1.1.0 | 2026-09-25 | Approved shared Atlas UI palette and light/dark design-token contract. | ACTIVE |
| 1.1.1 | 2026-09-25 | Expanded shared UI token table, derived contrast-safe values, and preferred shadcn/ui foundation. | ACTIVE |
| 1.2.0 | 2026-09-25 | Approved React/Vite/Tailwind/shadcn frontend direction and token mapping for both products. | ACTIVE |
| 1.3.0 | 2026-09-26 | Approved legacy knowledge transfer rule, evolution map, cross-product ownership, token-parity requirement, and legacy transfer gate. | ACTIVE |
