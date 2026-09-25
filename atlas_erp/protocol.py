"""Small Atlas Connect protocol kernel.

This module contains the protocol semantics needed by the first executable
slice.  The adapter is intentionally process-local and is a protocol/test
fixture, not production persistence.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from secrets import token_urlsafe
from typing import cast
from uuid import uuid4

from .registry import DEFAULT_CONNECT_VERSION, Registry


CONNECT_VERSION = DEFAULT_CONNECT_VERSION
_AUTHORITY_ROLES = frozenset({"master", "reader", "proposer"})


class ProtocolError(ValueError):
    """Base class for protocol conformance errors."""


class IncompatibleVersionError(ProtocolError):
    """Raised when peers do not share a protocol major version."""


class AuthorityConflictError(ProtocolError):
    """Raised when a capability would have zero or multiple masters."""


class InvalidCursorError(ProtocolError):
    """Raised when a synchronization cursor is unknown or for another scope."""


class PermissionDeniedError(ProtocolError):
    """Raised when a peer attempts an operation outside its authority."""


class ProposalError(ProtocolError):
    """Raised for invalid proposal transitions or missing reasons."""


def _text(value: object, kind: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProtocolError(f"{kind} must be a non-empty string")
    return value.strip()


def major_version(version: str) -> int:
    """Return the numeric major component of a dotted protocol version."""
    version = _text(version, "version")
    major = version.split(".", 1)[0]
    if not major.isdigit():
        raise ProtocolError(f"invalid protocol version: {version}")
    return int(major)


def compatible_version(local_version: str, peer_version: str) -> bool:
    """Return whether two protocol versions have the same major version."""
    return major_version(local_version) == major_version(peer_version)


def handshake(local_version: str, peer_version: str) -> dict[str, object]:
    """Validate the version handshake and return its accepted result."""
    if not compatible_version(local_version, peer_version):
        raise IncompatibleVersionError(
            f"incompatible protocol major versions: {local_version} and {peer_version}"
        )
    return {
        "accepted": True,
        "local_version": local_version,
        "peer_version": peer_version,
        "major": major_version(local_version),
    }


def validate_authorities(
    authorities: Mapping[str, Mapping[str, str]],
) -> dict[str, dict[str, str]]:
    """Validate explicit per-capability authority assignments.

    Each capability must have exactly one master.  Readers and proposers may
    coexist, but they never become masters implicitly.
    """
    if not isinstance(authorities, Mapping):
        raise AuthorityConflictError("authorities must map capabilities to peers")

    normalized: dict[str, dict[str, str]] = {}
    for capability, assignments in authorities.items():
        capability = _text(capability, "capability")
        if not isinstance(assignments, Mapping):
            raise AuthorityConflictError(
                f"authority for {capability} must map peers to roles"
            )
        peers: dict[str, str] = {}
        for peer_id, role in assignments.items():
            peer_id = _text(peer_id, "peer_id")
            if role not in _AUTHORITY_ROLES:
                raise AuthorityConflictError(
                    f"unknown authority role for {capability}: {role!r}"
                )
            peers[peer_id] = role
        masters = [peer_id for peer_id, role in peers.items() if role == "master"]
        if len(masters) != 1:
            raise AuthorityConflictError(
                f"{capability} must have exactly one master; found {len(masters)}"
            )
        normalized[capability] = peers
    return normalized


def _manifest_capabilities(manifest: Mapping[str, object]) -> tuple[str, ...]:
    raw = manifest.get("capabilities", ())
    if isinstance(raw, Mapping):
        raw_names: Iterable[object] = raw.keys()
    else:
        try:
            raw_names = tuple(cast(Iterable[object], raw))
        except TypeError as exc:
            raise ProtocolError("manifest capabilities must be an iterable") from exc

    names: list[str] = []
    for item in raw_names:
        if isinstance(item, Mapping):
            item = item.get("name")
        name = _text(item, "manifest capability")
        if name not in names:
            names.append(name)
    return tuple(names)


@dataclass(frozen=True)
class Snapshot:
    """A complete capability view and its opaque synchronization cursor."""

    capability: str
    data: object
    cursor: str


@dataclass(frozen=True)
class Delta:
    """An ordered capability change after a snapshot or another delta."""

    capability: str
    event_id: str
    payload: object
    cursor: str


@dataclass(frozen=True)
class Proposal:
    """The state and reason of a capability proposal."""

    proposal_id: str
    capability: str
    payload: object
    state: str
    reason: str | None


class InMemoryProtocolAdapter:
    """Protocol/test adapter with process-local state only.

    This adapter deliberately does not claim durability and is not suitable for
    production persistence.  It provides the small synchronization, inbox, and
    proposal primitives needed to run conformance tests.
    """

    def __init__(self) -> None:
        self._state: dict[str, object] = {}
        self._latest_cursors: dict[str, str] = {}
        self._cursor_positions: dict[str, tuple[str, int]] = {}
        self._next_positions: dict[str, int] = {}
        self._deltas: dict[str, list[tuple[int, Delta]]] = {}
        self._published_event_ids: set[str] = set()
        self._inbox: set[str] = set()
        self._proposals: dict[str, Proposal] = {}

    @property
    def inbox(self) -> frozenset[str]:
        return frozenset(self._inbox)

    @property
    def proposals(self) -> Mapping[str, Proposal]:
        return dict(self._proposals)

    def _new_cursor(self, capability: str, position: int) -> str:
        cursor = token_urlsafe(18)
        self._cursor_positions[cursor] = (capability, position)
        return cursor

    def _ensure_capability(self, capability: str) -> None:
        if capability not in self._state:
            self._state[capability] = None
            self._latest_cursors[capability] = self._new_cursor(capability, 0)
            self._next_positions[capability] = 0
            self._deltas[capability] = []

    def seed(self, capability: str, data: object) -> Snapshot:
        capability = _text(capability, "capability")
        if capability in self._state:
            raise ProtocolError(f"capability is already initialized: {capability}")
        self._state[capability] = deepcopy(data)
        self._latest_cursors[capability] = self._new_cursor(capability, 0)
        self._next_positions[capability] = 0
        self._deltas[capability] = []
        return self.snapshot(capability)

    def snapshot(self, capability: str) -> Snapshot:
        capability = _text(capability, "capability")
        self._ensure_capability(capability)
        return Snapshot(
            capability,
            deepcopy(self._state[capability]),
            self._latest_cursors[capability],
        )

    def append_delta(
        self, capability: str, event_id: str, payload: object
    ) -> Delta:
        capability = _text(capability, "capability")
        event_id = _text(event_id, "event_id")
        self._ensure_capability(capability)
        if event_id in self._published_event_ids:
            raise ProtocolError(f"event_id was already published: {event_id}")
        position = self._next_positions[capability] + 1
        cursor = self._new_cursor(capability, position)
        delta = Delta(capability, event_id, deepcopy(payload), cursor)
        self._deltas[capability].append((position, delta))
        self._next_positions[capability] = position
        self._latest_cursors[capability] = cursor
        self._state[capability] = deepcopy(payload)
        self._published_event_ids.add(event_id)
        return delta

    def deltas(self, capability: str, cursor: str) -> list[Delta]:
        capability = _text(capability, "capability")
        self._ensure_capability(capability)
        try:
            cursor_capability, position = self._cursor_positions[cursor]
        except (KeyError, TypeError) as exc:
            raise InvalidCursorError("unknown synchronization cursor") from exc
        if cursor_capability != capability:
            raise InvalidCursorError("cursor belongs to another capability")
        return [
            delta
            for delta_position, delta in self._deltas[capability]
            if delta_position > position
        ]

    def accept_event(self, delta: Delta) -> bool:
        """Record an event in the idempotent inbox, returning false on retry."""
        if not isinstance(delta, Delta):
            raise TypeError("delta must be a Delta")
        if not delta.event_id:
            raise ProtocolError("event_id must be non-empty")
        if delta.event_id in self._inbox:
            return False
        self._inbox.add(delta.event_id)
        return True

    def create_proposal(
        self,
        capability: str,
        payload: object,
        proposal_id: str | None = None,
    ) -> Proposal:
        capability = _text(capability, "capability")
        proposal_id = proposal_id or uuid4().hex
        proposal_id = _text(proposal_id, "proposal_id")
        if proposal_id in self._proposals:
            raise ProposalError(f"proposal already exists: {proposal_id}")
        proposal = Proposal(
            proposal_id,
            capability,
            deepcopy(payload),
            "pending",
            None,
        )
        self._proposals[proposal_id] = proposal
        return proposal

    def get_proposal(self, proposal_id: str) -> Proposal:
        try:
            return self._proposals[proposal_id]
        except KeyError as exc:
            raise ProposalError(f"unknown proposal: {proposal_id}") from exc

    def transition_proposal(
        self, proposal_id: str, state: str, reason: str
    ) -> Proposal:
        if state not in {"accepted", "rejected"}:
            raise ProposalError("proposal can only transition to accepted or rejected")
        reason = _text(reason, "proposal reason")
        current = self.get_proposal(proposal_id)
        if current.state != "pending":
            raise ProposalError(
                f"proposal is already {current.state}: {proposal_id}"
            )
        updated = replace(current, state=state, reason=reason)
        self._proposals[proposal_id] = updated
        return updated


class ProtocolKernel:
    """Connect protocol semantics layered over a protocol/test adapter."""

    def __init__(
        self,
        registry: Registry | None = None,
        adapter: InMemoryProtocolAdapter | None = None,
        *,
        app_id: str | None = None,
        connect_version: str | None = None,
    ) -> None:
        self.registry = registry if registry is not None else Registry()
        self.adapter = adapter if adapter is not None else InMemoryProtocolAdapter()
        self.app_id = app_id or self.registry.app_id
        self.connect_version = connect_version or self.registry.connect_version
        self._peer_manifest: dict[str, object] | None = None
        local_capabilities = cast(
            Iterable[str], self.registry.manifest()["capabilities"]
        )
        self._authorities: dict[str, dict[str, str]] = {
            capability: {self.app_id: "master"}
            for capability in local_capabilities
        }

    def manifest(self) -> dict[str, object]:
        manifest = self.registry.manifest()
        manifest["app_id"] = self.app_id
        manifest["connect_version"] = self.connect_version
        return manifest

    @property
    def peer_manifest(self) -> Mapping[str, object] | None:
        return None if self._peer_manifest is None else dict(self._peer_manifest)

    @property
    def authorities(self) -> Mapping[str, Mapping[str, str]]:
        return {
            capability: dict(assignments)
            for capability, assignments in self._authorities.items()
        }

    def pair(
        self,
        peer_manifest: Mapping[str, object],
        shared_capabilities: Iterable[str] = (),
    ) -> dict[str, object]:
        """Pair share-only; no data is adopted or moved as a side effect."""
        if not isinstance(peer_manifest, Mapping):
            raise ProtocolError("peer manifest must be a mapping")
        for field in ("app_id", "connect_version", "capabilities", "permissions"):
            if field not in peer_manifest:
                raise ProtocolError(f"peer manifest is missing {field}")
        peer_id = _text(peer_manifest["app_id"], "peer app_id")
        peer_version = _text(
            peer_manifest["connect_version"], "peer connect_version"
        )
        handshake(self.connect_version, peer_version)
        peer_capabilities = set(_manifest_capabilities(peer_manifest))
        local_capabilities = set(cast(Iterable[str], self.manifest()["capabilities"]))

        if isinstance(shared_capabilities, str):
            shared = (shared_capabilities,)
        else:
            shared = tuple(dict.fromkeys(shared_capabilities))
        for capability in shared:
            capability = _text(capability, "shared capability")
            if capability not in local_capabilities or capability not in peer_capabilities:
                raise ProtocolError(
                    f"shared capability is not offered by both peers: {capability}"
                )

        if self._peer_manifest is not None and self._peer_manifest["app_id"] != peer_id:
            raise ProtocolError("protocol kernel is already paired with another peer")
        self._peer_manifest = dict(peer_manifest)
        for capability in shared:
            self.grant(capability, peer_id, "reader")
        return {
            "peer_id": peer_id,
            "mode": "share-only",
            "shared_capabilities": tuple(shared),
            "adopted": False,
            "moved_data": False,
        }

    def grant(self, capability: str, peer_id: str, role: str = "reader") -> None:
        capability = _text(capability, "capability")
        peer_id = _text(peer_id, "peer_id")
        role = _text(role, "authority role")
        if capability not in self._authorities:
            raise ProtocolError(f"unknown capability: {capability}")
        assignments = dict(self._authorities[capability])
        assignments[peer_id] = role
        validated = validate_authorities({capability: assignments})[capability]
        self._authorities[capability] = validated

    def _require_role(
        self, capability: str, peer_id: str, required_role: str
    ) -> None:
        assignments = self._authorities.get(capability)
        if assignments is None or peer_id not in assignments:
            raise PermissionDeniedError(
                f"{peer_id} has no authority for {capability}"
            )
        role = assignments[peer_id]
        if role == required_role:
            return
        if required_role == "reader" and role == "master":
            return
        raise PermissionDeniedError(
            f"{peer_id} has {role} authority, not {required_role}, for {capability}"
        )

    def publish(self, capability: str, event_id: str, payload: object) -> Delta:
        capability = _text(capability, "capability")
        self._require_role(capability, self.app_id, "master")
        return self.adapter.append_delta(capability, event_id, payload)

    def snapshot(self, capability: str, peer_id: str | None = None) -> Snapshot:
        capability = _text(capability, "capability")
        self._require_role(capability, self.app_id if peer_id is None else peer_id, "reader")
        return self.adapter.snapshot(capability)

    def deltas(
        self, capability: str, cursor: str, peer_id: str | None = None
    ) -> list[Delta]:
        capability = _text(capability, "capability")
        self._require_role(capability, self.app_id if peer_id is None else peer_id, "reader")
        return self.adapter.deltas(capability, cursor)

    def receive_event(self, delta: Delta, peer_id: str | None = None) -> bool:
        if peer_id is not None:
            self._require_role(delta.capability, peer_id, "reader")
        return self.adapter.accept_event(delta)

    def submit_proposal(
        self,
        capability: str,
        payload: object,
        *,
        peer_id: str,
    ) -> Proposal:
        self._require_role(capability, peer_id, "proposer")
        return self.adapter.create_proposal(capability, payload)

    def resolve_proposal(
        self,
        proposal_id: str,
        state: str,
        reason: str,
        *,
        peer_id: str | None = None,
    ) -> Proposal:
        proposal = self.adapter.get_proposal(proposal_id)
        self._require_role(
            proposal.capability,
            self.app_id if peer_id is None else peer_id,
            "master",
        )
        return self.adapter.transition_proposal(proposal_id, state, reason)


# A descriptive alias for callers that prefer the product-specific name.
ConnectProtocol = ProtocolKernel
InMemoryAdapter = InMemoryProtocolAdapter
