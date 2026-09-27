"""Per-peer Connect authority, driven through the real ERP listener.

``connect/SPEC.md`` puts authority in the protocol module: a reader consumes
shared data, a proposer asks the master to decide, and only the master applies
an authoritative write.  The shipped path is ``ConnectHandler`` answering a real
request, and nothing in it has to consult the module at all for every one of the
module's own tests to stay green, so every case here drives a real listener over
a loopback socket and holds the wire's answer to the module's.

The credential is scoped to a peer for the same reason from the other end: one
opaque token names no peer, so no grant could ever be consulted about whoever
presented it.  ``CredentialIdentityTests`` is the property that makes a second
peer possible at all, and ``ShippedPeerGrantTests`` pins the provisional grant so
that changing it is a deliberate, visible edit rather than a quiet widening.

This file was ``tests/connect_authority_finding.py``, held outside the discovery
pattern while the write-authority half was a red finding rather than a guard.
"""

from __future__ import annotations

import json
import os
import unittest
from collections.abc import Mapping
from http.client import HTTPConnection
from typing import Any, cast
from unittest import mock

from atlas_erp import (
    Business,
    ConnectServer,
    InMemorySaleCommandStore,
    PermissionDeniedError,
    Proposal,
    ProtocolKernel,
    ProtocolError,
    SaleCommandStore,
)
from atlas_erp.connect_server import (
    AUDIT_CAPABILITY,
    CONNECTED_PEER_GRANTS,
    MANUAL_SALES_CAPABILITY,
    PEER_TOKENS_ENV,
    ROUTE_AUTHORITY,
    grant_configured_peers,
    main,
)
from atlas_erp.connect_server import _ROUTE_METHODS

ECOM = "atlas-ecom"
ECOM_TOKEN = "ecom-token"
OTHER_PEER = "third-party"
OTHER_TOKEN = "third-party-token"
# The serving app id is the master of every capability it serves, and so the
# only principal ``ProtocolKernel.authorize`` can permit to write one.  The peer
# cases below are the point; these two exist so the write route has a caller it
# is allowed to answer.
MASTER = "atlas-erp"
MASTER_TOKEN = "master-token"
UNCONFIGURED_TOKEN = "no-such-peer-token"
# The routes that answer without a grant, on purpose; see ManifestAndHealthTests.
UNGUARDED_ROUTES = ("/health", "/connect/manifest")


def _module_permits(
    protocol: ProtocolKernel, peer_id: str, capability: str, permission: str
) -> bool:
    """Ask the module the question the wire asks, without letting it raise."""

    try:
        protocol.authorize(peer_id, capability, permission)
    except PermissionDeniedError:
        return False
    return True


class _WireTestCase(unittest.TestCase):
    """A domain with stock, and a way to put a question to the served wire."""

    def setUp(self) -> None:
        self.business = Business()
        item = self.business.register_item("item-1", "SKU-1", "Widget", 1250)
        order = self.business.create_purchase_order(
            "supplier-1", [(item.item_id, 2)], order_id="po-authority-1"
        )
        self.business.receive_purchase_order(order.order_id, "receipt-authority-1")
        self.protocol = ProtocolKernel(self.business.registry)

    def serve(
        self,
        peer_tokens: Mapping[str, str],
        *,
        sale_store: SaleCommandStore | None = None,
    ) -> ConnectServer:
        server = ConnectServer(
            self.business, self.protocol, peer_tokens, port=0, sale_store=sale_store
        )
        server.start()
        self.addCleanup(server.close)
        return server

    def request(
        self,
        server: ConnectServer,
        path: str,
        *,
        token: object = ECOM_TOKEN,
        method: str = "GET",
        body: bytes | None = None,
    ) -> tuple[int, dict[str, str], Any]:
        connection = HTTPConnection(*server.address, timeout=2)
        headers: dict[str, str] = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            payload = json.loads(response.read())
            return response.status, dict(response.getheaders()), payload
        finally:
            connection.close()

    def sale_body(self, sale_id: str) -> bytes:
        return json.dumps(
            {
                "customer_id": "customer-authority-1",
                "sale_id": sale_id,
                "lines": [
                    {"item_id": "item-1", "quantity": 1, "unit_price_cents": 1250}
                ],
            }
        ).encode("utf-8")

    def post_sale(
        self, server: ConnectServer, *, token: object = ECOM_TOKEN, sale_id: str
    ) -> tuple[int, dict[str, str], Any]:
        return self.request(
            server,
            "/connect/sales",
            method="POST",
            token=token,
            body=self.sale_body(sale_id),
        )

    def exercise(
        self, server: ConnectServer, route: str, *, token: object
    ) -> int:
        """Drive one guarded route as a peer would and return only the status."""

        if route == "/connect/sales":
            return self.post_sale(server, token=token, sale_id=f"sale-{token!r}")[0]
        return self.request(server, route, token=token)[0]


class ConnectedWriteAuthorityTests(_WireTestCase):
    """``POST /connect/sales`` is an authoritative write, so it is guarded."""

    def test_a_reader_is_refused_the_sale_write_the_module_refused_for_it(self) -> None:
        # Pairing is share-only and grants reader, so this peer holds no
        # authority to change the capability: SPEC.md, "Pairing" and "Authority".
        self.protocol.pair(
            {
                "app_id": ECOM,
                "connect_version": self.protocol.connect_version,
                "capabilities": [MANUAL_SALES_CAPABILITY],
                "permissions": [f"{MANUAL_SALES_CAPABILITY}:read"],
            },
            (MANUAL_SALES_CAPABILITY,),
        )
        self.assertEqual(
            self.protocol.authorities[MANUAL_SALES_CAPABILITY],
            {MASTER: "master", ECOM: "reader"},
        )
        # What the module says about an authoritative write from that peer.
        with self.assertRaises(PermissionDeniedError) as refused:
            self.protocol.submit_proposal(
                MANUAL_SALES_CAPABILITY, {"total_cents": 1250}, peer_id=ECOM
            )
        server = self.serve({ECOM: ECOM_TOKEN})

        status, _, payload = self.post_sale(server, sale_id="sale-refused")

        self.assertEqual(
            status,
            403,
            "the wire performed the authoritative write the module refused: "
            f"{refused.exception}; "
            f"authorities={self.protocol.authorities[MANUAL_SALES_CAPABILITY]}; "
            f"response={payload}",
        )
        # And the refusal came before the domain was touched, so nothing was
        # created, decremented, or posted on the way to answering 403.
        self.assertEqual(list(self.business.sales), [])
        self.assertEqual(self.business.stock, {"item-1": 2})
        self.assertEqual(list(self.business.journals), [])

    def test_the_sale_route_answers_the_capability_s_own_master_and_no_peer(self) -> None:
        # One master per capability (SPEC.md, G2) is the module's own rule, so
        # the only principal it can permit to write a served capability is the
        # app that serves it.  A proposer therefore cannot write one, and the
        # connected sale now reaches the domain as a proposal the master decides
        # rather than as a peer write -- so the peer is answered, and the sale it
        # asked for exists, with the master's decision recorded as the reason it
        # was applied.
        self.protocol.grant(MANUAL_SALES_CAPABILITY, ECOM, "proposer")
        server = self.serve({MASTER: MASTER_TOKEN, ECOM: ECOM_TOKEN})

        peer_status, _, peer_payload = self.post_sale(
            server, token=ECOM_TOKEN, sale_id="sale-refused"
        )
        master_status, _, master_payload = self.post_sale(
            server, token=MASTER_TOKEN, sale_id="sale-accepted"
        )

        # The peer got the sale it asked for, and the master still gets its own
        # -- one path, asked by either, with nothing about the answer changed.
        self.assertEqual(peer_status, 201)
        self.assertEqual(peer_payload["sale_id"], "sale-refused")
        self.assertEqual(master_status, 201)
        self.assertEqual(master_payload["sale_id"], "sale-accepted")
        # Exactly the two sales, and no peer acquired the write to make them.
        self.assertEqual(list(self.business.sales), ["sale-refused", "sale-accepted"])
        with self.assertRaises(PermissionDeniedError):
            self.protocol.authorize(ECOM, MANUAL_SALES_CAPABILITY, "write")
        # And the decision is on the record, naming the sale it applied.
        decided = self.protocol.adapter.proposals
        self.assertEqual(
            sorted(proposal.state for proposal in decided.values()),
            ["accepted", "accepted"],
        )
        self.assertEqual(
            sorted(str(proposal.reason) for proposal in decided.values()),
            ["posted sale-accepted", "posted sale-refused"],
        )

    def test_the_wire_agrees_with_the_module_for_every_peer_and_every_route(self) -> None:
        # The agreement, asserted rather than restated: for each guarded route
        # and each peer, the status the listener answers is the module's own
        # verdict about the same capability and permission.  A transport that
        # decided authority for itself could not satisfy this.
        self.protocol.grant(AUDIT_CAPABILITY, ECOM, "reader")
        self.protocol.grant(MANUAL_SALES_CAPABILITY, ECOM, "proposer")
        server = self.serve({ECOM: ECOM_TOKEN, MASTER: MASTER_TOKEN, OTHER_PEER: OTHER_TOKEN})

        for peer_id, token in (
            (ECOM, ECOM_TOKEN),
            (MASTER, MASTER_TOKEN),
            (OTHER_PEER, OTHER_TOKEN),
        ):
            for route, (capability, permission) in ROUTE_AUTHORITY.items():
                with self.subTest(peer=peer_id, route=route):
                    permitted = _module_permits(
                        self.protocol, peer_id, capability, permission
                    )
                    status = self.exercise(server, route, token=token)
                    self.assertEqual(
                        permitted,
                        status in (200, 201),
                        f"the wire answered {status} where the module said "
                        f"{'permitted' if permitted else 'refused'} for {peer_id} "
                        f"on {capability}:{permission}",
                    )

    def test_a_refused_credential_is_401_and_a_refused_grant_is_403(self) -> None:
        # The two refusals mean different things to a peer: 401 says this token
        # is not one the transport knows, 403 says the token is recognised and
        # retrying with it will not help.  Conflating them would send a peer
        # back to pairing for a decision only the operator can make.
        server = self.serve({ECOM: ECOM_TOKEN, OTHER_PEER: OTHER_TOKEN})

        unknown_status, unknown_headers, unknown = self.request(
            server, "/connect/audit", token=UNCONFIGURED_TOKEN
        )
        ungranted_status, _, ungranted = self.request(
            server, "/connect/audit", token=OTHER_TOKEN
        )

        self.assertEqual(unknown_status, 401)
        self.assertEqual(unknown_headers["WWW-Authenticate"], "Bearer")
        self.assertEqual(unknown, {"error": "unauthorized"})
        self.assertEqual((ungranted_status, ungranted), (403, {"error": "forbidden"}))


class ConnectedProposalFlowTests(_WireTestCase):
    """The peer proposes, the master decides, the master applies.

    ``ROUTE_AUTHORITY`` asks the ``propose`` permission rather than ``write``, so
    these cases carry the property that makes the connected sale correct rather
    than merely reachable: a peer holds exactly enough authority to *ask*, and
    the write happens as the master because the master accepted.
    """

    def proposals(self) -> list[Proposal]:
        """Every proposal on the kernel, in no particular order.

        ``proposal_id`` is minted, so this is deliberately not ordered: a case
        that compares a set of them uses :meth:`decisions` instead.
        """
        return list(self.protocol.adapter.proposals.values())

    def decisions(self) -> list[tuple[str, str]]:
        """Every proposal's state and reason, in a stable order."""
        return sorted(
            (proposal.state, str(proposal.reason))
            for proposal in self.protocol.adapter.proposals.values()
        )

    def test_a_proposer_is_answered_and_the_master_applied_its_sale(self) -> None:
        # The connected checkout, in one case: a peer that holds nothing but
        # ``propose`` gets the sale it asked for, and it gets it from the domain
        # rather than from a shortcut, so every invariant it is handed is one the
        # local console would have produced.
        self.protocol.grant(MANUAL_SALES_CAPABILITY, ECOM, "proposer")
        server = self.serve({ECOM: ECOM_TOKEN})

        status, _, payload = self.post_sale(server, sale_id="sale-proposed")

        self.assertEqual(status, 201)
        self.assertEqual(payload["sale_id"], "sale-proposed")
        self.assertEqual(payload["journal_id"], "journal-sale-proposed")
        self.assertEqual(payload["total_cents"], 1250)
        self.assertTrue(payload["journal_balanced"])
        self.assertEqual(payload["stock"], [{"item_id": "item-1", "quantity": 1}])
        self.assertEqual(list(self.business.sales), ["sale-proposed"])
        # The decision is the master's, recorded, and it is what the write hung
        # on -- not the peer's request being applied on trust.
        self.assertEqual(
            self.decisions(),
            [("accepted", "posted sale-proposed")],
        )
        self.assertEqual(
            self.proposals()[0].payload,
            {
                "customer_id": "customer-authority-1",
                "sale_id": "sale-proposed",
                "lines": [
                    {"item_id": "item-1", "quantity": 1, "unit_price_cents": 1250}
                ],
            },
            "the proposal has to carry the peer's intent, not just its id: it is "
            "the record an operator decision would be made about",
        )

    def test_a_reader_is_refused_the_proposal_itself_and_leaves_no_record(self) -> None:
        # The landed reader case pins the wire's 403.  This pins the other half:
        # the refusal is the module's, it happens in the module, and nothing is
        # left behind for a later reader to mistake for a pending request.
        self.protocol.grant(MANUAL_SALES_CAPABILITY, ECOM, "reader")
        server = self.serve({ECOM: ECOM_TOKEN})

        status, _, payload = self.post_sale(server, sale_id="sale-reader")

        self.assertEqual((status, payload), (403, {"error": "forbidden"}))
        self.assertEqual(self.proposals(), [])
        with self.assertRaises(PermissionDeniedError):
            self.protocol.submit_proposal(
                MANUAL_SALES_CAPABILITY, {"sale_id": "sale-reader"}, peer_id=ECOM
            )
        self.assertEqual(self.proposals(), [])

    def test_a_peer_with_no_grant_at_all_is_refused_and_leaves_no_record(self) -> None:
        # Being a peer implies no authority, and the refusal costs it nothing:
        # a recognised credential with no grant is 403, and no proposal is
        # created for a peer that had no right to ask.
        server = self.serve({ECOM: ECOM_TOKEN})
        self.assertNotIn(ECOM, self.protocol.authorities[MANUAL_SALES_CAPABILITY])

        status, _, payload = self.post_sale(server, sale_id="sale-ungranted")

        self.assertEqual((status, payload), (403, {"error": "forbidden"}))
        self.assertEqual(self.proposals(), [])
        self.assertEqual(list(self.business.sales), [])

    def test_a_capability_that_advertises_no_propose_refuses_the_proposal(self) -> None:
        # ``audit.snapshot`` is a read-only projection: there is no record behind
        # it for the master to change, so a proposal about it is refused to
        # everyone, master included.  The grant is deliberately made, so the
        # refusal cannot be a missing role: it is the advertised set.
        self.protocol.grant(AUDIT_CAPABILITY, ECOM, "proposer")
        advertised = cast(
            "Mapping[str, list[str]]", self.protocol.manifest()["permissions"]
        )

        self.assertEqual(advertised[AUDIT_CAPABILITY], ["read"])
        with self.assertRaises(PermissionDeniedError) as refused:
            self.protocol.submit_proposal(
                AUDIT_CAPABILITY, {"total_cents": 1}, peer_id=ECOM
            )
        self.assertIn("does not advertise the propose permission", str(refused.exception))
        # And the master cannot propose about it either, so "only a master may
        # write" cannot be reached by asking instead.
        with self.assertRaises(PermissionDeniedError):
            self.protocol.submit_proposal(
                AUDIT_CAPABILITY, {"total_cents": 1}, peer_id=MASTER
            )
        self.assertEqual(self.proposals(), [])

    def test_a_rejected_proposal_leaves_no_sale_no_journal_and_no_movement(self) -> None:
        # The domain's own validation is the decision, so a refusal has to be as
        # clean as the in-memory and PostgreSQL refusal cases already are: the
        # "nothing half-written" property, measured on the same three things a
        # half-written sale would show up in.
        self.protocol.grant(MANUAL_SALES_CAPABILITY, ECOM, "proposer")
        server = self.serve({ECOM: ECOM_TOKEN})
        before_movements = self.business.stock_movements
        body = json.loads(self.sale_body("sale-too-many"))
        body["lines"] = [{"item_id": "item-1", "quantity": 5, "unit_price_cents": 1250}]

        status, _, payload = self.request(
            server,
            "/connect/sales",
            method="POST",
            token=ECOM_TOKEN,
            body=json.dumps(body).encode("utf-8"),
        )

        self.assertEqual((status, payload), (409, {"error": "insufficient stock"}))
        self.assertEqual(list(self.business.sales), [])
        self.assertEqual(list(self.business.journals), [])
        self.assertEqual(self.business.stock, {"item-1": 2})
        self.assertEqual(self.business.stock_movements, before_movements)
        # The refusal is on the record as a decision, with the domain's reason,
        # so the pending/rejected state is never a lie.
        self.assertEqual(
            self.decisions(),
            [("rejected", "insufficient stock")],
        )
        # Still nothing, measured again after the answer went out.
        self.assertEqual(list(self.business.sales), [])
        self.assertEqual(self.business.stock, {"item-1": 2})

    def test_a_price_the_master_will_not_accept_is_refused_the_same_way(self) -> None:
        # A second decision, to show the master is really deciding rather than
        # replaying one canned refusal: a wrong price is refused by the transport's
        # item-master check before the domain is touched, and leaves no proposal
        # accepted and no state behind.
        self.protocol.grant(MANUAL_SALES_CAPABILITY, ECOM, "proposer")
        server = self.serve({ECOM: ECOM_TOKEN})
        body = json.loads(self.sale_body("sale-wrong-price"))
        body["lines"] = [{"item_id": "item-1", "quantity": 1, "unit_price_cents": 1}]
        before_movements = self.business.stock_movements

        status, _, payload = self.request(
            server,
            "/connect/sales",
            method="POST",
            token=ECOM_TOKEN,
            body=json.dumps(body).encode("utf-8"),
        )

        self.assertEqual((status, payload), (400, {"error": "price mismatch"}))
        self.assertEqual(list(self.business.sales), [])
        self.assertEqual(self.business.stock, {"item-1": 2})
        self.assertEqual(self.business.stock_movements, before_movements)
        self.assertEqual(
            self.decisions(),
            [("rejected", "price mismatch")],
        )

    def test_a_replayed_key_returns_the_receipt_and_does_not_propose_twice(self) -> None:
        # The receipt is still the command's idempotency boundary and the
        # proposal sits behind it, so a retry across a restart is answered from
        # the stored receipt and the master is not asked to decide the same
        # intent twice.
        self.protocol.grant(MANUAL_SALES_CAPABILITY, ECOM, "proposer")
        store = InMemorySaleCommandStore()
        server = self.serve({ECOM: ECOM_TOKEN}, sale_store=store)

        first_status, first_headers, first = self.post_sale(
            server, sale_id="sale-replayed"
        )
        second_status, second_headers, second = self.post_sale(
            server, sale_id="sale-replayed"
        )

        self.assertEqual((first_status, second_status), (201, 201))
        self.assertEqual(first, second)
        self.assertEqual(
            set(first_headers) - set(second_headers), set(), "the replay's answer shape"
        )
        # One sale, one journal, one movement per line, and one proposal.
        self.assertEqual(list(self.business.sales), ["sale-replayed"])
        self.assertEqual(list(self.business.journals), ["journal-sale-replayed"])
        # Two lines in the domain in total: the receipt of two, and the sale of
        # one.  A second application would make three.
        self.assertEqual(len(self.business.stock_movements), 2)
        self.assertEqual(
            self.decisions(),
            [("accepted", "posted sale-replayed")],
            "the master decided twice for one idempotency key",
        )
        # And the same key with a different body is still a conflict rather than
        # a second decision: the receipt, not the domain, is what refuses it.
        different = json.loads(self.sale_body("sale-replayed"))
        different["customer_id"] = "customer-authority-2"
        conflict_status, _, conflict = self.request(
            server,
            "/connect/sales",
            method="POST",
            token=ECOM_TOKEN,
            body=json.dumps(different).encode("utf-8"),
        )
        self.assertEqual((conflict_status, conflict), (409, {"error": "sale id conflict"}))
        self.assertEqual(len(self.proposals()), 1)

    def test_a_rejected_command_releases_its_key_so_the_same_sale_can_be_retried(self) -> None:
        # The store is the command's idempotency boundary, so a refusal has to
        # release the key or a peer that fixed its request could never use the
        # ``sale_id`` it already owns.  The proposal is refused first and the
        # retry accepted, so both halves of that are on the record.
        self.protocol.grant(MANUAL_SALES_CAPABILITY, ECOM, "proposer")
        store = InMemorySaleCommandStore()
        server = self.serve({ECOM: ECOM_TOKEN}, sale_store=store)
        too_many = json.loads(self.sale_body("sale-retried"))
        too_many["lines"] = [
            {"item_id": "item-1", "quantity": 5, "unit_price_cents": 1250}
        ]

        refused_status, _, refused = self.request(
            server,
            "/connect/sales",
            method="POST",
            token=ECOM_TOKEN,
            body=json.dumps(too_many).encode("utf-8"),
        )
        retry_status, _, retry = self.post_sale(server, sale_id="sale-retried")

        self.assertEqual(
            (refused_status, refused), (409, {"error": "insufficient stock"})
        )
        # The retry is a different request under the same key, which is only
        # possible because the refusal released the reservation.
        self.assertEqual(retry_status, 201)
        self.assertEqual(retry["sale_id"], "sale-retried")
        self.assertEqual(list(self.business.sales), ["sale-retried"])
        self.assertEqual(self.business.stock, {"item-1": 1})
        self.assertEqual(
            self.decisions(),
            [("accepted", "posted sale-retried"), ("rejected", "insufficient stock")],
        )


class RouteAndModuleAgreementTests(_WireTestCase):
    """The transport names a capability and permission; the module decides."""

    def test_every_guarded_route_has_a_row_and_no_unmarked_route_can_appear(self) -> None:
        # The drift case, and the reason the rows are a table rather than an
        # argument at each call site: a new guarded route added without a row
        # would answer every authenticated peer, which is the exact failure this
        # file exists to make impossible.  Only /health and /connect/manifest may
        # serve a peer without a row, and both say why in their own cases.
        self.assertEqual(
            set(_ROUTE_METHODS) - set(UNGUARDED_ROUTES),
            set(ROUTE_AUTHORITY),
            "every guarded route needs a capability and a permission, and only "
            "the documented unguarded routes may be missing one",
        )

    def test_every_row_names_a_capability_and_permission_the_module_advertises(self) -> None:
        # A row asking about a permission the capability does not advertise is
        # refused for every peer, forever, including the master, so it would
        # look like a broken credential rather than a broken row.
        advertised = cast(
            "Mapping[str, list[str]]", self.protocol.manifest()["permissions"]
        )
        for route, (capability, permission) in ROUTE_AUTHORITY.items():
            with self.subTest(route=route):
                self.assertIn(capability, advertised)
                self.assertIn(
                    permission,
                    advertised[capability],
                    f"{route} asks about {capability}:{permission}, which the "
                    f"manifest does not advertise",
                )
        self.assertEqual(
            ROUTE_AUTHORITY["/connect/sales"], (MANUAL_SALES_CAPABILITY, "propose")
        )
        self.assertEqual(ROUTE_AUTHORITY["/connect/audit"], (AUDIT_CAPABILITY, "read"))


class ManifestAndHealthTests(_WireTestCase):
    """The two routes that answer without a grant, on purpose."""

    def test_health_is_public_and_reports_protocol_identity(self) -> None:
        server = self.serve({ECOM: ECOM_TOKEN})

        status, _, payload = self.request(server, "/health", token=None)

        self.assertEqual(status, 200)
        self.assertEqual(
            payload, {"app_id": MASTER, "connect_version": "1.0.0", "status": "ok"}
        )

    def test_a_peer_with_no_grant_may_still_read_the_manifest(self) -> None:
        # Deliberate, and the reason is ordering rather than convenience.  SPEC
        # "Manifest exchange" has the manifest read as part of the handshake and
        # "Pairing" grants nothing on its own, so a manifest behind a grant
        # would be a manifest no peer could ever be told about: refusing it
        # would make pairing impossible rather than safer.  It advertises what
        # ERP offers and carries no ERP-owned data, so an identified peer
        # reading it learns nothing a grant is meant to protect.
        self.assertNotIn(ECOM, self.protocol.authorities[MANUAL_SALES_CAPABILITY])
        server = self.serve({ECOM: ECOM_TOKEN})

        status, _, payload = self.request(server, "/connect/manifest")

        self.assertEqual(status, 200)
        self.assertEqual(payload["app_id"], MASTER)
        self.assertIn(
            MANUAL_SALES_CAPABILITY, cast("list[str]", payload["capabilities"])
        )

    def test_the_manifest_still_needs_a_credential_the_transport_knows(self) -> None:
        # Grant-free is not credential-free: the manifest is still a connected
        # peer's view of the product, so an unauthenticated caller gets nothing
        # and a recognised-but-ungranted one gets all of it.
        server = self.serve({ECOM: ECOM_TOKEN})

        for token in (None, UNCONFIGURED_TOKEN, f"Basic {ECOM_TOKEN}", f"Bearer {ECOM_TOKEN} x"):
            with self.subTest(token=token):
                status, headers, payload = self.request(
                    server, "/connect/manifest", token=token
                )
                self.assertEqual(status, 401)
                self.assertEqual(headers["WWW-Authenticate"], "Bearer")
                self.assertEqual(payload, {"error": "unauthorized"})


class CredentialIdentityTests(_WireTestCase):
    """The credential names the peer, which is what a second peer needs."""

    def test_two_peers_two_tokens_and_each_is_authorised_as_itself(self) -> None:
        # The property the per-peer credential exists for.  With one shared
        # token there is nothing to authorise against, so a second peer is
        # impossible and revoking one peer revokes both.
        #
        # The sale route is the one that can tell the two peers apart, and only
        # because it is no longer 403 for everybody: ecom may propose a sale and
        # the other peer holds no grant on the capability at all, so the same
        # request is 201 for one credential and 403 for the other.  Before the
        # proposal flow both were 403, which proved nothing about isolation.
        self.protocol.grant(AUDIT_CAPABILITY, ECOM, "reader")
        self.protocol.grant(AUDIT_CAPABILITY, OTHER_PEER, "reader")
        self.protocol.grant(MANUAL_SALES_CAPABILITY, ECOM, "proposer")
        server = self.serve({ECOM: ECOM_TOKEN, OTHER_PEER: OTHER_TOKEN})

        ecom_read = self.request(server, "/connect/audit", token=ECOM_TOKEN)[0]
        other_read = self.request(server, "/connect/audit", token=OTHER_TOKEN)[0]
        ecom_sale = self.post_sale(server, token=ECOM_TOKEN, sale_id="sale-ecom")[0]
        other_sale = self.post_sale(server, token=OTHER_TOKEN, sale_id="sale-other")[0]
        unknown = self.request(server, "/connect/audit", token=UNCONFIGURED_TOKEN)[0]

        # Each token gets its own peer's authority and none of the other's: the
        # two readers read, the granted proposer asks and is answered, the
        # ungranted peer is refused the same request, and an unconfigured token
        # is nobody.
        self.assertEqual(
            (ecom_read, other_read, ecom_sale, other_sale, unknown),
            (200, 200, 201, 403, 401),
        )
        self.assertEqual(self.post_sale(
            server, token=OTHER_TOKEN, sale_id="sale-other"
        )[2], {"error": "forbidden"})
        # And the refusal cost the other peer nothing: exactly the granted
        # peer's sale exists.
        self.assertEqual(list(self.business.sales), ["sale-ecom"])
        self.assertEqual(self.business.stock, {"item-1": 1})

    def test_a_claiming_peer_is_still_the_peer_its_credential_names(self) -> None:
        # The peer is the token, not anything the request says about itself, so
        # a caller that claims the master while holding a peer's credential is
        # still that peer.  The claim no longer buys a 403, because a proposer
        # may ask; so the pin is on who the module was told asked, and on the
        # authority the claim did not buy.
        self.protocol.grant(MANUAL_SALES_CAPABILITY, ECOM, "proposer")
        server = self.serve({ECOM: ECOM_TOKEN})
        body = json.loads(self.sale_body("sale-claimed-1"))
        body["app_id"] = MASTER
        with mock.patch.object(
            self.protocol,
            "submit_proposal",
            wraps=self.protocol.submit_proposal,
        ) as proposed:
            connection = HTTPConnection(*server.address, timeout=2)
            try:
                connection.request(
                    "POST",
                    "/connect/sales",
                    body=json.dumps(body).encode("utf-8"),
                    headers={
                        "Authorization": f"Bearer {ECOM_TOKEN}",
                        "Content-Type": "application/json",
                        "X-Atlas-App-Id": MASTER,
                    },
                )
                response = connection.getresponse()
                status = response.status
                response.read()
            finally:
                connection.close()

        self.assertEqual(status, 201)
        # The proposal was submitted by the credential's peer, and the claimed
        # app_id was never read from the request.
        self.assertEqual(
            [call.kwargs["peer_id"] for call in proposed.call_args_list], [ECOM]
        )
        # Which is why the claim changed nothing about what this peer may do.
        self.assertEqual(
            self.protocol.authorities[MANUAL_SALES_CAPABILITY][ECOM], "proposer"
        )
        with self.assertRaises(PermissionDeniedError):
            self.protocol.authorize(ECOM, MANUAL_SALES_CAPABILITY, "write")
        # And the master's own authority is exactly one grant, unchanged.
        self.assertEqual(
            [
                peer
                for peer, role in self.protocol.authorities[
                    MANUAL_SALES_CAPABILITY
                ].items()
                if role == "master"
            ],
            [MASTER],
        )

    def test_one_peer_and_ten_take_the_same_code_path(self) -> None:
        # There is no single-peer mode, because a single-peer mode is the
        # identity-less mode the per-peer credential removed.  One peer and ten
        # are both a mapping, and neither gets a shortcut.
        self.protocol.grant(AUDIT_CAPABILITY, ECOM, "reader")
        one = self.serve({ECOM: ECOM_TOKEN})
        ten = self.serve({ECOM: ECOM_TOKEN, **{f"peer-{i}": f"t-{i}" for i in range(9)}})

        for server in (one, ten):
            with self.subTest(peers=len(server.peer_tokens)):
                self.assertEqual(
                    self.request(server, "/connect/audit", token=ECOM_TOKEN)[0], 200
                )
        # The same token is a different peer on each server, and neither answer
        # is a shortcut: it is nobody on the one-peer transport and an
        # ungranted peer on the ten-peer one.
        self.assertEqual(self.request(one, "/connect/audit", token="t-0")[0], 401)
        self.assertEqual(self.request(ten, "/connect/audit", token="t-0")[0], 403)


class PeerCredentialConfigurationTests(_WireTestCase):
    """A configuration that cannot name a peer is refused, not served."""

    def test_a_credential_configuration_that_cannot_name_a_peer_is_refused(self) -> None:
        cases: tuple[tuple[str, object], ...] = (
            ("no-peers", {}),
            ("blank-token", {ECOM: ""}),
            ("padded-token", {ECOM: "   "}),
            ("blank-peer-id", {"  ": ECOM_TOKEN}),
            ("one-token-two-peers", {ECOM: "shared", OTHER_PEER: "shared"}),
            ("not-a-mapping", [(ECOM, ECOM_TOKEN)]),
            ("non-string-token", {ECOM: 7}),
        )
        for name, peer_tokens in cases:
            with self.subTest(case=name):
                with self.assertRaises(ValueError):
                    ConnectServer(
                        self.business, self.protocol, cast("dict[str, str]", peer_tokens)
                    )

    def test_the_entry_point_reads_one_peer_token_variable_and_no_other(self) -> None:
        # The retired single-token variable is not an alias for "the one peer":
        # honouring it would keep the identity-less configuration alive, which
        # is the footgun the rename exists to remove.
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(main(), 2)
        with mock.patch.dict(
            os.environ, {"ATLAS_ERP_CONNECT_TOKEN": ECOM_TOKEN}, clear=True
        ):
            self.assertEqual(main(), 2)
        for unusable in ("not-json", "[]", '{"atlas-ecom": ""}', '{"atlas-ecom": 7}'):
            with self.subTest(value=unusable):
                with mock.patch.dict(
                    os.environ, {PEER_TOKENS_ENV: unusable}, clear=True
                ):
                    self.assertEqual(main(), 2)


class ShippedPeerGrantTests(unittest.TestCase):
    """The grants the entry point ships, pinned so a change is deliberate."""

    def setUp(self) -> None:
        self.protocol = ProtocolKernel(Business().registry)
        grant_configured_peers(self.protocol)

    def granted_rows(self, peer_id: str) -> dict[str, str]:
        """Every (capability, role) this product gives one peer but itself."""

        return {
            capability: assignments[peer_id]
            for capability, assignments in self.protocol.authorities.items()
            if peer_id in assignments
        }

    def test_the_shipped_grants_are_exactly_two_rows(self) -> None:
        peers = set(CONNECTED_PEER_GRANTS)
        self.assertEqual(
            {peer: self.granted_rows(peer) for peer in peers},
            {
                "atlas-ecom": {
                    MANUAL_SALES_CAPABILITY: "proposer",
                    AUDIT_CAPABILITY: "reader",
                }
            },
            "the shipped grant for atlas-ecom changed.  Both product contracts "
            "say Ecom submits intent and ERP decides recognition, quantity, and "
            "value, which is what proposer means, so this is the role the "
            "contract names; the connected proposal flow is what makes it "
            "effective rather than a record of intent.",
        )

    def test_no_shipped_grant_promotes_a_peer_to_master(self) -> None:
        # G2 is one master per capability, and the serving app already holds
        # every one it serves, so a peer grant of master is not a grant this
        # product can make at all: ``ProtocolKernel.grant`` refuses the second
        # master.  Asserted here so the provisional write row is read as a
        # proposer rather than as a master this transport is waiting for.
        for capability, assignments in self.protocol.authorities.items():
            with self.subTest(capability=capability):
                self.assertEqual(
                    [peer for peer, role in assignments.items() if role == "master"],
                    [self.protocol.app_id],
                )
        with self.assertRaises(ProtocolError) as refused:
            self.protocol.grant(MANUAL_SALES_CAPABILITY, "atlas-ecom", "master")
        self.assertIn("master", str(refused.exception))


if __name__ == "__main__":
    unittest.main()
