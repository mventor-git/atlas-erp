# Atlas Connect Protocol Specification

## Metadata

- Protocol: Atlas Connect
- Specification version: 1.0.0
- Status: vendored shared protocol
- Vendored by: Atlas ERP and Atlas Ecom
- Authoring: authored once and vendored into both products for now
- Date: 2026-09-25
- Approval date: 2026-09-25

## Purpose

Atlas Connect is a peer-to-peer protocol for exchanging approved capabilities
between products. It reads the clusters, plugins, modules, and permissions
that each product already exposes, then bridges those capabilities without
making one product a required dependency of another. The protocol is embedded
and shipped in every product; it is not a repository, server, or service of
its own.

## Vocabulary

- **Peer:** an independently running product that embeds Atlas Connect.
- **Cluster:** a product-level capability area containing plugins and modules.
- **Plugin:** a unit of product functionality within a cluster.
- **Module:** the smallest independently described unit that can expose a
  capability or participate in synchronization.
- **Capability:** a named, permission-scoped operation or data projection.
- **Manifest:** the exchanged description of a peer's identity, protocol
  version, capabilities, and permissions.
- **Authority:** the role that determines who may read or write a capability.
- **Snapshot:** a complete, positionable view of a capability at a point in
  synchronization.
- **Delta:** an ordered change after a snapshot or another delta.
- **Proposal:** a request for the capability master to accept or reject a
  proposed change.
- **Inbox:** the local durable record used to make incoming events idempotent.
- **Outbox:** the local durable record of events waiting for delivery.

## Guarantees

- **G1 — additive-only:** new capabilities and protocol fields must be
  additive. A peer that does not implement an optional capability remains
  usable, and Atlas Connect is never a required dependency.
- **G2 — single master per capability:** at any time each capability has one
  explicit master. Readers and proposers never become masters implicitly.
- **G3 — version handshake:** peers exchange and validate the protocol version
  before exchanging capability data; incompatible or partial rollout is
  surfaced rather than silently ignored.
- **G4 — no direct table access:** peers exchange snapshots, deltas, and
  proposals through the protocol and never read or write another peer's
  database tables directly.

## Connection states

A connection progresses through these states:

`unpaired -> pairing -> connected -> degraded -> revoked`

- **unpaired:** no trusted peer relationship exists.
- **pairing:** the one-time pairing exchange and manifest handshake are being
  established.
- **connected:** the handshake succeeded and the agreed capabilities are
  available.
- **degraded:** one or more agreed capabilities cannot currently be exchanged;
  local operation continues and resynchronization is possible.
- **revoked:** the scoped credential or peer authorization has been revoked.

## Pairing

Pairing uses a one-time code together with a scoped HTTPS credential. The
code authorizes the initial exchange only; the scoped credential is then used
for the peer relationship. Pairing does not grant authority over capabilities
that were not explicitly shared.

## Manifest exchange

Each peer exchanges a manifest containing at least:

- `app_id`: the stable identity of the product peer.
- `connect_version`: the Atlas Connect protocol version it implements.
- `capabilities`: the capabilities the peer offers or consumes.
- `permissions`: the scoped permissions requested or granted for those
  capabilities.

The manifest is exchanged as part of the version handshake. A peer must not
assume that an absent optional capability is a reason to disable the product.

## Authority

Each capability uses one of these authority roles:

- **master:** the sole authority allowed to change the capability.
- **reader:** an allowed peer that may read shared capability data.
- **proposer:** a peer that may submit proposals but cannot change the
  capability directly.

The default is **share-only**: a newly connected peer can receive or expose
only what is explicitly shared. Adoption or import is a separate explicit
operation and must name the capability master; it is not implied by pairing or
by reading data.

## Synchronization primitives

The v1 exchange uses:

- A **snapshot** followed by **ordered deltas**.
- An **opaque cursor** to identify a synchronization position. The cursor's
  internal representation is not part of the protocol contract.
- An **idempotent inbox** keyed by `event_id`, so retries do not apply an event
  twice.
- A **durable outbox**, so locally committed events survive restart and retry.
- **Proposals** with the states `pending`, `accepted`, and `rejected`.

A reader may consume a snapshot and subsequent deltas. A proposer submits a
proposal for the master to decide. Only the master may apply an authoritative
write.

## Permissions and RBAC

Version 1 supports scoped permission grants. Full RBAC federation across
products is deferred; a connected peer receives only the scopes granted for
the capability and connection.

## Conformance targets

Atlas ERP and Atlas Ecom must each embed this same protocol module and pass
conformance checks for:

- one-time pairing and scoped credentials;
- manifest exchange and version handshake;
- explicit master, reader, and proposer authority;
- snapshot plus ordered delta synchronization with an opaque cursor;
- idempotent inbox and durable outbox behavior;
- proposal state transitions; and
- degradation and resynchronization without breaking standalone operation.

The protocol is optional at the product level: conformance is required for
connect behavior, not for running the product with no peer.

## Non-goals

Atlas Connect does not provide:

- a server or broker;
- hot loading;
- a global registry;
- automatic migration; or
- automatic discovery.

## Shared-update rule

The same Atlas Connect module version is vendored into all products. A release
updates the shared module everywhere, and the version handshake catches a
partial rollout or incompatible peer. Products must not silently mix protocol
implementations.

## Risks and unknowns

- The exact capability catalog and permission vocabulary need implementation
  testing across ERP and Ecom.
- Authority changes and explicit adoption need auditable conflict handling.
- Cursor retention, delta ordering, and replay behavior need defined limits.
- The fallback behavior during prolonged degradation and resynchronization
  needs validation with realistic data volumes.

## Amendment history

| Version | Date | Change | Status |
| --- | --- | --- | --- |
| 1.0.0 | 2026-09-25 | Initial approved vendored shared protocol covering pairing, authority, synchronization, proposals, and degradation. | vendored shared protocol |
