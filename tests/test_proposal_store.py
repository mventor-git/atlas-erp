"""Tests for the protocol module's durable proposal store.

The in-memory tests run everywhere.  The PostgreSQL tests run only against the
real temporary instances named by ``ATLAS_ERP_DATABASE_URL`` and
``ATLAS_ERP_CONNECT_DATABASE_URL``, because a fake would not prove that a
decision survives closing and reopening the store -- which is the one thing this
store exists to do, and the one thing the in-memory adapter cannot do at all.
``tests/two_instance_smoke.py`` provisions both and runs this file against them.

The refusal and replay cases are checked *in the owner's database* rather than
in the domain object, because that is where a half-written sale would be: a
durable protocol store must not make a refused command leave a sale behind.

The last two classes are the reconciliation of an accepted decision whose effect
was lost.  ``AcceptedProposalReconciliationTests`` covers the rules with the
in-memory store -- the store outlives the domain there, which is exactly the
shape a restart has -- and ``ReconcilerRestartProofTests`` posts a real sale
into a real database from a real child process, kills that process inside the
window between the accept and the durable write, and starts the real entry point
again to watch the effect appear with nobody asked.
"""

from __future__ import annotations

import io
import json
import os
import re
import socket
import subprocess
import sys
import time
import unittest
from collections.abc import Callable, Mapping
from contextlib import redirect_stderr, redirect_stdout
from http.client import HTTPConnection
from pathlib import Path
from typing import Any, cast
from unittest import mock
from uuid import uuid4

from atlas_erp import (
    ACCEPTED,
    PENDING,
    REJECTED,
    Business,
    BusinessStore,
    ConnectServer,
    InMemoryProposalStore,
    PostgresBusinessStore,
    PostgresProposalStore,
    PostgresSaleCommandStore,
    ProposalStore,
    ProposalStoreError,
    SaleCommandStore,
    snapshot_cursor,
)
from atlas_erp.business_store import CREATE_SCHEMA_SQL as BUSINESS_SCHEMA_SQL
from atlas_erp.connect_server import (
    CONNECT_DATABASE_ENV,
    HOST_ENV,
    PEER_TOKENS_ENV,
    PORT_ENV,
    Reconciliation,
    _fixture,
    _startup_business,
    grant_configured_peers,
    main,
    reconcile_accepted_proposals,
)
from atlas_erp.protocol import ProtocolAdapter, ProposalError, ProtocolKernel
from atlas_erp.proposal_store import CREATE_PROPOSALS_SQL
from atlas_erp.sale_store import CREATE_COMMANDS_SQL

ERP_ROOT = Path(__file__).resolve().parents[1]
# The child that dies inside the window, and the entry point that has to finish
# the work it left behind.
CRASH_CHILD = Path(__file__).resolve().parent / "reconcile_crash_child.py"
# Every child this file starts is killed with a bound, and every wait for one has
# a bound of its own, so a hung child fails the case instead of the run.
STOP_TIMEOUT_SECONDS = 10.0
CLIENT_TIMEOUT_SECONDS = 10.0
WINDOW_TIMEOUT_SECONDS = 60.0
DATABASE_ENV = "ATLAS_ERP_DATABASE_URL"
ECOM = "atlas-ecom"
ECOM_TOKEN = "proposal-store-token"
CAPABILITY = "sales.manual_sales"
REASON = "posted sale-by-restart"
# The fixture's own item, at the price the transport insists on.  Two of these
# are what a reconciler posts, so the sale id is one the fixture has *not* sold:
# a decision whose effect the next domain already holds would prove nothing.
LINES: list[dict[str, object]] = [
    {"item_id": "item-1", "quantity": 1, "unit_price_cents": 1250}
]
PAYLOAD: Mapping[str, object] = {
    "customer_id": "customer-1",
    "sale_id": "sale-1",
    "lines": [dict(line) for line in LINES],
}
# The sale id a lost decision names, chosen so the fixture's own ``sale-1`` does
# not answer for it.
LOST_SALE = "sale-lost"
# The four units the fixture leaves after its own sale: enough for a small
# reconciliation and not enough for one that asks for more than the decision
# could have authorised when it was made.
FIXTURE_STOCK = 4

# Every table the ERP product declares, read from its own DDL rather than listed
# here, so the check cannot fall behind a table added to either store.
PRODUCT_TABLES = frozenset(
    name
    for statement in (*BUSINESS_SCHEMA_SQL, CREATE_COMMANDS_SQL)
    for name in re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", statement)
)
# The one table the protocol store owns, read the same way.
PROTOCOL_TABLES = tuple(
    re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", "\n".join(CREATE_PROPOSALS_SQL))
)


def _database_url() -> str:
    return os.environ.get(DATABASE_ENV, "")


def _connect_database_url() -> str:
    return os.environ.get(CONNECT_DATABASE_ENV, "")


def _own_stock_item(
    business: Business, store: BusinessStore, suffix: str, quantity: int
) -> str:
    """Register one item and receive stock for it, written through to the owner.

    Each case owns its inventory rather than sharing the fixture's, because the
    store these tests run against is a real one that earlier cases and the smoke
    itself have already sold from: a case that borrowed the fixture's stock would
    be refused for exhaustion rather than for the reason it names.
    """

    item_id = f"item-{suffix}"
    item = business.register_item(item_id, f"SKU-{suffix}", "Widget", 1250)
    order = business.create_purchase_order(
        "supplier-1", [(item_id, quantity)], order_id=f"po-{suffix}"
    )
    receipt = business.receive_purchase_order(
        order.order_id, receipt_id=f"receipt-{suffix}"
    )
    with store.transaction():
        store.save_item(item)
        store.save_purchase_order(order)
        store.save_receipt(receipt)
        # The movements this receipt added and nobody else's, read off the domain
        # rather than rebuilt, so the durable ones are the ones it minted.
        store.save_movements(
            [
                movement
                for movement in business.stock_movements.values()
                if suffix in movement.movement_id
            ]
        )
    return item_id


def _owner_database(statement: str, *parameters: object) -> Any:
    """Run one read in the owner's own database and return the first column."""

    import psycopg

    query: Any = statement
    with psycopg.connect(_database_url(), connect_timeout=5) as connection:
        rows = connection.execute(query, parameters).fetchall()
    return rows[0][0] if rows else None


def _owner_sales(sale_id: str) -> int:
    """How many sales the owner's database holds for one id.

    Counted by id rather than in total: the store these tests run against may
    already hold the smoke's own sales, and a total would make the assertion
    about the fixture instead of about this test.
    """

    return int(
        _owner_database("SELECT count(*) FROM sales WHERE sale_id = %s", sale_id)
    )


def _protocol_proposals(sale_id: str) -> list[tuple[Any, ...]]:
    """Every proposal row the protocol store holds for one sale.

    Read on the owner's own account rather than through a store object, so a case
    can see a row a *child* process wrote and this process has not opened yet.
    """

    import psycopg

    with psycopg.connect(_connect_database_url(), connect_timeout=5) as connection:
        return connection.execute(
            "SELECT peer_id, state, reason, created_at IS NOT NULL,"
            " decided_at IS NOT NULL FROM connect_proposals"
            " WHERE payload ->> 'sale_id' = %s ORDER BY created_at",
            (sale_id,),
        ).fetchall()


def _post_sale(
    host: str, port: int, item_id: str, sale_id: str, quantity: int = 1
) -> tuple[int, Any]:
    """Post one connected sale over a real socket and return status and payload."""

    connection = HTTPConnection(host, port, timeout=CLIENT_TIMEOUT_SECONDS)
    body = json.dumps(
        {
            "customer_id": "customer-1",
            "sale_id": sale_id,
            "lines": [
                {"item_id": item_id, "quantity": quantity, "unit_price_cents": 1250}
            ],
        }
    ).encode("utf-8")
    try:
        connection.request(
            "POST",
            "/connect/sales",
            body=body,
            headers={
                "Authorization": f"Bearer {ECOM_TOKEN}",
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def _delete_owner_rows(suffix: str) -> None:
    """Remove one case's rows from the owner's database, and nobody else's."""

    import psycopg

    own = f"%{suffix}%"
    with psycopg.connect(_database_url()) as connection:
        for statement in (
            "DELETE FROM sale_lines WHERE sale_id LIKE %s",
            "DELETE FROM journal_lines WHERE journal_id LIKE %s",
            "DELETE FROM journal_entries WHERE journal_id LIKE %s",
            "DELETE FROM sales WHERE sale_id LIKE %s",
            "DELETE FROM stock_movements WHERE movement_id LIKE %s",
            "DELETE FROM connected_sale_commands WHERE sale_id LIKE %s",
            "DELETE FROM receipt_lines WHERE receipt_id LIKE %s",
            "DELETE FROM receipts WHERE receipt_id LIKE %s",
            "DELETE FROM purchase_order_lines WHERE order_id LIKE %s",
            "DELETE FROM purchase_orders WHERE order_id LIKE %s",
            "DELETE FROM master_items WHERE item_id LIKE %s",
        ):
            connection.execute(statement, (own,))


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


class InMemoryProposalStoreTests(unittest.TestCase):
    """The adapter's default store, and the ceiling it carries.

    These cases pin the semantics both adapters share.  The last one is the
    reason the durable adapter exists and is stated as a test rather than a
    comment: a fresh in-memory store knows nothing about the decision the
    previous one made.
    """

    def setUp(self) -> None:
        self.store = InMemoryProposalStore()
        self.proposal = self.store.create("proposal-1", ECOM, CAPABILITY, PAYLOAD)

    def test_a_created_proposal_is_pending_and_names_its_peer(self) -> None:
        self.assertEqual(self.proposal.state, PENDING)
        self.assertEqual(self.proposal.peer_id, ECOM)
        self.assertEqual(self.proposal.capability, CAPABILITY)
        self.assertEqual(self.proposal.payload, PAYLOAD)
        self.assertIsNone(self.proposal.reason)
        self.assertIsNone(self.proposal.decided_at)
        self.assertTrue(self.proposal.created_at, "a proposal records when it was made")

    def test_a_decision_is_terminal_and_carries_its_reason_and_time(self) -> None:
        decided = self.store.transition("proposal-1", ACCEPTED, REASON)

        self.assertEqual(decided.state, ACCEPTED)
        self.assertEqual(decided.reason, REASON)
        self.assertIsNotNone(decided.decided_at)
        for state in (ACCEPTED, REJECTED):
            with self.subTest(state=state), self.assertRaises(ProposalStoreError):
                self.store.transition("proposal-1", state, "a second decision")

    def test_a_proposal_can_only_be_accepted_or_rejected(self) -> None:
        with self.assertRaises(ProposalStoreError):
            self.store.transition("proposal-1", PENDING, "not a decision")

    def test_a_missing_or_duplicated_proposal_is_refused(self) -> None:
        with self.assertRaises(ProposalStoreError):
            self.store.get("proposal-missing")
        with self.assertRaises(ProposalStoreError):
            self.store.create("proposal-1", ECOM, CAPABILITY, PAYLOAD)

    def test_a_stored_payload_is_not_the_callers_dict(self) -> None:
        payload = cast("dict[str, object]", dict(PAYLOAD))
        self.store.create("proposal-2", ECOM, CAPABILITY, payload)
        stored = cast("Mapping[str, object]", self.store.get("proposal-2").payload)

        payload["sale_id"] = "rewritten-after-the-fact"

        self.assertEqual(stored["sale_id"], "sale-1")

    def test_all_returns_every_record_keyed_by_its_id(self) -> None:
        self.store.create("proposal-2", ECOM, CAPABILITY, PAYLOAD)

        self.assertEqual(
            sorted(self.store.all()), ["proposal-1", "proposal-2"]
        )

    def test_a_fresh_in_memory_store_has_forgotten_the_decision(self) -> None:
        # The ceiling, asserted.  Nothing here is broken: this is what a
        # process-local decision looks like from the next process, and it is the
        # reason ATLAS_ERP_CONNECT_DATABASE_URL exists.
        decided = self.store.transition("proposal-1", ACCEPTED, REASON)

        after_restart = InMemoryProposalStore()

        with self.assertRaises(ProposalStoreError):
            after_restart.get(decided.proposal_id)


class _KernelStoreTests(unittest.TestCase):
    """The module's own path to a proposal, over whatever store it was given."""

    def kernel(self, store: ProposalStore | None = None) -> ProtocolKernel:
        protocol = ProtocolKernel(Business().registry, ProtocolAdapter(store))
        protocol.grant(CAPABILITY, ECOM, "proposer")
        return protocol

    def test_the_kernel_records_the_peer_and_answers_from_the_store(self) -> None:
        store = InMemoryProposalStore()
        protocol = self.kernel(store)

        proposal = protocol.submit_proposal(CAPABILITY, PAYLOAD, peer_id=ECOM)
        decided = protocol.resolve_proposal(
            proposal.proposal_id, ACCEPTED, REASON, peer_id=None
        )

        self.assertEqual(decided.peer_id, ECOM)
        self.assertEqual(decided.reason, REASON)
        # The record is the store's, not the adapter's private state.
        self.assertEqual(store.all()[proposal.proposal_id], decided)
        self.assertEqual(protocol.adapter.proposals, store.all())

    def test_the_kernel_reports_a_missing_proposal_as_a_protocol_error(self) -> None:
        protocol = self.kernel()

        with self.assertRaises(ProposalError):
            protocol.resolve_proposal("proposal-missing", ACCEPTED, REASON)


@unittest.skipUnless(
    _connect_database_url(), f"set {CONNECT_DATABASE_ENV} to run"
)
class PostgresProposalStoreTests(unittest.TestCase):
    """The decision outlives the store that made it."""

    def setUp(self) -> None:
        self.prefix = f"proposal-{uuid4().hex[:12]}"
        self.addCleanup(self._delete_own_rows)

    def open_store(self) -> PostgresProposalStore:
        store = PostgresProposalStore(_connect_database_url())
        self.addCleanup(store.close)
        return store

    def _delete_own_rows(self) -> None:
        import psycopg

        with psycopg.connect(_connect_database_url()) as connection:
            connection.execute(
                "DELETE FROM connect_proposals WHERE proposal_id LIKE %s",
                (f"{self.prefix}%",),
            )

    def test_a_pending_proposal_survives_a_close_and_a_reopen(self) -> None:
        # The point of the module, as one test: a proposal made before a restart
        # is still there afterwards, byte for byte, and can still be decided by
        # the process that comes next.  Swapping the store below for the
        # in-memory one fails this case, which is what makes it a test of
        # durability rather than of the store's in-process behaviour.
        store = self.open_store()
        proposal = store.create(
            f"{self.prefix}-1", ECOM, CAPABILITY, PAYLOAD
        )
        store.close()

        reopened = self.open_store()
        found = reopened.get(proposal.proposal_id)

        self.assertEqual(found, proposal)
        self.assertEqual(found.state, PENDING)
        self.assertIsNone(found.decided_at)

    def test_a_decision_made_after_a_reopen_is_durable_and_terminal(self) -> None:
        store = self.open_store()
        proposal = store.create(f"{self.prefix}-1", ECOM, CAPABILITY, PAYLOAD)
        store.close()

        decided = self.open_store().transition(proposal.proposal_id, ACCEPTED, REASON)

        self.assertEqual(decided.state, ACCEPTED)
        self.assertEqual(decided.reason, REASON)
        self.assertIsNotNone(decided.decided_at)
        # A third store is a second restart, and it reads the decision rather
        # than the pending request that preceded it.
        after_restart = self.open_store().get(proposal.proposal_id)
        self.assertEqual(after_restart, decided)
        with self.assertRaises(ProposalStoreError):
            self.open_store().transition(proposal.proposal_id, REJECTED, "too late")

    def test_a_second_store_cannot_decide_the_same_proposal_twice(self) -> None:
        # Two stores on one database are two servers, and the row count is what
        # decides the winner: the loser is told the proposal is already decided
        # rather than overwriting the first decision.
        first = self.open_store()
        second = self.open_store()
        proposal = first.create(f"{self.prefix}-1", ECOM, CAPABILITY, PAYLOAD)

        decided = first.transition(proposal.proposal_id, ACCEPTED, REASON)

        with self.assertRaises(ProposalStoreError) as refused:
            second.transition(proposal.proposal_id, REJECTED, "a second decision")
        self.assertIn("already accepted", str(refused.exception))
        self.assertEqual(
            second.get(proposal.proposal_id), decided, "the first decision stands"
        )

    def test_a_duplicate_id_is_refused_rather_than_overwriting(self) -> None:
        store = self.open_store()
        original = store.create(f"{self.prefix}-1", ECOM, CAPABILITY, PAYLOAD)

        with self.assertRaises(ProposalStoreError):
            store.create(f"{self.prefix}-1", ECOM, CAPABILITY, {"sale_id": "other"})

        self.assertEqual(store.get(original.proposal_id).payload, PAYLOAD)

    def test_the_protocol_store_holds_no_product_table(self) -> None:
        # Read from the live catalog rather than from configuration: the claim
        # is that this database holds the protocol's record and nothing a
        # product owns.
        import psycopg

        with psycopg.connect(_connect_database_url(), connect_timeout=5) as connection:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                )
            }

        self.assertEqual(sorted(PROTOCOL_TABLES), ["connect_proposals"])
        self.assertEqual(
            sorted(tables & PRODUCT_TABLES),
            [],
            f"the protocol store holds product tables {sorted(tables & PRODUCT_TABLES)}",
        )

    def test_a_row_policy_confines_every_role_to_its_own_peer(self) -> None:
        # The policy is in the catalog rather than in a code path a caller could
        # forget, and it names no role and holds no credential.  PostgreSQL
        # stores the expression it parsed, so the comparison is case-insensitive:
        # the catalog holds ``CURRENT_USER``, not the spelling in the DDL.
        import psycopg

        with psycopg.connect(_connect_database_url(), connect_timeout=5) as connection:
            enabled = connection.execute(
                "SELECT relrowsecurity FROM pg_class WHERE relname = 'connect_proposals'"
            ).fetchone()
            policies = [
                (str(row[0]).lower(), str(row[1]).lower(), str(row[2]).lower(), str(row[3]).lower())
                for row in connection.execute(
                    "SELECT policyname, cmd, qual, with_check FROM pg_policies "
                    "WHERE tablename = 'connect_proposals'"
                )
            ]

        self.assertEqual(enabled, (True,))
        self.assertEqual(len(policies), 1, policies)
        self.assertEqual(policies[0][1], "all")
        self.assertIn("peer_id", policies[0][2])
        self.assertIn("current_user", policies[0][2])
        self.assertIn("current_user", policies[0][3])


@unittest.skipUnless(
    _database_url() and _connect_database_url(),
    f"set {DATABASE_ENV} and {CONNECT_DATABASE_ENV} to run",
)
class DurableProposalOverTheWireTests(unittest.TestCase):
    """The connected sale, driven through a restart with a durable decision.

    Every case here builds its server the way ``main`` does -- from
    :func:`~atlas_erp.connect_server._startup_business` with both durable stores
    -- so what is exercised is the startup path, not a hand-built arrangement.
    """

    def setUp(self) -> None:
        self.suffix = uuid4().hex[:12]
        self.sale_id = f"sale-durable-{self.suffix}"
        self.business_store = PostgresBusinessStore(_database_url())
        self.addCleanup(self.business_store.close)
        self.sale_store = PostgresSaleCommandStore(_database_url())
        self.addCleanup(self.sale_store.close)
        self.proposal_store = self.open_proposal_store()
        self.addCleanup(self._delete_own_rows)
        self.business, self.protocol = _startup_business(
            self.business_store, self.proposal_store
        )
        grant_configured_peers(self.protocol)
        # After the domain exists, and before the server takes a request: the
        # stock this case sells has to be on hand before the sale is refused for
        # want of it.
        self.item_id = _own_stock_item(
            self.business, self.business_store, self.suffix, 6
        )
        self.server = self.serve(
            self.business, self.protocol, self.business_store, self.sale_store
        )

    def open_proposal_store(self) -> ProposalStore:
        store: ProposalStore = PostgresProposalStore(_connect_database_url())
        self.addCleanup(store.close)
        return store

    def _delete_own_rows(self) -> None:
        import psycopg

        with psycopg.connect(_connect_database_url()) as connection:
            connection.execute(
                "DELETE FROM connect_proposals WHERE peer_id = %s", (ECOM,)
            )
        _delete_owner_rows(self.suffix)

    def serve(
        self,
        business: Business,
        protocol: ProtocolKernel,
        business_store: BusinessStore,
        sale_store: SaleCommandStore,
    ) -> ConnectServer:
        server = ConnectServer(
            business,
            protocol,
            {ECOM: ECOM_TOKEN},
            port=0,
            sale_store=sale_store,
            business_store=business_store,
        )
        server.start()
        self.addCleanup(server.close)
        return server

    def post_sale(self, server: ConnectServer, sale_id: str, quantity: int = 1) -> Any:
        return _post_sale(server.host, server.port, self.item_id, sale_id, quantity)

    def owner_database(self, statement: str, *parameters: object) -> Any:
        return _owner_database(statement, *parameters)

    def owner_sales(self, sale_id: str) -> int:
        return _owner_sales(sale_id)

    def proposals_for(self, sale_id: str) -> list[Any]:
        return [
            proposal
            for proposal in self.proposal_store.all().values()
            if isinstance(proposal.payload, Mapping)
            and proposal.payload.get("sale_id") == sale_id
        ]

    def test_the_decision_survives_a_restart_and_a_replay_decides_nothing_twice(
        self,
    ) -> None:
        status, first = self.post_sale(self.server, self.sale_id)
        self.assertEqual(status, 201, first)
        decided = self.proposals_for(self.sale_id)
        self.assertEqual(len(decided), 1)
        self.assertEqual(decided[0].state, ACCEPTED)
        self.assertEqual(decided[0].reason, f"posted {self.sale_id}")
        self.assertIsNotNone(decided[0].decided_at)

        # The restart: the server is closed, and closing it closes the stores it
        # was given, so every store below is a new one -- a second process's
        # worth of state built from the databases alone.
        self.server.close()
        restarted_business_store = PostgresBusinessStore(_database_url())
        self.addCleanup(restarted_business_store.close)
        restarted_sale_store = PostgresSaleCommandStore(_database_url())
        self.addCleanup(restarted_sale_store.close)
        self.proposal_store = self.open_proposal_store()
        business, protocol = _startup_business(
            restarted_business_store, self.proposal_store
        )
        grant_configured_peers(protocol)
        server = self.serve(
            business, protocol, restarted_business_store, restarted_sale_store
        )

        # The decision is still there, unchanged, with the time it was made.
        survived = self.proposals_for(self.sale_id)
        self.assertEqual(survived, decided, "the decision did not survive the restart")
        # And the restored domain still holds the sale, so the restart restored
        # the effect rather than replaying it.
        self.assertIn(self.sale_id, list(business.sales))
        self.assertEqual(self.owner_sales(self.sale_id), 1, "the sale was applied twice")

        replay_status, replay = self.post_sale(server, self.sale_id)

        self.assertEqual(replay_status, 201)
        self.assertEqual(replay, first, "a replay answered from the stored receipt")
        self.assertEqual(
            len(self.proposals_for(self.sale_id)),
            1,
            "the master decided twice for one idempotency key",
        )
        self.assertEqual(
            self.owner_sales(self.sale_id),
            1,
            "the replay applied the sale a second time",
        )
        self.assertEqual(
            self.owner_database(
                "SELECT count(*) FROM journal_entries WHERE sale_id = %s", self.sale_id
            ),
            1,
            "the replay posted a second journal",
        )
        # One journal entry with two lines, not two entries: a journal is counted
        # by entry because that is the record, and its balance is the property
        # worth keeping.
        self.assertIs(
            self.owner_database(
                "SELECT sum(debit_cents) = sum(credit_cents) FROM journal_lines"
                " WHERE journal_id = %s",
                f"journal-{self.sale_id}",
            ),
            True,
            "the replay left the sale's journal unbalanced",
        )

    def test_a_refused_proposal_leaves_no_sale_no_journal_and_no_movement(self) -> None:
        # The "nothing half-written" property, measured in the owner's own
        # database this time: a decision that is now durable must not make a
        # refusal leave anything behind to be found after a restart.
        refused_sale_id = f"{self.sale_id}-too-many"

        # One more than the stock this case owns, so the domain's own invariant
        # is what refuses it.
        status, payload = self.post_sale(self.server, refused_sale_id, quantity=7)

        self.assertEqual((status, payload), (409, {"error": "insufficient stock"}))
        self.assertEqual(
            self.owner_sales(refused_sale_id),
            0,
            "a refused proposal left a sale in the owner's database",
        )
        self.assertEqual(
            self.owner_database(
                "SELECT count(*) FROM journal_entries WHERE sale_id = %s",
                refused_sale_id,
            ),
            0,
            "a refused proposal posted a journal in the owner's database",
        )
        self.assertEqual(
            self.owner_database(
                "SELECT count(*) FROM stock_movements WHERE reference_id = %s",
                refused_sale_id,
            ),
            0,
            "a refused proposal moved stock",
        )
        # And the refusal is on the durable record with the domain's own reason.
        refused = self.proposals_for(refused_sale_id)
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0].state, REJECTED)
        self.assertEqual(refused[0].reason, "insufficient stock")
        # The key was released, so the same sale id works once it is affordable.
        self.assertEqual(
            self.owner_database(
                "SELECT count(*) FROM connected_sale_commands WHERE sale_id = %s",
                refused_sale_id,
            ),
            0,
            "a refused command burned its idempotency key",
        )
        self.assertEqual(self.post_sale(self.server, refused_sale_id)[0], 201)
        # Both decisions are on the durable record: the refusal, with the
        # domain's own reason, and the retry's acceptance.  Two proposals and one
        # sale, because the refusal released the key rather than burning it.
        self.assertEqual(
            sorted(
                (proposal.state, str(proposal.reason))
                for proposal in self.proposals_for(refused_sale_id)
            ),
            [
                ("accepted", f"posted {refused_sale_id}"),
                ("rejected", "insufficient stock"),
            ],
        )
        self.assertEqual(self.owner_sales(refused_sale_id), 1)

    def test_a_replayed_key_returns_the_stored_receipt_and_creates_no_proposal(
        self,
    ) -> None:
        first_status, first = self.post_sale(self.server, self.sale_id)
        second_status, second = self.post_sale(self.server, self.sale_id)

        self.assertEqual((first_status, second_status), (201, 201))
        self.assertEqual(first, second)
        self.assertEqual(len(self.proposals_for(self.sale_id)), 1)
        self.assertEqual(self.owner_sales(self.sale_id), 1, "the replay sold twice")
        self.assertEqual(
            self.owner_database(
                "SELECT count(*) FROM connected_sale_commands"
                " WHERE sale_id = %s AND status = 'completed'",
                self.sale_id,
            ),
            1,
            "the replay completed a second receipt",
        )


class AcceptedProposalReconciliationTests(unittest.TestCase):
    """What one startup does about a decision whose effect never happened.

    The store outlives the domain here, which is the shape a restart has, so
    every case builds the next process's domain from scratch over the same store
    -- which is what ``_startup_business`` does whether a database is configured
    or not.  The PostgreSQL cases further down are the ones that prove the store
    itself is durable; these prove what the startup does with what it finds.
    """

    def setUp(self) -> None:
        self.store = InMemoryProposalStore()

    def payload(self, sale_id: str = LOST_SALE, quantity: int = 1) -> dict[str, object]:
        """One accepted sale for the fixture's item, at a chosen id and size."""

        return {
            "customer_id": "customer-1",
            "sale_id": sale_id,
            "lines": [{**LINES[0], "quantity": quantity}],
        }

    def decide(
        self,
        *,
        proposal_id: str = "proposal-1",
        state: str = ACCEPTED,
        reason: str = REASON,
        capability: str = CAPABILITY,
        payload: Mapping[str, object] | None = None,
    ) -> str:
        """Put one proposal into the store, in the state a decision left it.

        Written through the store's own API rather than by constructing a
        ``Proposal``, so the row under test is one the module could really have
        written and one of the states it could really have been left in.
        """

        self.store.create(
            proposal_id, ECOM, capability, dict(payload or self.payload())
        )
        if state != PENDING:
            self.store.transition(proposal_id, state, reason)
        return proposal_id

    def next_start(self) -> tuple[Business, ProtocolKernel]:
        """The domain and kernel the next process builds, with nothing carried over.

        Nothing in the domain survives between the two, which is the whole point:
        an effect that is not durable has to be reapplied from the decision, and
        that is only visible if the next process starts from nothing.
        """

        return _fixture(self.store)

    def reconcile(self, business: Business, protocol: ProtocolKernel) -> Reconciliation:
        """Run the startup reconciliation, with no durable business store.

        The in-memory case is the standalone one: the domain is the state there
        is, so this is the whole of what applying an effect means here.
        """

        return reconcile_accepted_proposals(business, protocol, None)

    def test_an_accepted_proposal_with_no_effect_is_applied_on_the_next_start(
        self,
    ) -> None:
        proposal_id = self.decide()
        business, protocol = self.next_start()
        self.assertNotIn(LOST_SALE, list(business.sales), "the fixture sold this id")

        report = self.reconcile(business, protocol)

        self.assertEqual(report.applied, [proposal_id])
        self.assertEqual(report.unapplied, [])
        self.assertIn(LOST_SALE, list(business.sales))
        self.assertEqual(business.stock_for("item-1"), FIXTURE_STOCK - 1)
        journal = business.get_journal_for_sale(LOST_SALE)
        self.assertEqual(journal.total_debits_cents, journal.total_credits_cents)

    def test_applying_a_reconciled_effect_a_second_time_is_not_an_error(self) -> None:
        # What the boot after a completed reconciliation does.  The domain raises
        # on a repeated ``sale_id``, and the reconciler has to read that as the
        # state it was trying to produce rather than as a failure -- otherwise
        # every later start of a healthy process fails on the record of a
        # reconciliation that already did its work.
        proposal_id = self.decide()
        business, protocol = self.next_start()
        self.assertEqual(self.reconcile(business, protocol).applied, [proposal_id])

        second = self.reconcile(business, protocol)

        self.assertEqual(second.applied, [], "the second pass applied it again")
        self.assertEqual(second.already_applied, [proposal_id])
        self.assertEqual(second.unapplied, [])
        self.assertEqual(business.stock_for("item-1"), FIXTURE_STOCK - 1)
        self.assertEqual(len(business.sale_movements(LOST_SALE)), 1)

    def test_an_accepted_proposal_whose_effect_already_exists_is_left_alone(
        self,
    ) -> None:
        # The effect arrived -- the original request's own write, or an
        # earlier reconciliation.  The reconciler has to see that and do
        # nothing, which is a different answer from "apply it and find out".
        proposal_id = self.decide()
        business, protocol = self.next_start()
        business.create_manual_sale("customer-1", LINES, sale_id=LOST_SALE)
        cursor_before = snapshot_cursor(business.audit_snapshot().to_dict())

        report = self.reconcile(business, protocol)

        self.assertEqual(report.already_applied, [proposal_id])
        self.assertEqual(report.applied, [])
        self.assertEqual(
            snapshot_cursor(business.audit_snapshot().to_dict()),
            cursor_before,
            "a proposal whose effect was already there changed the domain",
        )
        self.assertEqual(business.stock_for("item-1"), FIXTURE_STOCK - 1)

    def test_a_pending_proposal_is_neither_applied_nor_rejected(self) -> None:
        # A proposal the master has not answered.  Applying it would be the
        # master deciding something nobody decided, and rejecting it would be
        # writing a decision too -- both worse than leaving the record honest.
        self.decide(proposal_id="proposal-pending", state=PENDING)
        business, protocol = self.next_start()

        report = self.reconcile(business, protocol)

        self.assertEqual(report.applied, [])
        self.assertEqual(report.already_applied, [])
        self.assertEqual(report.unapplied, [])
        self.assertNotIn(LOST_SALE, list(business.sales))
        self.assertEqual(
            self.store.get("proposal-pending").state,
            PENDING,
            "the reconciler answered a decision that was never made",
        )

    def test_a_rejected_proposal_is_never_applied_even_though_it_would_validate(
        self,
    ) -> None:
        # The payload is a sale this domain would happily post, and the only
        # thing stopping it is the decision.  A reconciler that read the payload
        # instead of the state would post it.
        self.decide(
            proposal_id="proposal-rejected", state=REJECTED, reason="insufficient stock"
        )
        business, protocol = self.next_start()

        report = self.reconcile(business, protocol)

        self.assertEqual((report.applied, report.unapplied), ([], []))
        self.assertNotIn(LOST_SALE, list(business.sales))
        self.assertEqual(business.stock_for("item-1"), FIXTURE_STOCK)
        self.assertEqual(self.store.get("proposal-rejected").state, REJECTED)

    def test_a_reconciled_effect_carries_the_sale_id_the_proposal_names(self) -> None:
        # The id is what makes a lost effect detectable at all, so the
        # reconciler has to take it from the record rather than mint one.  The
        # audit cursor moving is the peer-visible half of the same fact: a peer
        # holding the cursor from before the crash sees a different snapshot
        # afterwards.
        self.decide()
        business, protocol = self.next_start()
        cursor_before = snapshot_cursor(business.audit_snapshot().to_dict())

        self.reconcile(business, protocol)
        after = cast("dict[str, Any]", business.audit_snapshot().to_dict())

        self.assertNotEqual(snapshot_cursor(after), cursor_before)
        self.assertEqual(
            [
                sale["sale_id"]
                for sale in cast("list[dict[str, Any]]", after["sales"])
                if sale["sale_id"] == LOST_SALE
            ],
            [LOST_SALE],
        )
        self.assertEqual(
            [
                journal["sale_id"]
                for journal in cast("list[dict[str, Any]]", after["journals"])
                if journal["sale_id"] == LOST_SALE
            ],
            [LOST_SALE],
        )
        self.assertEqual(
            [
                movement["quantity_delta"]
                for movement in cast("list[dict[str, Any]]", after["stock_movements"])
                if movement["reference_id"] == LOST_SALE
            ],
            [-1],
        )

    def test_a_proposal_for_a_capability_this_transport_cannot_apply_is_reported(
        self,
    ) -> None:
        # The store belongs to the protocol, so a row may name a capability this
        # product does not serve.  Running the manual-sale shape over it would
        # be inventing an effect, and staying quiet about it would lose the fact
        # that a decision has no home here.
        self.decide(proposal_id="proposal-foreign", capability="audit.snapshot")
        business, protocol = self.next_start()

        report = self.reconcile(business, protocol)

        self.assertEqual(report.applied, [])
        self.assertEqual(
            report.unapplied, [("proposal-foreign", "no applier for audit.snapshot")]
        )
        self.assertEqual(len(business.sales), 1, "the fixture's own sale, and no other")

    def test_an_accepted_proposal_that_can_no_longer_be_applied_is_reported_not_fatal(
        self,
    ) -> None:
        # A decision the master made that the domain can no longer honour: the
        # stock was sold on.  This is a durable decision with no possible effect
        # and a person has to decide what happens to it.  Refusing to start
        # would turn one such decision into an outage that waiting cannot clear,
        # so the answer is to report it and serve anyway.
        self.decide(payload=self.payload(quantity=FIXTURE_STOCK + 5))
        business, protocol = self.next_start()

        report = self.reconcile(business, protocol)

        self.assertEqual(report.applied, [])
        self.assertEqual(report.unapplied, [("proposal-1", "insufficient stock")])
        self.assertNotIn(LOST_SALE, list(business.sales))
        # The decision is untouched: the reconciler reports that the effect could
        # not be produced, and does not second-guess what the master decided.
        self.assertEqual(self.store.get("proposal-1").state, ACCEPTED)

    def test_two_accepted_sales_for_one_item_take_its_stock_in_decided_order(
        self,
    ) -> None:
        # Both decisions are accepted and both effects are lost, so the second
        # has to find the stock the first consumed.  This is why the walk follows
        # the store's own order rather than an arbitrary one.
        first = self.decide(proposal_id="proposal-1")
        second = self.decide(
            proposal_id="proposal-2", payload=self.payload("sale-lost-2", 2)
        )
        business, protocol = self.next_start()
        self.assertEqual(business.stock_for("item-1"), FIXTURE_STOCK)

        report = self.reconcile(business, protocol)

        self.assertEqual(report.applied, [first, second])
        self.assertEqual(business.stock_for("item-1"), 1, "three of four units sold")
        self.assertEqual(sorted(business.sales), ["sale-1", LOST_SALE, "sale-lost-2"])

    def test_a_reconciliation_that_cannot_read_its_own_payload_reports_it(self) -> None:
        # The payload was validated when the master accepted it, so this is a
        # record whose shape this version no longer accepts.  It is reported and
        # nothing is guessed at it.
        self.decide(payload={"customer_id": "customer-1"})
        business, protocol = self.next_start()

        report = self.reconcile(business, protocol)

        self.assertEqual(report.applied, [])
        self.assertEqual(report.unapplied, [("proposal-1", "invalid request")])
        self.assertNotIn(LOST_SALE, list(business.sales))


class StartupBannerTests(unittest.TestCase):
    """The banner an operator reads first, and the no-database case around it."""

    def run_main(self, **environment: str) -> tuple[int, str, str]:
        """Run the real entry point and return its status, stdout, and stderr.

        ``serve_forever`` is stubbed because the banner is printed before it is
        called; everything up to it -- the store configuration, the startup
        reconciliation, and the line itself -- is the real path.
        """

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, environment, clear=True), mock.patch.object(
            ConnectServer, "serve_forever"
        ), redirect_stdout(out), redirect_stderr(err):
            status = main()
        return status, out.getvalue(), err.getvalue()

    def test_the_banner_names_the_proposal_store_mode_with_no_database(self) -> None:
        # The operator needs to know whether a crossing decision survives a
        # restart, and neither of the other two modes says anything about it:
        # both are about this product's own records.
        status, banner, _ = self.run_main(
            **{
                PEER_TOKENS_ENV: json.dumps({ECOM: ECOM_TOKEN}),
                "ATLAS_ERP_CONNECT_PORT": "0",
            }
        )

        self.assertEqual(status, 0)
        self.assertIn(
            "sale receipts: in-memory, business state: in-memory,"
            " crossing decisions: in-memory",
            banner,
        )
        self.assertIn("listening on", banner)
        self.assertNotIn("reconciled", banner, "a boot that changed nothing is quiet")

    def test_no_configured_proposal_store_means_no_reconciliation_at_all(self) -> None:
        # The reconciler is skipped rather than pointed at the empty in-memory
        # store a kernel builds for itself: with no protocol database there is no
        # decision to survive, and every standalone path behaves as it did before
        # this slice.
        with mock.patch(
            "atlas_erp.connect_server.reconcile_accepted_proposals"
        ) as reconcile:
            status, _, _ = self.run_main(
                **{
                    PEER_TOKENS_ENV: json.dumps({ECOM: ECOM_TOKEN}),
                    "ATLAS_ERP_CONNECT_PORT": "0",
                }
            )

        self.assertEqual(status, 0)
        reconcile.assert_not_called()

    def test_a_configured_proposal_store_is_reconciled_and_named_in_the_banner(
        self,
    ) -> None:
        # The other side of the same wiring, and the branch of the banner that
        # tells an operator the decision is durable.  The store is the in-memory
        # one because what is under test is that ``main`` reconciles and reports
        # when it is configured, not what PostgreSQL holds; the durability of the
        # store itself is the PostgreSQL cases' job.
        with mock.patch(
            "atlas_erp.connect_server._proposal_store",
            return_value=InMemoryProposalStore(),
        ), mock.patch(
            "atlas_erp.connect_server.reconcile_accepted_proposals",
            side_effect=reconcile_accepted_proposals,
        ) as reconcile:
            status, banner, _ = self.run_main(
                **{
                    PEER_TOKENS_ENV: json.dumps({ECOM: ECOM_TOKEN}),
                    CONNECT_DATABASE_ENV: "not-read-by-a-patched-store",
                    "ATLAS_ERP_CONNECT_PORT": "0",
                }
            )

        self.assertEqual(status, 0)
        reconcile.assert_called_once()
        self.assertIn("crossing decisions: postgres", banner)
        # Nothing to reconcile, so nothing to report: the line appears only when
        # the startup actually did or could not do something.
        self.assertNotIn("reconciled", banner)

    def test_a_configured_but_unusable_proposal_database_names_only_the_variable(
        self,
    ) -> None:
        # The rule all three stores share, and the one that matters most here:
        # the driver's error can quote the whole connection string, so the
        # message names the variable and nothing else.
        unusable = "postgresql://atlas_connect:hunter2@127.0.0.1:1/atlas_connect"
        status, _, complaint = self.run_main(
            **{
                PEER_TOKENS_ENV: json.dumps({ECOM: ECOM_TOKEN}),
                CONNECT_DATABASE_ENV: unusable,
            }
        )

        self.assertEqual(status, 2)
        self.assertIn(f"{CONNECT_DATABASE_ENV} could not be used", complaint)
        self.assertNotIn("hunter2", complaint)
        self.assertNotIn("postgresql://", complaint)


def _health(port: int) -> bool:
    """Whether the loopback server on ``port`` is answering its health route."""

    connection = HTTPConnection("127.0.0.1", port, timeout=1.0)
    try:
        connection.request("GET", "/health")
        response = connection.getresponse()
        return response.status == 200 and json.loads(response.read())["status"] == "ok"
    except (OSError, ValueError, KeyError):
        return False
    finally:
        connection.close()


@unittest.skipUnless(
    _database_url() and _connect_database_url(),
    f"set {DATABASE_ENV} and {CONNECT_DATABASE_ENV} to run",
)
class ReconcilerRestartProofTests(unittest.TestCase):
    """A real request, a real kill inside the window, and the real entry point.

    Everything above this class could be satisfied by an in-memory arrangement,
    and the thing that cannot be arranged is the sequence: a decision committed
    in one database, a process that dies before the write in the other, and a
    *different* process starting up afterwards with nobody involved.  So the crash
    is a child process killed with a signal inside the window, the recovery is
    ``python -m atlas_erp.connect_server``, and the assertions are read out of
    the two databases those children used.
    """

    def setUp(self) -> None:
        self.suffix = uuid4().hex[:12]
        self.sale_id = f"sale-lost-{self.suffix}"
        self.business_store = PostgresBusinessStore(_database_url())
        self.addCleanup(self.business_store.close)
        self.proposal_store = PostgresProposalStore(_connect_database_url())
        self.addCleanup(self.proposal_store.close)
        self.addCleanup(self._delete_own_rows)
        self._stopped: set[int] = set()
        self.business, self.protocol = _startup_business(
            self.business_store, self.proposal_store
        )
        grant_configured_peers(self.protocol)
        self.item_id = _own_stock_item(
            self.business, self.business_store, self.suffix, 6
        )

    def _delete_own_rows(self) -> None:
        import psycopg

        with psycopg.connect(_connect_database_url()) as connection:
            connection.execute(
                "DELETE FROM connect_proposals WHERE payload ->> 'sale_id' LIKE %s",
                (f"%{self.suffix}%",),
            )
        _delete_owner_rows(self.suffix)

    def child_environment(self, port: int) -> dict[str, str]:
        """The environment one child is started with.

        The connection strings are inherited from this process and are written
        nowhere: they are in the environment because the class is gated on them,
        and the children read the same three variables ``main`` does.
        """

        environment = os.environ.copy()
        environment.update(
            {
                PEER_TOKENS_ENV: json.dumps({ECOM: ECOM_TOKEN}),
                HOST_ENV: "127.0.0.1",
                PORT_ENV: str(port),
            }
        )
        return environment

    def start_child(self, argv: list[str], port: int) -> subprocess.Popen[str]:
        process = subprocess.Popen(
            argv,
            cwd=ERP_ROOT,
            env=self.child_environment(port),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.addCleanup(self.stop, process)
        return process

    def stop(self, process: subprocess.Popen[str]) -> str:
        """Kill one child and return everything it printed, once.

        Idempotent because the same process is stopped both by the case that
        needs what it printed and by the cleanup that has to run when a case fails
        before it gets there.  ``communicate`` is what drains the pipes and reaps
        the child, and it is the only way this file ends a child.
        """

        if process.pid in self._stopped:
            return ""
        self._stopped.add(cast(int, process.pid))
        if process.poll() is None:
            process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=STOP_TIMEOUT_SECONDS)
        return f"{stdout or ''}{stderr or ''}"

    def wait_until(
        self, ready: Callable[[], bool], process: subprocess.Popen[str], complaint: str
    ) -> None:
        """Poll a condition until it holds, or fail with the child's own status.

        The child's status is in the failure because the commonest reason a
        condition never arrives is a child that died on the way to it.
        """

        deadline = time.monotonic() + WINDOW_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if ready():
                return
            if process.poll() is not None:
                raise AssertionError(
                    f"{complaint}: the process exited with {process.returncode}\n"
                    + self.stop(process)
                )
            time.sleep(0.1)
        raise AssertionError(f"{complaint}: timed out after {WINDOW_TIMEOUT_SECONDS}s")

    def start_entry_point(self) -> tuple[subprocess.Popen[str], int]:
        """Start the real entry point and wait until it is serving.

        The wait is on ``/health``, which is not a convenience: the transport
        binds its socket after the reconciliation, so a server that answers is a
        server that has already finished the work.
        """

        port = _free_loopback_port()
        process = self.start_child(
            [sys.executable, "-B", "-m", "atlas_erp.connect_server"], port
        )
        self.wait_until(
            lambda: _health(port), process, "the entry point did not start serving"
        )
        return process, port

    def crash_inside_the_window(self) -> None:
        """Leave behind the state this class is about, from a real process.

        A real request, a real decision committed to the protocol store, and the
        process killed with the decision durable and the sale nowhere but in the
        memory that died.  The child is killed rather than shut down, so nothing
        gets a chance to abort the idempotency key or roll anything back, and the
        three assertions below are the proof that the window really opened rather
        than that the fixture looked like it had.
        """

        crash = self.start_child(
            [sys.executable, "-B", str(CRASH_CHILD), self.item_id, self.sale_id],
            _free_loopback_port(),
        )
        self.wait_until(
            lambda: any(
                row[1] == ACCEPTED for row in _protocol_proposals(self.sale_id)
            ),
            crash,
            "the child never accepted the sale",
        )
        self.stop(crash)

        decided = _protocol_proposals(self.sale_id)
        self.assertEqual(
            [(row[0], row[1]) for row in decided],
            [(ECOM, ACCEPTED)],
            f"the decision did not survive the kill: {decided}",
        )
        self.assertEqual(
            _owner_sales(self.sale_id),
            0,
            "the sale was already durable, so the window never opened",
        )
        self.assertEqual(
            _owner_database(
                "SELECT count(*) FROM connected_sale_commands WHERE sale_id = %s",
                self.sale_id,
            ),
            1,
            "the child never reserved the idempotency key",
        )

    def assert_one_reconciled_sale(self) -> None:
        """The effect is in the owner's database, complete, and counted once."""

        self.assertEqual(_owner_sales(self.sale_id), 1, "the sale was not applied once")
        self.assertEqual(
            _owner_database(
                "SELECT count(*) FROM journal_entries WHERE sale_id = %s", self.sale_id
            ),
            1,
            "the reconciled sale posted no journal, or two",
        )
        self.assertIs(
            _owner_database(
                "SELECT sum(debit_cents) = sum(credit_cents) FROM journal_lines"
                " WHERE journal_id = %s",
                f"journal-{self.sale_id}",
            ),
            True,
            "the reconciled sale's journal is not balanced",
        )
        self.assertEqual(
            _owner_database(
                "SELECT sum(quantity_delta) FROM stock_movements WHERE reference_id = %s",
                self.sale_id,
            ),
            -1,
            "the reconciled sale did not move stock, or moved it twice",
        )

    def test_a_decision_whose_durable_write_was_lost_is_applied_by_the_next_start(
        self,
    ) -> None:
        self.crash_inside_the_window()

        # The next start is the shipped entry point, so the reconciliation, its
        # ordering, and the banner are all under test rather than a call into the
        # module made by a test that already believes the answer.
        server, port = self.start_entry_point()
        self.assert_one_reconciled_sale()
        # The decision is the master's and is not revisited: finishing an effect
        # is not deciding again.
        self.assertEqual(
            [(row[1], row[2]) for row in _protocol_proposals(self.sale_id)],
            [(ACCEPTED, f"posted {self.sale_id}")],
        )
        # The receipt is the half this does not close, and it is left exactly as
        # the kill left it.  A completed receipt here would mean the reconciler
        # invented one, describing a response for a moment that never existed.
        self.assertEqual(
            _owner_database(
                "SELECT count(*) FROM connected_sale_commands"
                " WHERE sale_id = %s AND status = 'completed'",
                self.sale_id,
            ),
            0,
            "the reconciler completed a receipt it had no response to store",
        )

        # A peer that retries the same sale cannot apply it twice whichever half
        # answers it: the reconciler left the effect, and the receipt store either
        # replays its own response or the domain refuses the repeat.  Only the
        # count is asserted, because which of the two answers comes back depends
        # on whether the killed attempt's lease has expired yet.
        replay_status, replay = _post_sale("127.0.0.1", port, self.item_id, self.sale_id)
        self.assertIn(replay_status, (201, 409), f"answered {replay_status}: {replay}")
        self.assert_one_reconciled_sale()

        reported = self.stop(server)
        reconciled_at = reported.find("reconciled crossing decisions:")
        listened_at = reported.find("listening on")
        self.assertGreater(reconciled_at, -1, f"no reconciliation reported: {reported}")
        self.assertIn(self.sale_id, reported[reconciled_at:listened_at])
        # Before the banner, and the banner after it: the work is done, and said
        # so, before anything can reach the domain.
        self.assertGreater(
            listened_at, reconciled_at, f"the report came after the listener: {reported}"
        )
        self.assertIn("crossing decisions: postgres", reported)

    def test_a_start_after_a_completed_reconciliation_applies_nothing_and_still_serves(
        self,
    ) -> None:
        # The boot after a successful one.  The domain refuses the repeated id,
        # the reconciler reads that as the state it wanted rather than a failure,
        # and the process starts normally: a healthy restart is not a startup
        # error, and it says nothing happened.
        self.crash_inside_the_window()
        first, _ = self.start_entry_point()
        self.assert_one_reconciled_sale()
        self.stop(first)

        again, port = self.start_entry_point()
        self.assert_one_reconciled_sale()
        _post_sale("127.0.0.1", port, self.item_id, self.sale_id)
        self.assert_one_reconciled_sale()

        reported = self.stop(again)

        self.assert_one_reconciled_sale()
        self.assertNotIn(
            "reconciled crossing decisions:",
            reported,
            "the second start had nothing to do and said it had something to do",
        )
        self.assertIn("crossing decisions: postgres", reported)
