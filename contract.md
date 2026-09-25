# Atlas ERP Contract

## Metadata

- Product: Atlas ERP
- Contract version: 1.0.0
- Status: ACTIVE
- Date: 2026-09-25
- Approval date: 2026-09-25

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

## Data

- Atlas ERP owns its PostgreSQL database, named `atlas_erp`.
- It does not share tables with another application.
- Local business data and domain state remain in the local database.
- A durable local outbox is used for protocol events that need delivery to a
  connected peer.
- Peers exchange data through the protocol; they do not read or write each
  other's tables directly.

## Acceptance gates

1. **Standalone business path:** purchasing leads to stock, a manual sale, and a
   general-ledger entry without a connected commerce channel.
2. **Console:** the local console can operate the baseline capabilities.
3. **Connect conformance:** pairing, authority exchange, snapshot plus ordered
   deltas, proposals, and degradation/resynchronization behave according to
   the vendored protocol specification.
4. **Authority:** the default is share-only, and any single-master adoption or
   import is explicit and unambiguous.

## Risks and unknowns

- The exact module boundaries and APIs for the baseline clusters are not yet
  implemented.
- Authority changes and explicit adoption need an auditable policy before
  implementation.
- Partial protocol rollout must be detected by the version handshake without
  making Atlas ERP dependent on a peer.
- Operational behavior during prolonged disconnection and resynchronization
  needs validation with representative data volumes.

## Amendment history

| Version | Date | Change | Status |
| --- | --- | --- | --- |
| 1.0.0 | 2026-09-25 | Initial approved standalone ERP contract, baseline clusters, database ownership, and embedded connect defaults. | ACTIVE |
