"""Small authenticated loopback HTTP transport for Atlas Connect.

This is a deliberately narrow transport smoke, not pairing, TLS, or a
production security boundary.  Business and protocol state remain
process-local and are supplied by the caller.  The server only ever binds a
loopback address, and every connect route also refuses a non-loopback peer.

A connected sale can be made idempotent by ``sale_id`` by injecting a
:class:`~atlas_erp.sale_store.SaleCommandStore`.  That store holds the receipt
of the sale command only; the business state behind it stays in memory unless a
:class:`~atlas_erp.business_store.BusinessStore` is injected too, in which case
the item master, sales, journals, and stock movements are written through and
reloaded on the next start.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
import sys
import threading
from collections.abc import Iterable, Mapping
from http.server import BaseHTTPRequestHandler, HTTPServer
from ipaddress import ip_address
from typing import Any, cast
from urllib.parse import urlsplit

from .business import (
    Business,
    BusinessError,
    DuplicateSaleError,
    InsufficientStockError,
    StockMovement,
)
from .business_store import BusinessStore, PostgresBusinessStore
from .protocol import ProtocolKernel
from .sale_store import (
    IN_PROGRESS,
    PAYLOAD_CONFLICT,
    REPLAY,
    PostgresSaleCommandStore,
    SaleCommandStore,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4310
TOKEN_ENV = "ATLAS_ERP_CONNECT_TOKEN"
HOST_ENV = "ATLAS_ERP_CONNECT_HOST"
PORT_ENV = "ATLAS_ERP_CONNECT_PORT"
DATABASE_ENV = "ATLAS_ERP_DATABASE_URL"
JSON_CONTENT_TYPE = "application/json"
MANUAL_SALES_CAPABILITY = "sales.manual_sales"
MAX_BODY_BYTES = 64 * 1024
# A rejected body is drained up to this size before the connection is closed.
DRAIN_LIMIT_BYTES = 1024 * 1024
# Only the one method each known route serves; anything else is a 405.
_ROUTE_METHODS = {
    "/health": "GET",
    "/connect/manifest": "GET",
    "/connect/audit": "GET",
    "/connect/sales": "POST",
}


class _RequestError(Exception):
    """One client-caused request problem, mapped to a status and error code.

    ``close`` marks the cases where the request body was not read, so the
    connection cannot be reused for another request.
    """

    def __init__(self, status: int, code: str, *, close: bool = False) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.close = close


def _canonical_json(value: object) -> bytes:
    """Return one deterministic JSON representation for snapshot hashing."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def snapshot_cursor(data: object) -> str:
    """Return a deterministic opaque cursor for a local audit snapshot."""

    return hashlib.sha256(_canonical_json(data)).hexdigest()


def sale_command_hash(request: tuple[str, str, list[dict[str, object]]]) -> str:
    """Return the canonical hash that identifies one connected sale command.

    The hash covers the *validated* request, not the posted bytes, so JSON key
    order and insignificant whitespace do not change the identity of a
    command, while a different sale, customer, quantity, or price does.
    """

    customer_id, sale_id, lines = request
    return hashlib.sha256(
        _canonical_json(
            {
                "customer_id": customer_id,
                "sale_id": sale_id,
                "lines": [dict(line) for line in lines],
            }
        )
    ).hexdigest()


def wire_manifest(manifest: Mapping[str, object]) -> dict[str, object]:
    """Convert the local capability-permission map to the shared wire shape.

    The in-process ERP manifest keeps permissions keyed by capability.  The
    first HTTP profile exposes the equivalent scoped strings, for example
    ``inventory.stock:read``.
    """

    raw_permissions = cast(
        Mapping[str, Iterable[str]], manifest["permissions"]
    )
    permissions = sorted(
        {
            f"{capability}:{permission}"
            for capability, values in raw_permissions.items()
            for permission in values
        }
    )
    return {
        "app_id": manifest["app_id"],
        "connect_version": manifest["connect_version"],
        "capabilities": list(cast(Iterable[str], manifest["capabilities"])),
        "permissions": permissions,
    }


def _normalise_host(host: str) -> str:
    if not isinstance(host, str) or not host.strip():
        raise ValueError("connect host must be a loopback address")
    value = host.strip()
    if value.lower() == "localhost":
        return DEFAULT_HOST
    try:
        address = ip_address(value)
    except ValueError as exc:
        raise ValueError("connect host must be a loopback address") from exc
    if not address.is_loopback:
        raise ValueError("connect host must be a loopback address")
    return value


def _normalise_port(port: int) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("connect port must be an integer from 0 to 65535")
    return port


def _is_loopback(address: str) -> bool:
    """Return True for a loopback peer, including IPv4-mapped IPv6 peers."""

    try:
        parsed = ip_address(address)
    except ValueError:
        return False
    mapped = getattr(parsed, "ipv4_mapped", None)
    return parsed.is_loopback or (mapped is not None and mapped.is_loopback)


def _request_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _RequestError(400, "invalid request")
    # Normalise here so the idempotency key, the hash, and the domain value are
    # the same string a padded request carries.
    return value.strip()


def _sale_request(payload: object) -> tuple[str, str, list[dict[str, object]]]:
    """Validate one connected manual-sale request body into sale arguments."""

    if not isinstance(payload, dict):
        raise _RequestError(400, "invalid request")
    customer_id = _request_text(payload.get("customer_id"))
    sale_id = _request_text(payload.get("sale_id"))
    raw_lines = payload.get("lines")
    if not isinstance(raw_lines, list) or not raw_lines:
        raise _RequestError(400, "invalid request")
    return customer_id, sale_id, [_sale_request_line(line) for line in raw_lines]


def _sale_request_line(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _RequestError(400, "invalid request")
    item_id = _request_text(value.get("item_id"))
    quantity = value.get("quantity")
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
        raise _RequestError(400, "invalid request")
    unit_price_cents = value.get("unit_price_cents")
    if isinstance(unit_price_cents, bool) or not isinstance(unit_price_cents, int):
        raise _RequestError(400, "invalid request")
    return {
        "item_id": item_id,
        "quantity": quantity,
        "unit_price_cents": unit_price_cents,
    }


class _ConnectHTTPServer(HTTPServer):
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        business: Business,
        protocol: ProtocolKernel,
        token: str,
        sale_store: SaleCommandStore | None,
        business_store: BusinessStore | None,
    ) -> None:
        self.business = business
        self.protocol = protocol
        self.token = token
        self.sale_store = sale_store
        self.business_store = business_store
        super().__init__(server_address, handler)


class ConnectHandler(BaseHTTPRequestHandler):
    """HTTP handler for the Connect read profile and one guarded sale write.

    The sale write is deliberately thin: it validates the request, checks every
    submitted unit price against the local item master, and then calls the same
    :class:`~atlas_erp.business.Business` manual-sale path as the local console
    would, so stock and journal invariants still hold.  It     never retries.

    When a sale store is injected, ``sale_id`` becomes a durable idempotency
    key: an identical retry replays the stored receipt without calling the
    domain again, and a different request for the same ``sale_id`` is a
    conflict.  Without one, a retry of an already accepted ``sale_id`` gets
    ``409 duplicate sale`` from the in-memory domain.

    When a business store is injected as well, a posted sale is written through
    *before* its receipt is completed, so a store that cannot record the sale
    leaves the receipt uncompleted and the peer gets no ``201``.  That is the
    same direction the abort below already takes: a receipt a peer may replay
    is only written once the business facts behind it are durable.

    The result carries the posted sale record in the same typed shape as the
    audit snapshot, so a peer can validate what it was handed without a second
    read.
    """


    server: Any

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if not self._loopback_peer():
            return
        path = self._path()
        if path == "/health":
            self._send_json(
                200,
                {
                    "app_id": self.server.protocol.app_id,
                    "status": "ok",
                    "connect_version": self.server.protocol.connect_version,
                },
            )
            return

        if path == "/connect/manifest":
            if not self._authenticated():
                self._unauthorized()
                return
            self._send_json(
                200,
                wire_manifest(self.server.protocol.manifest()),
            )
            return

        if path == "/connect/audit":
            if not self._authenticated():
                self._unauthorized()
                return
            try:
                data = self.server.business.audit_snapshot().to_dict()
                payload = {
                    "app_id": self.server.protocol.app_id,
                    "type": "snapshot",
                    "capability": "audit.snapshot",
                    "connect_version": self.server.protocol.connect_version,
                    "cursor": snapshot_cursor(data),
                    "data": data,
                }
                self._send_json(200, payload)
            except (TypeError, ValueError):
                self._send_json(500, {"error": "internal server error"})
            return

        # 405 with the allowed method for a known route, 404 otherwise.
        self._method_not_allowed()

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        if not self._loopback_peer():
            return
        if self._path() != "/connect/sales":
            self._method_not_allowed()
            return
        if not self._authenticated():
            self._unauthorized()
            return
        try:
            request = _sale_request(self._read_json_body())
            payload = self._post_connected_sale(request)
        except _RequestError as error:
            if error.close:
                self.close_connection = True
            self._send_json(error.status, {"error": error.code})
            return
        except Exception:
            # Never leak an internal failure detail to the caller.
            self._send_json(500, {"error": "internal server error"})
            return
        self._send_json(201, payload)

    def _post_connected_sale(
        self, request: tuple[str, str, list[dict[str, object]]]
    ) -> dict[str, object]:
        """Post one guarded manual sale, or replay an earlier identical one.

        Every path here answers ``201`` with a receipt or raises; the only
        statuses a caller sees otherwise are the request errors it already maps.
        """

        store = self.server.sale_store
        if store is None:
            return self._create_manual_sale(request)

        _, sale_id, _ = request
        reservation = store.reserve(sale_id, sale_command_hash(request))
        if reservation.outcome == REPLAY:
            # The stored receipt is returned unchanged, without touching the
            # domain, so a retry across a restart cannot apply the sale twice.
            return cast("dict[str, object]", reservation.response)
        if reservation.outcome == PAYLOAD_CONFLICT:
            raise _RequestError(409, "sale id conflict")
        if reservation.outcome == IN_PROGRESS:
            raise _RequestError(409, "sale in progress")
        try:
            payload = self._create_manual_sale(request)
            # The durable write comes before the receipt on purpose: a store
            # that cannot record the sale aborts the key below, so the peer is
            # never handed a 201 for a sale no restart would still know about.
            self._write_business_state(sale_id)
        except BaseException:
            # A rejected command must not burn the idempotency key.
            store.abort(sale_id)
            raise
        store.complete(sale_id, payload)
        return payload

    def _write_business_state(self, sale_id: str) -> None:
        """Write one posted sale, its journal, and its movements through.

        A no-op without a business store, where the in-memory domain is the
        only state there is.  The movements are read back off the domain rather
        than rebuilt, so the durable movement ids are the ones the domain
        itself minted.
        """

        store = self.server.business_store
        if store is None:
            return
        business = self.server.business
        sale = business.get_sale(sale_id)
        store.save_sale(sale, business.get_journal_for_sale(sale_id))
        store.save_movements(_sale_movements(business, sale_id))

    def _create_manual_sale(
        self, request: tuple[str, str, list[dict[str, object]]]
    ) -> dict[str, object]:
        """Post one guarded manual sale and return its wire result."""

        business = self.server.business
        customer_id, sale_id, lines = request
        try:
            for line in lines:
                item = business.get_item(cast(str, line["item_id"]))
                if line["unit_price_cents"] != item.price_cents:
                    raise _RequestError(400, "price mismatch")
            sale = business.create_manual_sale(customer_id, lines, sale_id=sale_id)
        except _RequestError:
            raise
        except DuplicateSaleError as exc:
            raise _RequestError(409, "duplicate sale") from exc
        except InsufficientStockError as exc:
            raise _RequestError(409, "insufficient stock") from exc
        except BusinessError as exc:
            raise _RequestError(400, "invalid request") from exc
        journal = business.get_journal_for_sale(sale.sale_id)
        return {
            "app_id": self.server.protocol.app_id,
            "capability": MANUAL_SALES_CAPABILITY,
            "sale_id": sale.sale_id,
            "customer_id": sale.customer_id,
            "journal_id": sale.journal_id,
            "total_cents": sale.total_cents,
            # The posted sale record, in the same typed shape as the audit
            # snapshot, so a peer can validate the result it was handed.
            "lines": [
                {
                    "item_id": line.item_id,
                    "quantity": line.quantity,
                    "unit_price_cents": line.unit_price_cents,
                    "total_cents": line.total_cents,
                }
                for line in sale.lines
            ],
            "stock": [
                {"item_id": item_id, "quantity": business.stock_for(item_id)}
                for item_id in dict.fromkeys(
                    cast(str, line["item_id"]) for line in lines
                )
            ],
            "journal_balanced": (
                journal.total_debits_cents == journal.total_credits_cents
            ),
        }

    def _drain_body(self) -> None:
        """Read and discard a rejected body that is small enough to drain.

        Answering before the body is read makes the platform reset the
        connection, and the client then loses the status it was supposed to
        get.  Draining first lets the client finish writing and read the
        response.  The read is bounded twice over: a declared length above
        :data:`DRAIN_LIMIT_BYTES` is left alone, and an unusable length drains
        nothing instead of reading to end of stream.
        """

        try:
            length = int(self.headers.get("Content-Length") or -1)
        except ValueError:
            return
        if 0 <= length <= DRAIN_LIMIT_BYTES:
            self.rfile.read(length)

    def _read_json_body(self) -> object:
        """Read one bounded JSON body, or raise the matching request error."""

        content_type = self.headers.get("Content-Type") or ""
        if content_type.split(";", 1)[0].strip().lower() != JSON_CONTENT_TYPE:
            self._drain_body()
            raise _RequestError(415, "unsupported media type")
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length) if raw_length is not None else -1
        except ValueError as exc:
            raise _RequestError(400, "invalid request") from exc
        if length < 0:
            raise _RequestError(400, "invalid request")
        if length > MAX_BODY_BYTES:
            # Read and discard a rejected body that is still small enough to
            # drain, so the client can finish writing instead of being reset.
            self._drain_body()
            raise _RequestError(413, "payload too large", close=True)
        body = self.rfile.read(length)
        if len(body) != length:
            raise _RequestError(400, "invalid request", close=True)
        try:
            return json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _RequestError(400, "invalid request") from exc

    def do_PUT(self) -> None:  # noqa: N802 - stdlib handler API
        self._method_not_allowed()

    def do_PATCH(self) -> None:  # noqa: N802 - stdlib handler API
        self._method_not_allowed()

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib handler API
        self._method_not_allowed()

    def do_OPTIONS(self) -> None:  # noqa: N802 - stdlib handler API
        self._method_not_allowed()

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler API
        self._method_not_allowed()

    def do_TRACE(self) -> None:  # noqa: N802 - stdlib handler API
        self._method_not_allowed()

    def do_CONNECT(self) -> None:  # noqa: N802 - stdlib handler API
        self._method_not_allowed()

    def __getattr__(self, name: str) -> Any:
        # BaseHTTPRequestHandler otherwise turns less common HTTP methods into
        # an HTML 501 response.  Treat every other method as 405 for this
        # deliberately small profile.
        if name.startswith("do_"):
            return self._method_not_allowed
        raise AttributeError(name)

    def log_message(self, format: str, *args: object) -> None:
        # Do not log request lines or headers, where a caller could place data.
        return

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        # Keep framework-generated errors JSON-shaped as well.  An unknown
        # method is a method error for this API, not a 501.
        if code == 501:
            self._method_not_allowed()
            return
        self._send_json(code, {"error": message or self.responses.get(code, ("error",))[0]})

    def _path(self) -> str:
        try:
            return urlsplit(self.path).path
        except ValueError:
            return self.path.split("?", 1)[0]

    def _loopback_peer(self) -> bool:
        if _is_loopback(self.client_address[0]):
            return True
        self._send_json(403, {"error": "forbidden"})
        return False

    def _authenticated(self) -> bool:
        header = self.headers.get("Authorization")
        if header is None:
            return False
        parts = header.split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return False
        return hmac.compare_digest(
            parts[1].encode("utf-8"), self.server.token.encode("utf-8")
        )

    def _method_not_allowed(self) -> None:
        allowed = _ROUTE_METHODS.get(self._path())
        if allowed is None:
            self._not_found()
            return
        self._send_json(
            405,
            {"error": "method not allowed"},
            headers={"Allow": allowed},
        )

    def _unauthorized(self) -> None:
        self._send_json(
            401,
            {"error": "unauthorized"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    def _not_found(self) -> None:
        self._send_json(404, {"error": "not found"})

    def _send_json(
        self,
        status: int,
        payload: object,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", JSON_CONTENT_TYPE)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if getattr(self, "command", None) != "HEAD":
            self.wfile.write(body)


def _sale_movements(business: Business, sale_id: str) -> list[StockMovement]:
    """Return the stock movements one sale produced, in recorded order.

    A sale movement is the one whose own reason is ``sale`` and whose
    reference is the sale, which is what the domain set when it posted the
    sale.  A receipt movement is excluded by the reason, so an id shared by a
    receipt and a sale cannot pull the wrong lines in.
    """

    return [
        movement
        for movement in business.stock_movements.values()
        if movement.reason == "sale" and movement.reference_id == sale_id
    ]


def _make_server(
    host: str,
    port: int,
    business: Business,
    protocol: ProtocolKernel,
    token: str,
    sale_store: SaleCommandStore | None,
    business_store: BusinessStore | None,
) -> _ConnectHTTPServer:
    if ":" in host:
        class _IPv6ConnectHTTPServer(_ConnectHTTPServer):
            address_family = socket.AF_INET6

        server_type = _IPv6ConnectHTTPServer
    else:
        server_type = _ConnectHTTPServer
    return server_type(
        (host, port),
        ConnectHandler,
        business,
        protocol,
        token,
        sale_store,
        business_store,
    )


class ConnectServer:
    """A one-shot HTTP server around injected business and protocol objects.

    ``sale_store`` is optional.  Injecting one makes ``POST /connect/sales``
    idempotent by ``sale_id`` for the lifetime of that store; leaving it out
    keeps the in-memory behaviour where a repeated ``sale_id`` is rejected by
    the domain itself.

    ``business_store`` is optional too.  Injecting one makes the state a posted
    sale produces durable, so the receipt a peer can replay and the sale that
    receipt names both survive a restart.
    """

    def __init__(
        self,
        business: Business,
        protocol: ProtocolKernel,
        token: str,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        sale_store: SaleCommandStore | None = None,
        business_store: BusinessStore | None = None,
    ) -> None:
        if not isinstance(token, str) or not token.strip():
            raise ValueError("connect token must not be empty")
        self.business = business
        self.protocol = protocol
        self.sale_store = sale_store
        self.business_store = business_store
        self._host = _normalise_host(host)
        self._port = _normalise_port(port)
        self._server = _make_server(
            self._host,
            self._port,
            business,
            protocol,
            token,
            sale_store,
            business_store,
        )
        self._thread: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._closed = False

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def address(self) -> tuple[str, int]:
        return (self._host, self.port)

    @property
    def server_address(self) -> tuple[str, int]:
        return self.address

    @property
    def url(self) -> str:
        host = f"[{self._host}]" if ":" in self._host else self._host
        return f"http://{host}:{self.port}"

    def start(self) -> ConnectServer:
        """Start serving in a daemon thread and return immediately."""

        with self._state_lock:
            if self._closed:
                raise RuntimeError("connect server is closed")
            if self._thread is not None and self._thread.is_alive():
                return self
            self._thread = threading.Thread(
                target=self.serve_forever,
                name="atlas-erp-connect",
                daemon=True,
            )
            self._thread.start()
        return self

    def serve_forever(self) -> None:
        """Serve requests until :meth:`stop` or process termination."""

        if self._closed:
            raise RuntimeError("connect server is closed")
        try:
            self._server.serve_forever(poll_interval=0.1)
        finally:
            self._close_socket()

    def stop(self) -> None:
        """Stop serving and join a thread started by :meth:`start`."""

        with self._state_lock:
            thread = self._thread
        if thread is not None and thread.is_alive():
            if thread is threading.current_thread():
                self._close_socket()
                return
            self._server.shutdown()
            thread.join(timeout=5)
            if thread.is_alive():
                raise TimeoutError("connect server did not stop")
        self._close_socket()
        with self._state_lock:
            self._thread = None

    def close(self) -> None:
        """Close the listening socket; prefer :meth:`stop` for a running server."""

        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            self.stop()
        else:
            self._close_socket()
        if self.sale_store is not None:
            self.sale_store.close()
        if self.business_store is not None:
            self.business_store.close()

    def wait(self, timeout: float | None = None) -> None:
        """Wait for a server started with :meth:`start` to finish."""

        if self._thread is None:
            raise RuntimeError("connect server is not started")
        self._thread.join(timeout=timeout)

    def _close_socket(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            self._server.server_close()

    def __enter__(self) -> ConnectServer:
        return self.start()

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.stop()


def _fixture() -> tuple[Business, ProtocolKernel]:
    """Build the in-memory state the smoke server serves.

    The item master price is the price a connected peer must submit, and the
    received quantity leaves stock for one further connected sale.  All of it
    is process-local smoke data, not seeded production data.
    """

    business = Business()
    item = business.register_item("item-1", "SKU-1", "Widget", 1250)
    order = business.create_purchase_order(
        "supplier-1", [(item.item_id, 5)], order_id="po-1"
    )
    business.receive_purchase_order(order.order_id, receipt_id="receipt-1")
    business.create_manual_sale(
        "customer-1",
        [{"item_id": item.item_id, "quantity": 1, "unit_price_cents": 1250}],
        sale_id="sale-1",
    )
    return business, ProtocolKernel(business.registry)


def _environment_port() -> int:
    raw = os.environ.get(PORT_ENV)
    if raw is None:
        return DEFAULT_PORT
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{PORT_ENV} must be an integer from 0 to 65535") from exc


def _database_store() -> SaleCommandStore | None:
    """Return the durable sale store, or None when no database is configured.

    An unset or blank ``ATLAS_ERP_DATABASE_URL`` keeps the in-memory behaviour.
    A configured but unusable database is a startup failure: silently falling
    back would hand a peer a receipt store that does not survive a restart.
    """

    dsn = os.environ.get(DATABASE_ENV, "")
    if not dsn.strip():
        return None
    try:
        return PostgresSaleCommandStore(dsn)
    except Exception as exc:
        # The driver error can quote the connection string, so only the
        # variable name is reported.
        raise ValueError(f"{DATABASE_ENV} could not be used") from exc


def _business_store() -> BusinessStore | None:
    """Return the durable business store, on exactly the sale store's terms.

    Same variable, same blank-means-in-memory rule, and the same refusal to
    report anything but the variable name when a configured database cannot be
    used.  Two stores over one URL is deliberate: they own different tables and
    merging them into one transaction is a later slice.
    """

    dsn = os.environ.get(DATABASE_ENV, "")
    if not dsn.strip():
        return None
    try:
        return PostgresBusinessStore(dsn)
    except Exception as exc:
        raise ValueError(f"{DATABASE_ENV} could not be used") from exc


def _seed_business_store(store: BusinessStore, business: Business) -> None:
    """Write the smoke fixture into an empty store, once.

    Without this the store would never hold an item, and a restart would restore
    a domain with no master to sell from.  The fixture's purchase order and
    receipt have no table yet, but its movements do, so the stock a reload
    derives is the stock that was there.

    ponytail: one transaction per call, so a seed interrupted half way leaves a
    partial store that a later boot restores as truth.  Seed the purchasing
    tables with the rest of the schema when they arrive.
    """

    for item in business.items.values():
        store.save_item(item)
    for sale in business.sales.values():
        store.save_sale(sale, business.get_journal_for_sale(sale.sale_id))
    store.save_movements(business.stock_movements.values())


def _startup_business(
    store: BusinessStore | None,
) -> tuple[Business, ProtocolKernel]:
    """Return the domain this process serves and the kernel over it.

    With no business store this is the in-memory smoke fixture, unchanged.  With
    one, stored state wins: an empty store is seeded from the fixture once, and
    a populated store is restored instead of the fixture, which is what makes a
    restart serve the sales it already acknowledged instead of forgetting them.
    """

    if store is None:
        return _fixture()
    records = store.load()
    if not records.items:
        business, protocol = _fixture()
        _seed_business_store(store, business)
        return business, protocol
    business = Business()
    business.restore(
        items=records.items,
        sales=records.sales,
        journals=records.journals,
        stock_movements=records.stock_movements,
    )
    return business, ProtocolKernel(business.registry)


def main() -> int:
    token = os.environ.get(TOKEN_ENV)
    if token is None or not token.strip():
        print(f"{TOKEN_ENV} must be set", file=sys.stderr)
        return 2

    try:
        host = os.environ.get(HOST_ENV, DEFAULT_HOST)
        port = _environment_port()
        store = _database_store()
        business_store = _business_store()
        business, protocol = _startup_business(business_store)
        server = ConnectServer(
            business,
            protocol,
            token,
            host=host,
            port=port,
            sale_store=store,
            business_store=business_store,
        )
    except (OSError, ValueError) as exc:
        print(f"could not start Atlas ERP Connect: {exc}", file=sys.stderr)
        return 2

    mode = "postgres" if store is not None else "in-memory"
    state = "postgres" if business_store is not None else "in-memory"
    print(
        f"Atlas ERP Connect listening on {server.url} "
        f"(sale receipts: {mode}, business state: {state})",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ConnectHandler",
    "ConnectServer",
    "DATABASE_ENV",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DRAIN_LIMIT_BYTES",
    "HOST_ENV",
    "JSON_CONTENT_TYPE",
    "MANUAL_SALES_CAPABILITY",
    "MAX_BODY_BYTES",
    "PORT_ENV",
    "TOKEN_ENV",
    "main",
    "sale_command_hash",
    "snapshot_cursor",
    "wire_manifest",
]
