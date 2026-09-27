"""Small authenticated loopback HTTP transport for Atlas Connect.

This is a deliberately narrow transport smoke, not pairing, TLS, or a
production security boundary.  Business and protocol state remain
process-local and are supplied by the caller.  The server only ever binds a
loopback address, and every connect route also refuses a non-loopback peer.

Authority is the protocol module's decision, not this file's: a guarded route
names the capability and the permission it exercises and the module answers
whether the peer that presented the request may.  For that question to have an
answer the credential has to name a peer, so the transport is configured with
one token per peer ``app_id`` in ``ATLAS_ERP_PEER_TOKENS`` -- a JSON object
mapping ``app_id`` to that peer's token, for example
``{"atlas-ecom": "<token>"}``.  One variable carries one peer or ten, and there
is no single-peer form: an alias for "the one peer" would keep the
identity-less configuration alive, which is the one thing a peer-scoped
credential exists to remove.

A connected sale can be made idempotent by ``sale_id`` by injecting a
:class:`~atlas_erp.sale_store.SaleCommandStore`.  That store holds the receipt
of the sale command only; the business state behind it stays in memory unless a
:class:`~atlas_erp.business_store.BusinessStore` is injected too, in which case
the item master, the purchasing a receipt's movements came from, sales,
journals, and stock movements are written through and reloaded on the next
start.
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
)
from .business_store import BusinessStore, PostgresBusinessStore
from .protocol import PermissionDeniedError, ProtocolError, ProtocolKernel
from .sale_store import (
    IN_PROGRESS,
    PAYLOAD_CONFLICT,
    REPLAY,
    PostgresSaleCommandStore,
    SaleCommandStore,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4310
PEER_TOKENS_ENV = "ATLAS_ERP_PEER_TOKENS"
HOST_ENV = "ATLAS_ERP_CONNECT_HOST"
PORT_ENV = "ATLAS_ERP_CONNECT_PORT"
DATABASE_ENV = "ATLAS_ERP_DATABASE_URL"
JSON_CONTENT_TYPE = "application/json"
MANUAL_SALES_CAPABILITY = "sales.manual_sales"
AUDIT_CAPABILITY = "audit.snapshot"
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
# The capability and permission each guarded route exercises.  This is the only
# statement the transport makes about a route, and it is a statement about the
# route rather than about the peer: which role may do what belongs to the module
# alone, so a row here is a question, not an answer.  It is a table rather than an
# argument at each call site so that a new guarded route without a row is a test
# failure instead of an unauthenticated answer.
#
# The sale row asks about ``propose`` and not ``write``, which is the whole point
# of the connected sale surviving enforcement.  ``write`` is reachable only by the
# master of the capability, and the master of a served capability is the app
# serving it (SPEC.md, G2), so a ``write`` row would answer 403 to every peer and
# there would be no connected checkout at all.  ``propose`` is the permission that
# lets a peer ask the master to decide, and the decision is what authorises the
# write: the domain write below still runs as the master and still runs only
# because the master resolved the proposal as accepted.
ROUTE_AUTHORITY: Mapping[str, tuple[str, str]] = {
    "/connect/sales": (MANUAL_SALES_CAPABILITY, "propose"),
    "/connect/audit": (AUDIT_CAPABILITY, "read"),
}


class _RequestError(Exception):
    """One client-caused request problem, mapped to a status and error code.

    ``close`` marks the cases where the request body was not read, so the
    connection cannot be reused for another request.  ``headers`` carries the
    response headers the status implies, such as the wait a retrying caller
    needs, so the hint is in the standard place rather than in the body.
    """

    def __init__(
        self,
        status: int,
        code: str,
        *,
        close: bool = False,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.close = close
        self.headers = headers


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


def _peer_tokens(value: object) -> dict[str, str]:
    """Return the peer-scoped credentials, refusing one that cannot name a peer.

    A credential map is only useful if it identifies who presented it, so the
    cases that would blur that are startup failures rather than warnings: no
    peers at all serves nobody, a blank ``app_id`` or token is not a peer, and
    one token shared by two peers makes the answer to "which peer is this?"
    depend on iteration order.
    """
    if not isinstance(value, Mapping):
        raise ValueError("peer tokens must map a peer app_id to its token")
    tokens: dict[str, str] = {}
    for peer_id, token in value.items():
        if not isinstance(peer_id, str) or not peer_id.strip():
            raise ValueError("peer token app_id must be a non-empty string")
        if not isinstance(token, str) or not token.strip():
            raise ValueError(f"connect token for {peer_id} must not be empty")
        if token in tokens.values():
            raise ValueError(f"one token cannot identify two peers: {peer_id}")
        tokens[peer_id.strip()] = token
    if not tokens:
        raise ValueError("at least one peer token must be configured")
    return tokens


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
        peer_tokens: Mapping[str, str],
        sale_store: SaleCommandStore | None,
        business_store: BusinessStore | None,
    ) -> None:
        self.business = business
        self.protocol = protocol
        self.peer_tokens = peer_tokens
        self.sale_store = sale_store
        self.business_store = business_store
        super().__init__(server_address, handler)


class ConnectHandler(BaseHTTPRequestHandler):
    """HTTP handler for the Connect read profile and one guarded sale write.

    Every connect route resolves the presented credential to a peer ``app_id``
    first, because a grant is held by an ``app_id`` and a request that names
    nobody cannot be held to one.  The two guarded routes then ask the protocol
    module whether that peer may exercise the capability they act on; this class
    never decides authority itself.

    A peer never writes.  The sale route asks the ``propose`` permission, and
    what it then does is submit the peer's intent as a proposal, have the master
    -- the only principal ``resolve_proposal`` permits -- decide it, and apply it
    as the master when the decision is acceptance.  Who accepts, and when, is
    policy this transport does not have yet; see ``_propose_and_apply``.

    The write itself is deliberately thin: it validates the request, checks every
    submitted unit price against the local item master, and then calls the same
    :class:`~atlas_erp.business.Business` manual-sale path as the local console
    would, so stock and journal invariants still hold.  It never retries.

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
            # Identity only, deliberately: see ManifestAndHealthTests in
            # tests/test_connect_authority.py.  A peer has to be able to read the
            # manifest to decide whether to pair, and pairing is what grants
            # authority, so a grant-gated manifest would make pairing impossible.
            if self._authenticated_peer() is None:
                self._unauthorized()
                return
            self._send_json(
                200,
                wire_manifest(self.server.protocol.manifest()),
            )
            return

        if path == "/connect/audit":
            if self._authorised_peer(path) is None:
                return
            try:
                data = self.server.business.audit_snapshot().to_dict()
                payload = {
                    "app_id": self.server.protocol.app_id,
                    "type": "snapshot",
                    "capability": AUDIT_CAPABILITY,
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
        peer_id = self._authorised_peer("/connect/sales")
        if peer_id is None:
            return
        try:
            request = _sale_request(self._read_json_body())
            payload = self._post_connected_sale(request, peer_id)
        except _RequestError as error:
            if error.close:
                self.close_connection = True
            self._send_json(error.status, {"error": error.code}, headers=error.headers)
            return
        except Exception:
            # Never leak an internal failure detail to the caller.
            self._send_json(500, {"error": "internal server error"})
            return
        self._send_json(201, payload)

    def _post_connected_sale(
        self, request: tuple[str, str, list[dict[str, object]]], peer_id: str
    ) -> dict[str, object]:
        """Post one guarded manual sale, or replay an earlier identical one.

        Every path here answers ``201`` with a receipt or raises; the only
        statuses a caller sees otherwise are the request errors it already maps.
        """

        store = self.server.sale_store
        if store is None:
            return self._propose_and_apply(peer_id, request)

        _, sale_id, _ = request
        reservation = store.reserve(sale_id, sale_command_hash(request))
        if reservation.outcome == REPLAY:
            # The stored receipt is returned unchanged, without touching the
            # domain, so a retry across a restart cannot apply the sale twice.
            return cast("dict[str, object]", reservation.response)
        if reservation.outcome == PAYLOAD_CONFLICT:
            raise _RequestError(409, "sale id conflict")
        if reservation.outcome == IN_PROGRESS:
            # The holder's lease is the wait, and ``Retry-After`` is where a
            # caller reads one, so a retrying peer is not left guessing.  The
            # hint is a header rather than body content because the body is the
            # stable error shape a client parses once.
            raise _RequestError(
                409,
                "sale in progress",
                headers=_retry_after(reservation.retry_after_seconds),
            )
        try:
            # The proposal is inside the reservation and not before it, so a
            # replayed key answers from the receipt without asking the master to
            # decide the same intent twice.
            payload = self._propose_and_apply(peer_id, request)
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

    def _propose_and_apply(
        self, peer_id: str, request: tuple[str, str, list[dict[str, object]]]
    ) -> dict[str, object]:
        """Turn one peer's request into a proposal the master decides, then apply it.

        This is the only route by which a peer's intent reaches the domain, and
        it is deliberately not a write: the peer holds ``propose`` and no more,
        the master is the only principal ``resolve_proposal`` permits, and the
        write below happens as the master and only because the master accepted.
        A peer that wanted a sale without the master's validation cannot express
        that, because the validation *is* the decision.

        Who accepts is **policy, not mechanism, and it is open**: the master
        decides here, immediately and deterministically, because the only
        decision available today is the domain's own -- insufficient stock, price
        mismatch, duplicate sale -- and an operator in the loop would need a
        decision surface this transport does not have.  What this flow does
        provide is the place one would live: a proposal is a pending record with
        a payload, a proposer, and a master that has not yet answered, which is
        the honest shape of an unanswered request.

        An unexpected failure leaves the proposal ``pending`` rather than
        ``rejected`` on purpose: the master did not decide, and a record that
        claims otherwise is worse than one that admits it.

        # ponytail: proposals live in the protocol adapter, which is process-local
        # and says so.  Two ceilings follow, both real and both recorded rather
        # than fixed here: a receipt replayed after a restart names a sale whose
        # proposal no longer exists, because the receipt is the durable record
        # and the decision is not; and one decided proposal is retained per
        # connected sale for the life of the process, because deleting the
        # decision on apply would throw away the only record of why it happened.
        # Give proposals a durable table before an operator decision can rely on
        # them, and before the retention is worth bounding.
        """

        customer_id, sale_id, lines = request
        proposal = self.server.protocol.submit_proposal(
            MANUAL_SALES_CAPABILITY,
            {
                "customer_id": customer_id,
                "sale_id": sale_id,
                "lines": [dict(line) for line in lines],
            },
            peer_id=peer_id,
        )
        try:
            payload = self._create_manual_sale(request)
        except _RequestError as error:
            # The domain's own refusal is the decision, and its code is the
            # reason: a peer is told the same thing either way.
            self.server.protocol.resolve_proposal(
                proposal.proposal_id, "rejected", error.code
            )
            raise
        self.server.protocol.resolve_proposal(
            proposal.proposal_id, "accepted", f"posted {payload['sale_id']}"
        )
        return payload

    def _write_business_state(self, sale_id: str) -> None:
        """Write one posted sale, its journal, and its movements through.

        A no-op without a business store, where the in-memory domain is the
        only state there is.  The movements are read back off the domain rather
        than rebuilt, so the durable movement ids are the ones the domain
        itself minted.

        The sale, its journal, and its movements are one transaction because
        stock is a sum over the movement rows rather than a stored level: a
        durable sale whose movements never landed is not a loud half-write but a
        number that is now permanently too high, and no later restart corrects
        it.  A crash between two bare calls would be exactly that.
        """

        store = self.server.business_store
        if store is None:
            return
        business = self.server.business
        sale = business.get_sale(sale_id)
        with store.transaction():
            store.save_sale(sale, business.get_journal_for_sale(sale_id))
            store.save_movements(business.sale_movements(sale_id))

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

    def _authenticated_peer(self) -> str | None:
        """Return the ``app_id`` the presented credential belongs to, or None.

        The credential is scoped to a peer precisely so the module can be asked
        about *someone*: one opaque token names no peer, so no grant could be
        consulted about whoever presented it.  Every configured credential is
        compared and none short-circuits, so the number of peers is not visible
        in how long the answer takes.
        """
        header = self.headers.get("Authorization")
        if header is None:
            return None
        parts = header.split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return None
        presented = parts[1].encode("utf-8")
        peer_id: str | None = None
        for candidate, token in self.server.peer_tokens.items():
            if hmac.compare_digest(presented, token.encode("utf-8")):
                peer_id = candidate
        return peer_id

    def _authorised_peer(self, path: str) -> str | None:
        """Return the peer id when the module permits it to use this route.

        ``ROUTE_AUTHORITY`` names the capability and permission the route
        exercises and the module answers, so nothing here restates which role
        may do what.  None means the request has already been answered: 401 for
        a credential this transport does not know, 403 for a known peer holding
        no grant that permits it.

        Both refusals answer before the body is read, which is the same shape as
        the rejected-content-type case ``_drain_body`` exists for.  A refused
        ``POST /connect/sales`` was measured delivering its 403 on 40 of 40
        sends, so nothing is drained here; if a change makes the connection
        close differently, that measurement is the thing to re-take.
        """
        peer_id = self._authenticated_peer()
        if peer_id is None:
            self._unauthorized()
            return None
        capability, permission = ROUTE_AUTHORITY[path]
        try:
            self.server.protocol.authorize(peer_id, capability, permission)
        except PermissionDeniedError:
            self._forbidden()
            return None
        return peer_id

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

    def _forbidden(self) -> None:
        # 403 rather than 401: the credential was recognised, so the peer is
        # known and retrying with the same one cannot help.  Only a grant changes
        # this answer, and a peer cannot mint one.
        self._send_json(403, {"error": "forbidden"})

    def _not_found(self) -> None:
        self._send_json(404, {"error": "not found"})

    def _send_json(
        self,
        status: int,
        payload: object,
        *,
        headers: Mapping[str, str] | None = None,
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


def _retry_after(seconds: int | None) -> dict[str, str] | None:
    """Return the ``Retry-After`` header for a wait hint, or no header at all.

    A store that reports no wait has nothing to say about when to try again, and
    an invented value would be worse than none, so the header is simply absent
    rather than defaulted to zero.
    """

    return None if seconds is None else {"Retry-After": str(seconds)}


def _make_server(
    host: str,
    port: int,
    business: Business,
    protocol: ProtocolKernel,
    peer_tokens: Mapping[str, str],
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
        peer_tokens,
        sale_store,
        business_store,
    )


class ConnectServer:
    """A one-shot HTTP server around injected business and protocol objects.

    ``peer_tokens`` maps a peer ``app_id`` to that peer's credential, and is the
    only way a request is identified: the handler asks the protocol module about
    the peer the presented token names.  There is no single-token form, because a
    token that names nobody is a token no grant can be held against.

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
        peer_tokens: Mapping[str, str],
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        sale_store: SaleCommandStore | None = None,
        business_store: BusinessStore | None = None,
    ) -> None:
        self.business = business
        self.protocol = protocol
        self.peer_tokens = _peer_tokens(peer_tokens)
        self.sale_store = sale_store
        self.business_store = business_store
        self._host = _normalise_host(host)
        self._port = _normalise_port(port)
        self._server = _make_server(
            self._host,
            self._port,
            business,
            protocol,
            self.peer_tokens,
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
    """Write the smoke fixture into an empty store, once, as one unit of work.

    Without this the store would never hold an item, and a restart would restore
    a domain with no master to sell from.  The records are written in the order
    the domain created them, purchasing included, so the movements a receipt
    produced are stored beside the receipt that produced them.

    The whole seed is one transaction because the next boot trusts whatever is
    there: a seed interrupted half way would otherwise leave a partial store that
    the next boot restores as truth.  ponytail: a connected sale is one such block
    too, but it is not the *same* block as its receipt, so a crash can still
    leave business state ahead of a ``pending`` receipt; merging the two stores
    into one transaction is a later slice.
    """

    with store.transaction():
        for item in business.items.values():
            store.save_item(item)
        for order in business.purchase_orders.values():
            store.save_purchase_order(order)
        for receipt in business.receipts.values():
            store.save_receipt(receipt)
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
        purchase_orders=records.purchase_orders,
        receipts=records.receipts,
        sales=records.sales,
        journals=records.journals,
        stock_movements=records.stock_movements,
    )
    return business, ProtocolKernel(business.registry)


CONNECTED_PEER_GRANTS: Mapping[str, Mapping[str, str]] = {
    "atlas-ecom": {
        MANUAL_SALES_CAPABILITY: "proposer",
        AUDIT_CAPABILITY: "reader",
    },
}
"""The grants this transport ships, and a record of debt rather than design.

``atlas-ecom`` is the one peer whose configuration is known here, and these two
rows are all of it:

* ``audit.snapshot:reader`` is what Ecom has always used and is what the read
  profile is for.
* ``sales.manual_sales:proposer`` is the role **both** product contracts already
  name for Ecom -- it submits intent, and ERP decides recognition, quantity, and
  value -- and it is the role SPEC.md gives a peer that may ask the master to
  change a capability without changing it.  It is a working authority now:
  ``POST /connect/sales`` asks the ``propose`` permission, so a peer holds
  exactly enough to ask, and the master decides and applies.

The grant that would let a peer write is ``master``, and it is not available:
one master per capability (SPEC.md, G2) is the module's own rule, the serving app
already holds every capability it serves, and ``ProtocolKernel.grant`` refuses a
second master.  So the write stays closed to peers by the module's semantics
rather than by this file, and the connected sale reaches the domain as a
proposal the master accepted rather than as a peer's request applied on trust.

``tests/test_connect_authority.py`` pins these two rows exactly, so the day they
change is a deliberate commit rather than a quiet widening of what a peer may do.
"""


def grant_configured_peers(protocol: ProtocolKernel) -> None:
    """Apply :data:`CONNECTED_PEER_GRANTS` to a kernel, one peer at a time."""

    for peer_id, capabilities in CONNECTED_PEER_GRANTS.items():
        for capability, role in capabilities.items():
            protocol.grant(capability, peer_id, role)


def _environment_peer_tokens() -> dict[str, str]:
    """Return the peer credentials named by the environment, or refuse.

    ``ATLAS_ERP_PEER_TOKENS`` is a JSON object mapping a peer ``app_id`` to that
    peer's token, for example ``{"atlas-ecom": "<token>"}``.  JSON because every
    other value this transport reads is JSON, and because one unambiguous format
    is what lets one variable carry one peer or ten without a second form to get
    wrong.  An unset or unreadable value is a startup failure rather than an empty
    map: an empty map would serve nobody while announcing itself as a started
    transport, and a value that cannot be parsed is a typo, not an absence.
    """

    raw = os.environ.get(PEER_TOKENS_ENV, "")
    if not raw.strip():
        raise ValueError(
            f"{PEER_TOKENS_ENV} must be set to a JSON object of peer app_id to token"
        )
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{PEER_TOKENS_ENV} must be a JSON object of peer app_id to token"
        ) from exc
    if not isinstance(value, dict):
        raise ValueError(
            f"{PEER_TOKENS_ENV} must be a JSON object of peer app_id to token"
        )
    return cast("dict[str, str]", value)


def main() -> int:
    try:
        peer_tokens = _environment_peer_tokens()
        host = os.environ.get(HOST_ENV, DEFAULT_HOST)
        port = _environment_port()
        store = _database_store()
        business_store = _business_store()
        business, protocol = _startup_business(business_store)
        grant_configured_peers(protocol)
        server = ConnectServer(
            business,
            protocol,
            peer_tokens,
            host=host,
            port=port,
            sale_store=store,
            business_store=business_store,
        )
    except (OSError, ValueError, ProtocolError) as exc:
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
    "AUDIT_CAPABILITY",
    "CONNECTED_PEER_GRANTS",
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
    "PEER_TOKENS_ENV",
    "PORT_ENV",
    "ROUTE_AUTHORITY",
    "grant_configured_peers",
    "main",
    "sale_command_hash",
    "snapshot_cursor",
    "wire_manifest",
]
