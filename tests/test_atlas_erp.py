import unittest

from atlas_erp import (
    AuthorityConflictError,
    BASELINE_CLUSTERS,
    IncompatibleVersionError,
    InMemoryProtocolAdapter,
    ProposalError,
    ProtocolKernel,
    Registry,
    handshake,
    validate_authorities,
)


class AtlasErpConformanceTests(unittest.TestCase):
    def registry_with_stock(self) -> Registry:
        registry = Registry()
        registry.register_plugin("inventory", "stock")
        registry.register_module(
            "inventory",
            "stock",
            "availability",
            {"inventory.stock": {"read"}},
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


if __name__ == "__main__":
    unittest.main()
