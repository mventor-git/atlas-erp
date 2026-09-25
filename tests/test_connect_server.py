import io
import json
import unittest
from collections.abc import Iterable, Mapping
from http.client import HTTPConnection, HTTPMessage, parse_headers
from typing import Any, cast
from unittest import mock

from atlas_erp import (
    RESERVED,
    Business,
    ConnectServer,
    InMemorySaleCommandStore,
    ProtocolKernel,
    SaleCommandStore,
    sale_command_hash,
    snapshot_cursor,
    wire_manifest,
)
from atlas_erp.connect_server import (
    DRAIN_LIMIT_BYTES,
    MAX_BODY_BYTES,
    ConnectHandler,
    _RequestError,
)


_MISSING = object()
_RAW = object()
# The rejected-request race is rare, so the wire test repeats it; the drain
# itself is pinned directly by RejectedRequestBodyTests.
REPEATED_REJECTIONS = 25


class ConnectHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.business = Business()
        item = self.business.register_item("item-1", "SKU-1", "Widget", 1250)
        order = self.business.create_purchase_order(
            "supplier-1", [(item.item_id, 2)], order_id="po-http-1"
        )
        self.business.receive_purchase_order(order.order_id, "receipt-http-1")
        self.business.create_manual_sale(
            "customer-1",
            [{"item_id": item.item_id, "quantity": 1, "unit_price_cents": 1250}],
            sale_id="sale-http-1",
        )
        self.protocol = ProtocolKernel(self.business.registry)
        self.server = ConnectServer(
            self.business,
            self.protocol,
            "test-token",
            port=0,
        )
        self.server.start()
        self.addCleanup(self.server.stop)

    def request(
        self,
        path: str,
        *,
        token: object = _MISSING,
        method: str = "GET",
        body: bytes | None = None,
        content_type: str | None = None,
        server: ConnectServer | None = None,
    ) -> tuple[int, dict[str, str], Any]:
        connection = HTTPConnection(
            *(server if server is not None else self.server).address, timeout=2
        )
        headers: dict[str, str] = {}
        if token is not _MISSING:
            headers["Authorization"] = f"Bearer {token}"
        if content_type is not None:
            headers["Content-Type"] = content_type
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            payload = json.loads(response.read())
            return response.status, dict(response.getheaders()), payload
        finally:
            connection.close()

    def post_sale(
        self,
        payload: object,
        *,
        token: object = "test-token",
        raw: bytes | None = None,
        content_type: str | None = "application/json",
        server: ConnectServer | None = None,
    ) -> tuple[int, dict[str, str], Any]:
        body = raw if raw is not None else json.dumps(payload).encode("utf-8")
        return self.request(
            "/connect/sales",
            token=token,
            method="POST",
            body=body,
            content_type=content_type,
            server=server,
        )

    def store_server(self, store: SaleCommandStore) -> ConnectServer:
        """Start a second server over the same domain, sharing one sale store."""

        server = ConnectServer(
            self.business,
            self.protocol,
            "test-token",
            port=0,
            sale_store=store,
        )
        server.start()
        self.addCleanup(server.close)
        return server

    def test_health_is_local_and_reports_protocol_identity(self) -> None:
        status, headers, payload = self.request("/health")

        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("application/json"))
        self.assertEqual(
            payload,
            {
                "app_id": "atlas-erp",
                "status": "ok",
                "connect_version": "1.0.0",
            },
        )

    def test_protected_routes_reject_missing_and_invalid_bearer_tokens(self) -> None:
        for path in ("/connect/manifest", "/connect/audit"):
            for token in (_MISSING, "wrong-token"):
                with self.subTest(path=path, token=token):
                    status, headers, payload = self.request(path, token=token)
                    self.assertEqual(status, 401)
                    self.assertEqual(headers["WWW-Authenticate"], "Bearer")
                    self.assertEqual(payload, {"error": "unauthorized"})

    def test_manifest_returns_normalized_wire_shape_without_mutating_local_manifest(
        self,
    ) -> None:
        local_manifest = self.protocol.manifest()
        local_permissions = cast(
            Mapping[str, Iterable[str]], local_manifest["permissions"]
        )
        expected_permissions = sorted(
            {
                f"{capability}:{permission}"
                for capability, permissions in local_permissions.items()
                for permission in permissions
            }
        )

        status, _, payload = self.request(
            "/connect/manifest", token="test-token"
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload, wire_manifest(local_manifest))
        self.assertEqual(
            payload,
            {
                "app_id": "atlas-erp",
                "connect_version": "1.0.0",
                "capabilities": list(
                    cast(Iterable[str], local_manifest["capabilities"])
                ),
                "permissions": expected_permissions,
            },
        )
        self.assertIsInstance(payload["permissions"], list)
        self.assertEqual(payload["permissions"], sorted(set(expected_permissions)))
        self.assertIsInstance(local_manifest["permissions"], Mapping)
        self.assertEqual(self.protocol.manifest(), local_manifest)

    def test_audit_returns_snapshot_data_and_deterministic_cursor(self) -> None:
        status, _, first = self.request("/connect/audit", token="test-token")
        second_status, _, second = self.request("/connect/audit", token="test-token")

        self.assertEqual(status, 200)
        self.assertEqual(second_status, 200)
        self.assertEqual(
            set(first),
            {
                "app_id",
                "type",
                "capability",
                "connect_version",
                "cursor",
                "data",
            },
        )
        self.assertEqual(first["app_id"], "atlas-erp")
        self.assertEqual(first["type"], "snapshot")
        self.assertEqual(first["capability"], "audit.snapshot")
        self.assertEqual(first["connect_version"], "1.0.0")
        self.assertEqual(first["data"], self.business.audit_snapshot().to_dict())
        self.assertEqual(
            first["data"]["items"],
            [
                {
                    "item_id": "item-1",
                    "sku": "SKU-1",
                    "name": "Widget",
                    "price_cents": 1250,
                }
            ],
        )
        self.assertEqual(first["cursor"], snapshot_cursor(first["data"]))
        self.assertEqual(first["cursor"], second["cursor"])

    def test_unknown_routes_are_json_404s(self) -> None:
        status, headers, payload = self.request("/not-a-route")

        self.assertEqual(status, 404)
        self.assertTrue(headers["Content-Type"].startswith("application/json"))
        self.assertEqual(payload, {"error": "not found"})

    def test_known_routes_reject_non_get_methods(self) -> None:
        for path, method in (
            ("/health", "POST"),
            ("/connect/manifest", "POST"),
            ("/connect/audit", "POST"),
            ("/connect/sales", "GET"),
            ("/connect/sales", "PUT"),
        ):
            with self.subTest(path=path, method=method):
                status, headers, payload = self.request(path, method=method)

                self.assertEqual(status, 405)
                self.assertEqual(headers["Allow"], "POST" if path == "/connect/sales" else "GET")
                self.assertEqual(payload, {"error": "method not allowed"})

    def test_non_loopback_bind_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "loopback"):
            ConnectServer(
                self.business,
                self.protocol,
                "test-token",
                host="0.0.0.0",
                port=0,
            )

    def sale_body(self, **overrides: object) -> dict[str, object]:
        body: dict[str, object] = {
            "customer_id": "customer-http-2",
            "sale_id": "sale-http-2",
            "lines": [
                {"item_id": "item-1", "quantity": 1, "unit_price_cents": 1250}
            ],
        }
        body.update(overrides)
        return body

    def test_connected_sale_posts_a_manual_sale_and_reports_the_result(self) -> None:
        status, _, payload = self.post_sale(self.sale_body())

        self.assertEqual(status, 201)
        self.assertEqual(
            payload,
            {
                "app_id": "atlas-erp",
                "capability": "sales.manual_sales",
                "sale_id": "sale-http-2",
                "customer_id": "customer-http-2",
                "journal_id": "journal-sale-http-2",
                "total_cents": 1250,
                "lines": [
                    {
                        "item_id": "item-1",
                        "quantity": 1,
                        "unit_price_cents": 1250,
                        "total_cents": 1250,
                    }
                ],
                "stock": [{"item_id": "item-1", "quantity": 0}],
                "journal_balanced": True,
            },
        )
        sale = self.business.get_sale("sale-http-2")
        journal = self.business.get_journal_for_sale("sale-http-2")
        self.assertEqual(sale.total_cents, payload["total_cents"])
        self.assertEqual(journal.total_debits_cents, journal.total_credits_cents)
        self.assertEqual(self.business.stock, {"item-1": 0})

        _, _, audit = self.request("/connect/audit", token="test-token")
        audit_sales = audit["data"]["sales"]
        self.assertEqual([entry["sale_id"] for entry in audit_sales], ["sale-http-1", "sale-http-2"])

    def test_connected_sale_requires_the_bearer_token(self) -> None:
        for token in (_MISSING, "wrong-token"):
            with self.subTest(token=token):
                status, headers, payload = self.post_sale(
                    self.sale_body(sale_id=f"sale-unauthorized-{token!r}"),
                    token=token,
                )

                self.assertEqual(status, 401)
                self.assertEqual(headers["WWW-Authenticate"], "Bearer")
                self.assertEqual(payload, {"error": "unauthorized"})

        self.assertEqual(list(self.business.sales), ["sale-http-1"])
        self.assertEqual(self.business.stock, {"item-1": 1})

    def test_connected_sale_rejects_a_price_that_is_not_the_item_master_price(self) -> None:
        status, _, payload = self.post_sale(
            self.sale_body(
                sale_id="sale-price-mismatch",
                lines=[{"item_id": "item-1", "quantity": 1, "unit_price_cents": 999}],
            )
        )

        self.assertEqual(status, 400)
        self.assertEqual(payload, {"error": "price mismatch"})
        self.assertNotIn("sale-price-mismatch", self.business.sales)
        self.assertEqual(self.business.stock, {"item-1": 1})
        self.assertEqual(list(self.business.journals), ["journal-sale-http-1"])

    def test_duplicate_connected_sale_is_a_conflict_and_is_never_reapplied(self) -> None:
        first_status, _, _ = self.post_sale(self.sale_body())
        second_status, _, payload = self.post_sale(self.sale_body())

        self.assertEqual(first_status, 201)
        self.assertEqual(second_status, 409)
        self.assertEqual(payload, {"error": "duplicate sale"})
        self.assertEqual(self.business.stock, {"item-1": 0})
        self.assertEqual(len(self.business.sales), 2)
        movements = [
            movement.quantity_delta
            for movement in self.business.stock_movements.values()
            if movement.reference_id == "sale-http-2"
        ]
        self.assertEqual(movements, [-1])

    def test_connected_sale_reports_each_sold_item_once(self) -> None:
        self.business.register_item("item-2", "SKU-2", "Gadget", 500)
        order = self.business.create_purchase_order(
            "supplier-2", [("item-2", 2)], order_id="po-http-2"
        )
        self.business.receive_purchase_order(order.order_id, "receipt-http-2")

        status, _, payload = self.post_sale(
            self.sale_body(
                sale_id="sale-http-3",
                lines=[
                    {"item_id": "item-2", "quantity": 1, "unit_price_cents": 500},
                    {"item_id": "item-1", "quantity": 1, "unit_price_cents": 1250},
                    {"item_id": "item-2", "quantity": 1, "unit_price_cents": 500},
                ],
            )
        )

        self.assertEqual(status, 201)
        self.assertEqual(payload["total_cents"], 2250)
        self.assertEqual(
            payload["stock"],
            [{"item_id": "item-2", "quantity": 0}, {"item_id": "item-1", "quantity": 0}],
        )
        self.assertEqual(self.business.stock, {"item-1": 0, "item-2": 0})

    def test_connected_sale_rejects_more_than_the_available_stock(self) -> None:
        status, _, payload = self.post_sale(
            self.sale_body(
                sale_id="sale-http-no-stock",
                lines=[{"item_id": "item-1", "quantity": 2, "unit_price_cents": 1250}],
            )
        )

        self.assertEqual(status, 409)
        self.assertEqual(payload, {"error": "insufficient stock"})
        self.assertEqual(list(self.business.sales), ["sale-http-1"])
        self.assertEqual(self.business.stock, {"item-1": 1})

    def test_malformed_connected_sale_requests_are_rejected(self) -> None:
        cases: tuple[tuple[str, object], ...] = (
            ("not-json", _RAW),
            ("not-an-object", [self.sale_body()]),
            ("missing-customer", self.sale_body(customer_id="")),
            ("missing-sale-id", {k: v for k, v in self.sale_body().items() if k != "sale_id"}),
            ("empty-lines", self.sale_body(lines=[])),
            ("lines-not-a-list", self.sale_body(lines="one-line")),
            ("line-not-an-object", self.sale_body(lines=[["item-1", 1, 1250]])),
            ("missing-item", self.sale_body(lines=[{"quantity": 1, "unit_price_cents": 1250}])),
            ("zero-quantity", self.sale_body(
                lines=[{"item_id": "item-1", "quantity": 0, "unit_price_cents": 1250}]
            )),
            ("fractional-quantity", self.sale_body(
                lines=[{"item_id": "item-1", "quantity": 1.5, "unit_price_cents": 1250}]
            )),
            ("fractional-price", self.sale_body(
                lines=[{"item_id": "item-1", "quantity": 1, "unit_price_cents": 12.5}]
            )),
            ("unknown-item", self.sale_body(
                lines=[{"item_id": "missing-item", "quantity": 1, "unit_price_cents": 1250}]
            )),
        )
        for name, body in cases:
            with self.subTest(case=name):
                if body is _RAW:
                    status, _, payload = self.post_sale(
                        None, raw=b"{not json", content_type="application/json"
                    )
                else:
                    status, _, payload = self.post_sale(body)

                self.assertEqual(status, 400)
                self.assertEqual(payload, {"error": "invalid request"})

        self.assertEqual(list(self.business.sales), ["sale-http-1"])
        self.assertEqual(self.business.stock, {"item-1": 1})

    def test_connected_sale_requires_json_content_type_and_a_bounded_body(self) -> None:
        status, _, payload = self.post_sale(
            self.sale_body(), content_type="text/plain"
        )
        self.assertEqual(status, 415)
        self.assertEqual(payload, {"error": "unsupported media type"})

        status, _, payload = self.post_sale(self.sale_body(), content_type=None)
        self.assertEqual(status, 415)
        self.assertEqual(payload, {"error": "unsupported media type"})

        oversized = b'{"customer_id":"' + b"x" * (64 * 1024) + b'"}'
        status, _, payload = self.post_sale(
            self.sale_body(), raw=oversized, content_type="application/json"
        )
        self.assertEqual(status, 413)
        self.assertEqual(payload, {"error": "payload too large"})

        self.assertEqual(list(self.business.sales), ["sale-http-1"])

    def test_connected_sale_maps_unexpected_failures_to_an_opaque_500(self) -> None:
        with mock.patch.object(
            self.business,
            "create_manual_sale",
            side_effect=RuntimeError("database socket at /secret/path failed"),
        ):
            status, _, payload = self.post_sale(self.sale_body())

        self.assertEqual(status, 500)
        self.assertEqual(payload, {"error": "internal server error"})
        self.assertNotIn("secret", json.dumps(payload))
        self.assertEqual(list(self.business.sales), ["sale-http-1"])

    def test_a_retry_of_the_same_command_replays_the_stored_receipt(self) -> None:
        store = InMemorySaleCommandStore()
        server = self.store_server(store)

        with mock.patch.object(
            self.business,
            "create_manual_sale",
            wraps=self.business.create_manual_sale,
        ) as create_sale:
            first_status, _, first = self.post_sale(self.sale_body(), server=server)
            second_status, _, second = self.post_sale(self.sale_body(), server=server)

        self.assertEqual((first_status, second_status), (201, 201))
        self.assertEqual(first, second)
        self.assertEqual(first["sale_id"], "sale-http-2")
        # The replay never re-ran the domain, so stock moved exactly once.
        self.assertEqual(create_sale.call_count, 1)
        self.assertEqual(self.business.stock, {"item-1": 0})
        self.assertEqual(len(self.business.sales), 2)

    def test_the_same_sale_id_with_another_request_is_a_conflict(self) -> None:
        store = InMemorySaleCommandStore()
        server = self.store_server(store)
        status, _, _ = self.post_sale(self.sale_body(), server=server)
        self.assertEqual(status, 201)

        conflict_status, _, conflict = self.post_sale(
            self.sale_body(customer_id="customer-http-3"), server=server
        )

        self.assertEqual(conflict_status, 409)
        self.assertEqual(conflict, {"error": "sale id conflict"})
        # The rejected request left the accepted receipt and the stock alone.
        replay_status, _, replay = self.post_sale(self.sale_body(), server=server)
        self.assertEqual(replay_status, 201)
        self.assertEqual(replay["customer_id"], "customer-http-2")
        self.assertEqual(self.business.stock, {"item-1": 0})

    def test_a_reserved_but_unfinished_command_is_reported_as_in_progress(self) -> None:
        store = InMemorySaleCommandStore()
        server = self.store_server(store)
        body = self.sale_body()
        lines = cast(list[dict[str, object]], body["lines"])
        reservation = store.reserve(
            "sale-http-2", sale_command_hash(("customer-http-2", "sale-http-2", lines))
        )
        self.assertEqual(reservation.outcome, RESERVED)

        status, _, payload = self.post_sale(body, server=server)

        self.assertEqual(status, 409)
        self.assertEqual(payload, {"error": "sale in progress"})
        self.assertEqual(list(self.business.sales), ["sale-http-1"])
        self.assertEqual(self.business.stock, {"item-1": 1})

    def test_a_failed_command_releases_its_sale_id_for_a_later_attempt(self) -> None:
        store = InMemorySaleCommandStore()
        server = self.store_server(store)
        rejected_status, _, _ = self.post_sale(
            self.sale_body(
                sale_id="sale-http-no-stock",
                lines=[{"item_id": "item-1", "quantity": 2, "unit_price_cents": 1250}],
            ),
            server=server,
        )
        self.assertEqual(rejected_status, 409)

        later_status, _, later = self.post_sale(
            self.sale_body(
                sale_id="sale-http-no-stock",
                lines=[{"item_id": "item-1", "quantity": 1, "unit_price_cents": 1250}],
            ),
            server=server,
        )

        self.assertEqual(later_status, 201)
        self.assertEqual(later["sale_id"], "sale-http-no-stock")
        self.assertEqual(self.business.stock, {"item-1": 0})

    def test_a_rejected_content_type_lets_the_client_read_the_response(self) -> None:
        # Answering before the request body is read makes Windows reset the
        # connection, so the client sees a transport abort instead of the 415.
        for repeat in range(REPEATED_REJECTIONS):
            for content_type in ("text/plain", None):
                with self.subTest(repeat=repeat, content_type=content_type):
                    status, _, payload = self.post_sale(
                        self.sale_body(), content_type=content_type
                    )

                    self.assertEqual(status, 415)
                    self.assertEqual(payload, {"error": "unsupported media type"})


class RejectedRequestBodyTests(unittest.TestCase):
    """A body rejected before it is read must be drained within its bounds.

    The wire symptom is a rare connection reset, so it is only observed by
    repeating it.  These cases drive the reader directly and assert the drain,
    which fails every time the drain is missing instead of once in eighty.
    """

    def reject(self, headers: dict[str, str], body: bytes) -> tuple[int, str, int]:
        """Return the status, error code, and body bytes the handler consumed."""

        handler = ConnectHandler.__new__(ConnectHandler)
        raw = "".join(f"{name}: {value}\r\n" for name, value in headers.items())
        handler.headers = cast(
            "HTTPMessage", parse_headers(io.BytesIO(raw.encode("utf-8")))
        )
        stream = io.BytesIO(body)
        handler.rfile = stream
        with self.assertRaises(_RequestError) as caught:
            handler._read_json_body()
        return caught.exception.status, caught.exception.code, stream.tell()

    def test_a_wrong_or_missing_content_type_drains_the_declared_body(self) -> None:
        body = b'{"sale_id":"sale-http-2"}'
        for headers in (
            {"Content-Type": "text/plain", "Content-Length": str(len(body))},
            {"Content-Length": str(len(body))},
        ):
            with self.subTest(headers=headers):
                self.assertEqual(
                    self.reject(headers, body),
                    (415, "unsupported media type", len(body)),
                )

    def test_an_oversized_body_is_drained_only_within_the_drain_limit(self) -> None:
        oversized = b"x" * (MAX_BODY_BYTES + 1)
        self.assertEqual(
            self.reject(
                {
                    "Content-Type": "application/json",
                    "Content-Length": str(len(oversized)),
                },
                oversized,
            ),
            (413, "payload too large", len(oversized)),
        )
        # Beyond the drain limit nothing is read, so memory stays bounded.
        self.assertEqual(
            self.reject(
                {
                    "Content-Type": "application/json",
                    "Content-Length": str(DRAIN_LIMIT_BYTES + 1),
                },
                b"x" * 16,
            ),
            (413, "payload too large", 0),
        )

    def test_an_unusable_content_length_reads_nothing(self) -> None:
        for length in (None, "not-a-number", "-1", "1.5"):
            headers = {"Content-Type": "application/json"}
            if length is not None:
                headers["Content-Length"] = length
            with self.subTest(length=length):
                self.assertEqual(
                    self.reject(headers, b'{"sale_id":"sale-http-2"}'),
                    (400, "invalid request", 0),
                )


if __name__ == "__main__":
    unittest.main()
