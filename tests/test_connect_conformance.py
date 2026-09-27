"""Atlas Connect conformance checks driven through the real ERP listener.

``connect/SPEC.md`` puts authority in the protocol module: a capability the
product serves has to be one the module can be told about.  The module's own
tests cannot see whether the shipped path does that, because the shipped path is
``ConnectHandler`` answering a real request, and nothing in it has to call the
module at all for every one of those tests to stay green.

So these cases drive the real ``ConnectServer`` listener over a loopback socket
and hold its answers to the module's.  The module is the oracle: it says what
the caller was allowed to do, and the wire has to agree.

The write-authority half of this measurement is red and lives outside the suite
in ``tests/connect_authority_finding.py``; it is a finding, not a guard yet.
"""

from __future__ import annotations

import json
import unittest
from http.client import HTTPConnection
from typing import Any, cast

from atlas_erp import (
    Business,
    ConnectServer,
    ProtocolError,
    ProtocolKernel,
)
TOKEN = "conformance-token"
PEER_APP_ID = "atlas-ecom"


class ConnectConformanceTests(unittest.TestCase):
    """The shipped Connect path, measured against the protocol module."""

    def setUp(self) -> None:
        self.business = Business()
        item = self.business.register_item("item-1", "SKU-1", "Widget", 1250)
        order = self.business.create_purchase_order(
            "supplier-1", [(item.item_id, 2)], order_id="po-conformance-1"
        )
        self.business.receive_purchase_order(order.order_id, "receipt-conformance-1")
        self.protocol = ProtocolKernel(self.business.registry)
        # As the app that serves them, for the reason given in
        # tests/test_connect_server.py: only the master of a served capability
        # may write it, and the read here is a check that the served capability
        # is a capability the kernel can be told about at all.
        self.server = ConnectServer(
            self.business, self.protocol, {"atlas-erp": TOKEN}, port=0
        )
        self.server.start()
        self.addCleanup(self.server.stop)

    def peer_manifest(self, *capabilities: str) -> dict[str, object]:
        """A peer manifest naming the capabilities it offers and asks to share."""

        return {
            "app_id": PEER_APP_ID,
            "connect_version": self.protocol.connect_version,
            "capabilities": list(capabilities),
            "permissions": [f"{capability}:read" for capability in capabilities],
        }

    def request(
        self,
        path: str,
        *,
        token: object = TOKEN,
        method: str = "GET",
        body: bytes | None = None,
    ) -> tuple[int, dict[str, str], Any]:
        connection = HTTPConnection(*self.server.address, timeout=2)
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

    def test_the_capability_the_server_serves_is_a_capability_the_kernel_knows(
        self,
    ) -> None:
        status, _, payload = self.request("/connect/audit")
        self.assertEqual(status, 200)
        served = cast(str, payload["capability"])

        # The served capability has to be grantable, or a peer could never be
        # given read authority over what the product actually publishes.
        try:
            self.protocol.grant(served, PEER_APP_ID, "reader")
        except ProtocolError as error:
            self.fail(
                f"the served capability {served!r} cannot be granted in the "
                f"protocol module: {error}"
            )
        self.assertIn(
            served,
            cast("list[str]", self.protocol.manifest()["capabilities"]),
            f"{served!r} is served over the wire but is not in the kernel catalog",
        )

    def test_a_peer_can_be_paired_for_read_authority_over_the_served_capability(
        self,
    ) -> None:
        status, _, payload = self.request("/connect/audit")
        self.assertEqual(status, 200)
        served = cast(str, payload["capability"])

        # The advertised manifest is what a peer reads before pairing, so the
        # capability has to be in it or the peer is never told it can ask.
        manifest_status, _, manifest = self.request("/connect/manifest")
        self.assertEqual(manifest_status, 200)
        self.assertIn(served, cast("list[str]", manifest["capabilities"]))

        paired = self.protocol.pair(self.peer_manifest(served), (served,))

        self.assertEqual(paired["peer_id"], PEER_APP_ID)
        self.assertEqual(self.protocol.authorities[served]["atlas-erp"], "master")
        self.assertEqual(self.protocol.authorities[served][PEER_APP_ID], "reader")


if __name__ == "__main__":
    unittest.main()
