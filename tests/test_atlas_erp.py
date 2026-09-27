import unittest
from collections.abc import Callable, Mapping
from typing import cast

from atlas_erp import (
    AuthorityConflictError,
    BASELINE_CLUSTERS,
    Business,
    IncompatibleVersionError,
    InMemoryProtocolAdapter,
    PermissionDeniedError,
    ProposalError,
    ProtocolError,
    ProtocolKernel,
    Registry,
    handshake,
    validate_authorities,
)
from atlas_erp.protocol import _ROLE_PERMISSIONS

PEER_APP_ID = "atlas-ecom"
SALES = "sales.manual_sales"
AUDIT = "audit.snapshot"


class AtlasErpConformanceTests(unittest.TestCase):
    def registry_with_stock(self) -> Registry:
        registry = Registry()
        registry.register_plugin("inventory", "stock")
        registry.register_module(
            "inventory",
            "stock",
            "availability",
            # The advertised set of a real capability row, which is where the
            # product derives ``propose`` from ``write``; a proposal is refused
            # for a capability that does not advertise it.
            {"inventory.stock": {"read", "write", "propose"}},
        )
        return registry

    def peer_manifest(self) -> dict[str, object]:
        peer = Registry(app_id="peer")
        peer.register_plugin("inventory", "stock")
        peer.register_module(
            "inventory",
            "stock",
            "availability",
            {"inventory.stock": {"read"}},
        )
        return peer.manifest()

    def test_standalone_registry_boot(self) -> None:
        registry = Registry()
        manifest = registry.manifest()
        self.assertEqual(manifest["app_id"], "atlas-erp")
        self.assertEqual(manifest["connect_version"], "1.0.0")
        self.assertEqual(set(BASELINE_CLUSTERS), set(registry.cluster_names))
        self.assertEqual(manifest["capabilities"], [])

    def test_incompatible_version_rejection(self) -> None:
        with self.assertRaises(IncompatibleVersionError):
            handshake("1.0.0", "2.0.0")
        self.assertTrue(handshake("1.0.0", "1.4.2")["accepted"])

    def test_duplicate_master_rejection(self) -> None:
        with self.assertRaises(AuthorityConflictError):
            validate_authorities(
                {
                    "inventory.stock": {
                        "atlas-erp": "master",
                        "peer": "master",
                    }
                }
            )

    def test_the_permissions_table_is_the_only_statement_of_the_roles(self) -> None:
        # Roles are named once, by the permissions table, and validate_authorities
        # reads that same table rather than a second list of its own.  So a role
        # added there is grantable with nothing else to update, and the two can
        # no longer disagree about which role names exist.
        for role in sorted(_ROLE_PERMISSIONS):
            with self.subTest(role=role):
                assignments = (
                    {"p": role} if role == "master" else {"m": "master", "p": role}
                )
                self.assertEqual(
                    validate_authorities({"inventory.stock": assignments})[
                        "inventory.stock"
                    ],
                    assignments,
                )
        with self.assertRaises(AuthorityConflictError) as unknown:
            validate_authorities({"inventory.stock": {"m": "master", "p": "root"}})
        self.assertIn("unknown authority role", str(unknown.exception))

    def test_pairing_defaults_to_share_only_without_moving_data(self) -> None:
        adapter = InMemoryProtocolAdapter()
        adapter.seed("inventory.stock", {"on_hand": 10})
        protocol = ProtocolKernel(self.registry_with_stock(), adapter)
        before = adapter.snapshot("inventory.stock")

        pairing = protocol.pair(self.peer_manifest())

        self.assertEqual(pairing["mode"], "share-only")
        self.assertFalse(pairing["adopted"])
        self.assertFalse(pairing["moved_data"])
        self.assertEqual(adapter.snapshot("inventory.stock"), before)
        self.assertNotIn("peer", protocol.authorities["inventory.stock"])

    def test_snapshot_and_ordered_deltas_use_an_opaque_cursor(self) -> None:
        adapter = InMemoryProtocolAdapter()
        protocol = ProtocolKernel(self.registry_with_stock(), adapter)
        snapshot = adapter.snapshot("inventory.stock")
        first = protocol.publish("inventory.stock", "event-1", {"on_hand": 9})
        second = protocol.publish("inventory.stock", "event-2", {"on_hand": 8})

        deltas = protocol.deltas("inventory.stock", snapshot.cursor)

        self.assertEqual([delta.event_id for delta in deltas], ["event-1", "event-2"])
        self.assertTrue(all(isinstance(delta.cursor, str) for delta in deltas))
        self.assertNotEqual(snapshot.cursor, first.cursor)
        self.assertNotEqual(first.cursor, second.cursor)

    def test_inbox_deduplicates_repeated_event_ids(self) -> None:
        adapter = InMemoryProtocolAdapter()
        delta = adapter.append_delta("inventory.stock", "event-1", {"on_hand": 9})

        self.assertTrue(adapter.accept_event(delta))
        self.assertFalse(adapter.accept_event(delta))
        self.assertEqual(adapter.inbox, frozenset({"event-1"}))

    def test_proposal_transitions_are_terminal_and_require_reasons(self) -> None:
        protocol = ProtocolKernel(self.registry_with_stock())
        protocol.pair(self.peer_manifest())
        protocol.grant("inventory.stock", "peer", "proposer")

        proposal = protocol.submit_proposal(
            "inventory.stock", {"on_hand": 7}, peer_id="peer"
        )
        self.assertEqual(proposal.state, "pending")
        accepted = protocol.resolve_proposal(
            proposal.proposal_id, "accepted", "reviewed by owner"
        )
        self.assertEqual(accepted.state, "accepted")
        self.assertEqual(accepted.reason, "reviewed by owner")
        with self.assertRaises(ProposalError):
            protocol.resolve_proposal(
                proposal.proposal_id, "rejected", "late decision"
            )

        rejected_proposal = protocol.submit_proposal(
            "inventory.stock", {"on_hand": 6}, peer_id="peer"
        )
        rejected = protocol.resolve_proposal(
            rejected_proposal.proposal_id, "rejected", "outside policy"
        )
        self.assertEqual(rejected.state, "rejected")
        with self.assertRaises(ProposalError):
            protocol.resolve_proposal(
                rejected_proposal.proposal_id, "accepted", "cannot change terminal state"
            )


class KernelAuthorisationTests(unittest.TestCase):
    """The public per-peer question a transport asks the module.

    ``tests/connect_authority_finding.py`` asks the same question over the wire
    in the same authority setup and is red, because the transport does not ask.
    These cases fix the module's half of that statement so the wiring has an
    oracle to meet when it does.
    """

    def setUp(self) -> None:
        self.protocol = ProtocolKernel(Business().registry)
        self.protocol.pair(
            {
                "app_id": PEER_APP_ID,
                "connect_version": self.protocol.connect_version,
                "capabilities": [SALES],
                "permissions": [f"{SALES}:read"],
            },
            (SALES,),
        )

    def test_a_paired_reader_is_refused_the_authoritative_write_the_wire_performed(
        self,
    ) -> None:
        # The exact grant the red finding drives: pairing is share-only, so
        # atlas-ecom holds reader and the module must not let it change sales.
        with self.assertRaises(PermissionDeniedError) as refused:
            self.protocol.authorize(PEER_APP_ID, SALES, "write")
        self.assertIn("reader", str(refused.exception))

        # The same peer as master is the only authority that passes.  A
        # capability has exactly one master and the local app already holds it,
        # so mastery is expressed by being the app that owns the capability.
        master = ProtocolKernel(Business().registry, app_id=PEER_APP_ID)
        master.authorize(PEER_APP_ID, SALES, "write")

    def test_a_read_only_capability_refuses_a_write_even_to_its_master(self) -> None:
        # audit.snapshot is advertised as read-only, so the manifest makes no
        # promise to check a write grant against; no role can exceed it.
        with self.assertRaises(PermissionDeniedError) as refused:
            self.protocol.authorize(self.protocol.app_id, AUDIT, "write")
        self.assertIn("does not advertise", str(refused.exception))
        self.protocol.authorize(self.protocol.app_id, AUDIT, "read")

    def test_an_unknown_capability_is_refused_before_any_authority_is_read(
        self,
    ) -> None:
        with self.assertRaises(ProtocolError) as unknown:
            self.protocol.authorize(PEER_APP_ID, "sales.not_a_capability", "read")
        self.assertIn("unknown capability", str(unknown.exception))

    def test_an_unknown_permission_is_refused_even_for_a_master(self) -> None:
        with self.assertRaises(PermissionDeniedError) as unadvertised:
            self.protocol.authorize(self.protocol.app_id, SALES, "delete")
        self.assertIn("does not advertise", str(unadvertised.exception))

    def test_a_peer_with_no_grant_is_refused_anything(self) -> None:
        with self.assertRaises(PermissionDeniedError) as absent:
            self.protocol.authorize("atlas-hq", SALES, "read")
        self.assertIn("no authority", str(absent.exception))

    def test_only_the_master_may_write_and_every_granted_role_may_read(self) -> None:
        self.protocol.grant(SALES, PEER_APP_ID, "proposer")
        with self.assertRaises(PermissionDeniedError) as proposer:
            self.protocol.authorize(PEER_APP_ID, SALES, "write")
        self.assertIn("proposer", str(proposer.exception))

        self.protocol.authorize(PEER_APP_ID, SALES, "read")
        self.protocol.grant(SALES, PEER_APP_ID, "reader")
        with self.assertRaises(PermissionDeniedError) as reader:
            self.protocol.authorize(PEER_APP_ID, SALES, "write")
        self.assertIn("reader", str(reader.exception))
        self.protocol.authorize(PEER_APP_ID, SALES, "read")
        self.protocol.authorize(self.protocol.app_id, SALES, "write")

    def test_authorising_reports_the_authority_it_checked_and_changes_none_of_it(
        self,
    ) -> None:
        before = self.protocol.authorities

        self.protocol.authorize(PEER_APP_ID, SALES, "read")
        with self.assertRaises(PermissionDeniedError):
            self.protocol.authorize(PEER_APP_ID, AUDIT, "write")

        self.assertEqual(self.protocol.authorities, before)
        self.assertEqual(
            self.protocol.authorities[SALES],
            {"atlas-erp": "master", PEER_APP_ID: "reader"},
        )


class AuthorisationAgreesWithTheGateTests(unittest.TestCase):
    """The public seam and the internal role gate may not answer differently.

    ``authorize`` is the question a transport asks before it acts; ``snapshot``,
    ``deltas``, ``receive_event``, and ``publish`` are the actions, and each runs
    its own ``_require_role`` check.  A grant the seam promises and the gate
    refuses is a denial the transport is not expecting, so the two answers have
    to be the same answer.  These cases sweep every role over every capability
    the registry knows, which is what catches the class rather than one instance.

    ``deltas`` and ``receive_event`` are not enumerated separately: they call the
    same ``_require_role(..., "reader")`` line as ``snapshot``, so ``snapshot`` is
    the representative for the read gate and covers it.
    """

    @staticmethod
    def _allowed(action: Callable[[], object]) -> bool:
        """Whether an authority check let an action through."""
        try:
            action()
        except PermissionDeniedError:
            return False
        return True

    def setUp(self) -> None:
        self.registry = Business().registry
        self.capabilities = cast(
            "list[str]", self.registry.manifest()["capabilities"]
        )
        self.advertised = cast(
            "Mapping[str, list[str]]", self.registry.manifest()["permissions"]
        )

    def kernel_holding(self, capability: str, role: str | None) -> ProtocolKernel:
        """A kernel whose master is the app and whose ``role`` peer is granted."""
        kernel = ProtocolKernel(self.registry)
        if role is not None and role != "master":
            kernel.grant(capability, PEER_APP_ID, role)
        return kernel

    def test_the_read_seam_and_the_read_gate_agree_for_every_role(self) -> None:
        for role in ("master", "reader", "proposer", None):
            for capability in self.capabilities:
                with self.subTest(role=role, capability=capability):
                    kernel = self.kernel_holding(capability, role)
                    if role is None:
                        peer = "atlas-hq"  # never granted on any capability
                    elif role == "master":
                        peer = kernel.app_id
                    else:
                        peer = PEER_APP_ID
                    self.assertEqual(
                        self._allowed(lambda: kernel.authorize(peer, capability, "read")),
                        self._allowed(lambda: kernel.snapshot(capability, peer)),
                    )

    def test_the_write_seam_and_the_write_gate_agree_for_the_master(self) -> None:
        # publish() is the master-gated write and takes no peer_id: it always runs
        # as the app that owns the capability, so the master is the only peer the
        # two answers can be compared for.  Every capability is swept, read-only
        # ones included, because the gate now consults the same advertised
        # permissions the seam does instead of a second statement of its own.
        # audit.snapshot is the case that earned the sweep: a computed projection
        # with no stored record for a write to create.
        for capability in sorted(self.advertised):
            with self.subTest(capability=capability):
                kernel = self.kernel_holding(capability, "master")
                self.assertEqual(
                    self._allowed(
                        lambda: kernel.authorize(kernel.app_id, capability, "write")
                    ),
                    self._allowed(lambda: kernel.publish(capability, "e1", {"n": 1})),
                )

    def test_a_proposer_may_read_what_it_may_not_change(self) -> None:
        # The specific grant the reader gate once refused and the seam allowed.
        # Snapshot, propose, let the master decide: a proposer that cannot read
        # the capability cannot propose coherently about it.  Its refusal is on
        # the seam, because publish() runs only as the app that owns the
        # capability and so cannot express a proposer's write.
        kernel = self.kernel_holding(SALES, "proposer")
        self.assertEqual(kernel.snapshot(SALES, PEER_APP_ID).capability, SALES)
        with self.assertRaises(PermissionDeniedError):
            kernel.authorize(PEER_APP_ID, SALES, "write")
        proposal = kernel.submit_proposal(SALES, {"n": 1}, peer_id=PEER_APP_ID)
        accepted = kernel.resolve_proposal(
            proposal.proposal_id, "accepted", "reviewed by owner"
        )
        self.assertEqual(accepted.state, "accepted")


if __name__ == "__main__":
    unittest.main()
