"""The protocol module's own durable store: the proposal record.

``contract.md`` v1.7.0, *Separation and interoperability*, says this store
belongs to the protocol rather than to either product: product domain code never
reads or writes it and only the protocol adapter reaches it, and a peer reaches
it with credentials scoped to its own records.  Nothing in
:mod:`atlas_erp.business`, :mod:`atlas_erp.web` or
:mod:`atlas_erp.demo_catalog` knows this module exists, which is what makes the
claim structural rather than a promise in a docstring.

What it buys is stated in the same section and is deliberately narrow: **the
decision survives a restart**, not that a decision and its effect are one
transaction.  A proposal row lives here while the sale it authorised lives in the
owner's database, and no two-phase commit is attempted, so a crash between the
two leaves a decided proposal with no effect -- visible, reconcilable, and
resolvable, which is the whole point of keeping the decision at all.  What
completes it is :func:`~atlas_erp.connect_server.reconcile_accepted_proposals`,
once at startup, because that is the one moment an ``accepted`` row with no
effect has only one possible explanation.

Two adapters implement the one :class:`ProposalStore` interface:

* :class:`InMemoryProposalStore` for unit tests and the loopback smoke.  It is
  the default when no protocol database is configured, exactly as the sale and
  business stores are, so every standalone path keeps working with no database.
* :class:`PostgresProposalStore` for a decision that outlives the process.

Isolation is the database's job, not this module's.  ``CREATE PROPOSALS_SQL``
enables row-level security with one policy that keeps every row's ``peer_id``
equal to ``current_user``, so a peer's login is confined to its own records by
PostgreSQL itself rather than by a ``WHERE peer = ?`` this module might forget.
The policy names no role and holds no password: a peer's ``app_id`` *is* its
database role name, and provisioning that role is not this module's business.

Known ceiling: a payload is stored as JSON, so a tuple inside one would come back
as a list and a value ``json.dumps`` cannot encode is refused by the durable
adapter rather than stored.  The proposal payloads that cross the wire are JSON
already, so this is a statement about the in-process API rather than about a live
path, and a refusal is fail-closed: no row is written and no decision is claimed.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Protocol, cast

if TYPE_CHECKING:
    import psycopg


class ProposalStoreError(ValueError):
    """Raised when a proposal is missing, duplicated, or cannot make the move."""


# The three states a proposal can hold, and the only two a decision can name.
PENDING = "pending"
ACCEPTED = "accepted"
REJECTED = "rejected"
DECIDED_STATES = (ACCEPTED, REJECTED)

# The one table this module owns, and the two statements that make it private to
# its own peer.  Every name is a literal here on purpose: no caller-supplied value
# ever reaches a table, column, or policy name.
#
# The row policy is the whole of the per-peer isolation and it is one statement:
# every role that is not the table's owner is confined to the rows whose
# ``peer_id`` is its own role name, for reads and for writes alike.  It is
# ``current_user`` rather than a session setting because a role cannot change
# which role it is -- a session can change any custom setting it likes, so a GUC
# would be a self-asserted claim rather than a boundary.
#
# The table is not created with ``FORCE ROW LEVEL SECURITY``, so the adapter's own
# role -- the one that owns the table, and the only role that writes proposals --
# is not filtered and sees every peer's rows.  A deployment that connected the
# adapter as some other role would see none of them, which fails closed.
#
# The decided-row check is the database half of "a decided proposal always has a
# reason and a decision time": it makes a record that claims to have been decided
# without saying why impossible to store, whichever adapter wrote it.
#
# ponytail: ``ensure_schema`` is ``CREATE TABLE IF NOT EXISTS``, so a table
# created before a later constraint existed keeps the wider column and never
# gains the constraint.  That is a missing migration runner, the same recorded
# undone item the other two stores carry, and it is not worth a migration system
# to avoid on a table with one writer.
#
# ponytail: this table grows one row per connected sale and nothing removes one.
# Every decision is terminal and a decided row is only read again by
# :func:`~atlas_erp.connect_server.reconcile_accepted_proposals` on a boot where
# its effect is missing, so the growth is pure audit history past that point and
# the ceiling is unbounded retention on a table with one writer.  The upgrade path
# is a retention policy -- an age or a count past which a decided row is archived
# or deleted -- and it is deliberately not written here because how long a peer's
# decision must stay answerable is a policy question, not an engineering one.
CREATE_PROPOSALS_SQL = (
    """
CREATE TABLE IF NOT EXISTS connect_proposals (
    proposal_id text PRIMARY KEY,
    peer_id text NOT NULL,
    capability text NOT NULL,
    payload jsonb NOT NULL,
    state text NOT NULL CHECK (state IN ('pending', 'accepted', 'rejected')),
    reason text,
    created_at timestamptz NOT NULL DEFAULT now(),
    decided_at timestamptz,
    CONSTRAINT connect_proposals_decision CHECK (
        (state = 'pending' AND reason IS NULL AND decided_at IS NULL)
        OR (state <> 'pending' AND reason IS NOT NULL AND decided_at IS NOT NULL)
    )
)
""",
    "ALTER TABLE connect_proposals ENABLE ROW LEVEL SECURITY",
    # CREATE POLICY has no IF NOT EXISTS, so the guard reads the catalog: the
    # DDL stays a literal and can be replayed on every boot.
    """
DO $policy$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'connect_proposals'
          AND policyname = 'connect_proposals_own_rows'
    ) THEN
        CREATE POLICY connect_proposals_own_rows ON connect_proposals
            FOR ALL TO PUBLIC
            USING (peer_id = current_user)
            WITH CHECK (peer_id = current_user);
    END IF;
END
$policy$
""",
)
# The columns every read below names, in the order the statements read them.
_PROPOSAL_COLUMNS = (
    "proposal_id, peer_id, capability, payload, state, reason, created_at, decided_at"
)


def _now() -> str:
    """Return this instant as an ISO-8601 UTC string.

    Both adapters answer timestamps in this shape so a proposal that came out of
    PostgreSQL and one that came out of the process are the same record, and so a
    timestamp read back after a restart is comparable with the one written before
    it.
    """

    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Proposal:
    """A peer's request that the master decide, and what it decided.

    ``peer_id`` is the peer whose intent this is, and it is the column the
    database's row policy is written against: it is what makes a proposal
    somebody's record rather than an anonymous one.  ``created_at`` and
    ``decided_at`` are ISO-8601 UTC strings, ``decided_at`` being ``None`` while
    the master has not answered.
    """

    proposal_id: str
    peer_id: str
    capability: str
    payload: object
    state: str
    reason: str | None
    created_at: str
    decided_at: str | None


class ProposalStore(Protocol):
    """The durable proposal record, as the protocol adapter sees it.

    One row is one proposal from creation to decision, and the two states a
    decision can name are terminal: a second :meth:`transition` is refused rather
    than overwriting the first decision, so the reason a sale was recognised is
    the reason on the record.
    """

    def ensure_schema(self) -> None:
        """Create the owned table and its row policy when they do not exist yet."""

        ...

    def create(
        self, proposal_id: str, peer_id: str, capability: str, payload: object
    ) -> Proposal:
        """Record one ``pending`` proposal for ``peer_id``."""

        ...

    def get(self, proposal_id: str) -> Proposal:
        """Return one stored proposal, or refuse if it names no record."""

        ...

    def transition(self, proposal_id: str, state: str, reason: str) -> Proposal:
        """Move one ``pending`` proposal to ``accepted`` or ``rejected``."""

        ...

    def all(self) -> Mapping[str, Proposal]:
        """Return every stored proposal, keyed by ``proposal_id``."""

        ...

    def close(self) -> None:
        """Release any resource the store holds."""

        ...


class InMemoryProposalStore:
    """A process-local proposal store, used by tests and the loopback smoke.

    It gives the same transitions and refusals as the PostgreSQL adapter within
    one process, and nothing more: the decision is lost on restart.  The payload
    is deep-copied in, so a caller that keeps mutating the dict it submitted does
    not rewrite a record that was already made.
    """

    def __init__(self) -> None:
        self._proposals: dict[str, Proposal] = {}
        # Reentrant because a transition reads the record it is about to replace
        # through ``get``, the same reason ``PostgresBusinessStore`` takes one.
        self._lock = threading.RLock()

    def ensure_schema(self) -> None:
        return None

    def create(
        self, proposal_id: str, peer_id: str, capability: str, payload: object
    ) -> Proposal:
        with self._lock:
            if proposal_id in self._proposals:
                raise ProposalStoreError(f"proposal already exists: {proposal_id}")
            proposal = Proposal(
                proposal_id,
                peer_id,
                capability,
                deepcopy(payload),
                PENDING,
                None,
                _now(),
                None,
            )
            self._proposals[proposal_id] = proposal
            return proposal

    def get(self, proposal_id: str) -> Proposal:
        with self._lock:
            try:
                return self._proposals[proposal_id]
            except KeyError as exc:
                raise ProposalStoreError(f"unknown proposal: {proposal_id}") from exc

    def transition(self, proposal_id: str, state: str, reason: str) -> Proposal:
        if state not in DECIDED_STATES:
            raise ProposalStoreError("a proposal can only be accepted or rejected")
        with self._lock:
            current = self.get(proposal_id)
            if current.state != PENDING:
                raise ProposalStoreError(
                    f"proposal is already {current.state}: {proposal_id}"
                )
            updated = replace(current, state=state, reason=reason, decided_at=_now())
            self._proposals[proposal_id] = updated
            return updated

    def all(self) -> Mapping[str, Proposal]:
        with self._lock:
            return dict(self._proposals)

    def close(self) -> None:
        return None


class PostgresProposalStore:
    """A durable proposal store backed on one PostgreSQL table.

    Every statement is a single round trip and every read is positional, in the
    column order of :data:`_PROPOSAL_COLUMNS`.  A create is an ``ON CONFLICT DO
    NOTHING`` insert, so two processes minting the same id produce one record and
    one refusal, and a transition is a conditional ``UPDATE`` whose ``rowcount``
    decides the winner, so two servers cannot both decide one proposal.
    """

    def __init__(self, dsn: str, *, connect_timeout: int = 5) -> None:
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("database URL must be a non-empty string")
        self._lock = threading.Lock()
        self._connection = _connect(dsn, connect_timeout=connect_timeout)
        self.ensure_schema()

    def ensure_schema(self) -> None:
        """Create the owned table and its row policy when they do not exist yet."""

        with self._lock, self._connection.transaction():
            for statement in CREATE_PROPOSALS_SQL:
                self._connection.execute(statement)

    def create(
        self, proposal_id: str, peer_id: str, capability: str, payload: object
    ) -> Proposal:
        with self._lock, self._connection.transaction():
            row = self._connection.execute(
                "INSERT INTO connect_proposals "
                "(proposal_id, peer_id, capability, payload, state) "
                "VALUES (%s, %s, %s, %s::jsonb, 'pending') "
                "ON CONFLICT (proposal_id) DO NOTHING RETURNING "
                + _PROPOSAL_COLUMNS,
                (proposal_id, peer_id, capability, json.dumps(payload, sort_keys=True)),
            ).fetchone()
        if row is None:
            raise ProposalStoreError(f"proposal already exists: {proposal_id}")
        return _proposal(row)

    def get(self, proposal_id: str) -> Proposal:
        with self._lock, self._connection.transaction():
            row = self._connection.execute(
                "SELECT " + _PROPOSAL_COLUMNS + " FROM connect_proposals "
                "WHERE proposal_id = %s",
                (proposal_id,),
            ).fetchone()
        if row is None:
            raise ProposalStoreError(f"unknown proposal: {proposal_id}")
        return _proposal(row)

    def transition(self, proposal_id: str, state: str, reason: str) -> Proposal:
        if state not in DECIDED_STATES:
            raise ProposalStoreError("a proposal can only be accepted or rejected")
        with self._lock, self._connection.transaction():
            row = self._connection.execute(
                "UPDATE connect_proposals SET state = %s, reason = %s, decided_at = now() "
                "WHERE proposal_id = %s AND state = 'pending' RETURNING "
                + _PROPOSAL_COLUMNS,
                (state, reason, proposal_id),
            ).fetchone()
        if row is None:
            # Either the row is gone or the master already decided it, and the
            # two are not the same answer.  The second read is what tells them
            # apart, exactly as the reclaim in sale_store's reserve does.
            existing = self.get(proposal_id)
            raise ProposalStoreError(
                f"proposal is already {existing.state}: {proposal_id}"
            )
        return _proposal(row)

    def all(self) -> Mapping[str, Proposal]:
        with self._lock, self._connection.transaction():
            rows = self._connection.execute(
                "SELECT " + _PROPOSAL_COLUMNS + " FROM connect_proposals "
                "ORDER BY created_at, proposal_id"
            ).fetchall()
        return {str(row[0]): _proposal(row) for row in rows}

    def close(self) -> None:
        with self._lock:
            self._connection.close()


def _proposal(row: tuple[Any, ...]) -> Proposal:
    """Build one record from a row read in :data:`_PROPOSAL_COLUMNS` order."""

    return Proposal(
        str(row[0]),
        str(row[1]),
        str(row[2]),
        cast("object", row[3]),
        str(row[4]),
        None if row[5] is None else str(row[5]),
        cast("datetime", row[6]).isoformat(),
        None if row[7] is None else cast("datetime", row[7]).isoformat(),
    )


def _connect(dsn: str, *, connect_timeout: int) -> psycopg.Connection[Any]:
    """Open one connection, importing the driver only when it is needed.

    The import is local so the package keeps working when the optional
    PostgreSQL driver is not installed.
    """

    import psycopg

    return psycopg.connect(dsn, connect_timeout=connect_timeout)


__all__ = [
    "ACCEPTED",
    "CREATE_PROPOSALS_SQL",
    "DECIDED_STATES",
    "InMemoryProposalStore",
    "PENDING",
    "PostgresProposalStore",
    "Proposal",
    "ProposalStore",
    "ProposalStoreError",
    "REJECTED",
]
