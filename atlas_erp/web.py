"""Loopback browser console for standalone Atlas ERP.

This is the product's first browser surface: a server-rendered HTML dashboard
plus three read-only JSON routes, built on the standard library with no
external CSS or JavaScript and no build step.

Everything it shows is demo fixture data.  The catalogue mirrors the committed
``db/seed.sql`` example rows through :mod:`atlas_erp.demo_catalog`, and the
sales, stock, and journal history is process-local state in a
:class:`~atlas_erp.business.Business` object that is rebuilt on every start and
lost on exit.  No database is read or written, and the page says so.

The listening socket is bound to ``127.0.0.1`` and there is deliberately no host
knob, so the console cannot be exposed to a network.  It listens on 4311, which
leaves the Connect API on 4310 usable at the same time.  Start it with
``python -m atlas_erp.web``.
"""

from __future__ import annotations

import html
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, cast
from urllib.parse import urlsplit

from .business import AuditSnapshot, Business, JournalEntry, Sale
from .demo_catalog import DemoCatalog, DemoItem, build_demo_catalog

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4311
PORT_ENV = "ATLAS_ERP_WEB_PORT"
JSON_CONTENT_TYPE = "application/json"
HTML_CONTENT_TYPE = "text/html; charset=utf-8"
TITLE = "Atlas ERP console"
RECENT_ACTIVITY_LIMIT = 5
# Only the one method each route serves; anything else is a 405.
_ROUTE_METHODS = {
    "/": "GET",
    "/api/health": "GET",
    "/api/catalog": "GET",
    "/api/audit": "GET",
}

# sale_id, customer_id, item_id, quantity.  Deterministic, in-memory, and lost
# on exit; the prices come from the item master, so the journal stays balanced
# by construction rather than by a second hardcoded amount.
_DEMO_SALES: tuple[tuple[str, str, str, int], ...] = (
    ("sale-2026-0918", "counter-breda", "item-aurora-linen-shirt", 2),
    ("sale-2026-0921", "web-utrecht", "item-harbor-wool-throw", 1),
    ("sale-2026-0924", "counter-breda", "item-aurora-linen-shirt", 1),
)

_STYLE = """
:root {
  color-scheme: light dark;
  --bg: #0f1115; --panel: #171a21; --line: #262b36;
  --ink: #e7eaf0; --muted: #98a2b3; --accent: #8ab8ff; --good: #5fd39a; --warn: #f0c26a;
}
@media (prefers-color-scheme: light) {
  :root {
    --bg: #f5f6f8; --panel: #ffffff; --line: #e2e5ea;
    --ink: #191c22; --muted: #5c6675; --accent: #1b5fd0; --good: #1a7f4f; --warn: #96650a;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 1.5rem; background: var(--bg); color: var(--ink);
  font: 15px/1.5 ui-sans-serif, system-ui, "Segoe UI", Roboto, sans-serif;
}
a { color: var(--accent); }
h1 { font-size: 1.5rem; margin: 0; }
h2 { font-size: 1.05rem; margin: 0 0 .75rem; }
h3 { font-size: 1rem; margin: 0 0 .2rem; }
code { font-family: ui-monospace, Consolas, monospace; font-size: .9em; }
header, section { margin: 0 0 1.5rem; }
header { display: flex; flex-wrap: wrap; gap: .5rem 1rem; align-items: baseline; }
nav { display: flex; gap: .75rem; flex-wrap: wrap; }
.muted { color: var(--muted); }
.banner {
  margin: .75rem 0 0; padding: .7rem .9rem; border: 1px solid var(--warn);
  border-radius: 8px; background: color-mix(in srgb, var(--warn) 12%, transparent);
}
.tiles { display: grid; gap: .75rem;
  grid-template-columns: repeat(auto-fill, minmax(9.5rem, 1fr)); }
.tile { padding: .7rem .8rem; border: 1px solid var(--line); border-radius: 8px;
  background: var(--panel); }
.tile .value { font-size: 1.35rem; font-variant-numeric: tabular-nums; }
.tile .label { color: var(--muted); font-size: .8rem; }
.grid { display: grid; gap: 1rem; grid-template-columns: repeat(auto-fill, minmax(19rem, 1fr)); }
.card { border: 1px solid var(--line); border-radius: 10px; background: var(--panel);
  overflow: hidden; }
.card .body { padding: .8rem .9rem 1rem; }
.card img { width: 100%; aspect-ratio: 4 / 3; object-fit: cover; display: block;
  background: var(--line); }
.meta { margin: 0 0 .5rem; color: var(--muted); font-size: .85rem; }
.price { margin: 0 0 .5rem; font-variant-numeric: tabular-nums; }
.chips { display: flex; flex-wrap: wrap; gap: .3rem; margin: 0 0 .6rem; padding: 0;
  list-style: none; }
.chip, .status {
  display: inline-block; padding: .1rem .45rem; border: 1px solid var(--line);
  border-radius: 999px; font-size: .75rem; color: var(--muted);
}
.status-open { color: var(--warn); border-color: var(--warn); }
.status-received, .status-good { color: var(--good); border-color: var(--good); }
table { width: 100%; border-collapse: collapse; font-size: .85rem; }
th, td { padding: .35rem .4rem; text-align: left; border-bottom: 1px solid var(--line);
  vertical-align: middle; }
th { color: var(--muted); font-weight: 600; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.variant img { width: 2.2rem; height: 2.2rem; border-radius: 4px; }
.variant td:first-child { width: 3rem; }
.variant-id { font-family: ui-monospace, Consolas, monospace; font-size: .75rem; }
.scroll { overflow-x: auto; }
footer { color: var(--muted); font-size: .8rem; }
"""


def _e(value: object) -> str:
    """Escape one value for HTML text and quoted attributes alike."""

    return html.escape(str(value), quote=True)


def _money(cents: int) -> str:
    """Format integer minor units.  No currency is implied; the schema is cents."""

    return f"{cents / 100:.2f}"


def _chip(value: object) -> str:
    return f'<li class="chip">{_e(value)}</li>'


def _normalise_port(port: int) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("web port must be an integer from 0 to 65535")
    return port


def build_demo_business(catalog: DemoCatalog) -> Business:
    """Drive the real domain path from the seed's received order and some sales.

    The domain is item-level while the seed's purchase-order lines are
    variant-level, so the received order's lines are folded into item
    quantities.  Only the received order posts stock, which leaves the open
    order in the catalogue genuinely outstanding.
    """

    business = Business()
    for item in catalog.items:
        business.register_item(item.item_id, item.sku, item.name, item.price_cents)

    received = next(
        order for order in catalog.purchase_orders if order.status == "received"
    )
    quantities: dict[str, int] = {}
    for line in received.lines:
        item_id = catalog.variant(line.variant_id).item_id
        quantities[item_id] = quantities.get(item_id, 0) + line.quantity
    order = business.create_purchase_order(
        received.supplier_id,
        sorted(quantities.items()),
        order_id=received.purchase_order_id,
    )
    business.receive_purchase_order(
        order.order_id, receipt_id=f"receipt-{order.order_id}"
    )

    for sale_id, customer_id, item_id, quantity in _DEMO_SALES:
        business.create_manual_sale(
            customer_id,
            [
                {
                    "item_id": item_id,
                    "quantity": quantity,
                    "unit_price_cents": business.get_item(item_id).price_cents,
                }
            ],
            sale_id=sale_id,
        )
    return business


def health_payload(business: Business) -> dict[str, object]:
    """Return the ``/api/health`` body.

    It reports the data mode as well as liveness, because a reader of this
    endpoint must not mistake demo fixture state for a real ERP database.
    """

    manifest = business.registry.manifest()
    capabilities = cast("list[str]", manifest["capabilities"])
    return {
        "app_id": manifest["app_id"],
        "status": "ok",
        "surface": "console",
        "connect_api": "separate process on port 4310",
        "data": "demo-fixture",
        "catalog_source": "db/seed.sql",
        "persistence": "in-memory",
        "capabilities": len(capabilities),
        "bind": DEFAULT_HOST,
    }


def _render_header() -> str:
    return (
        "<header>"
        f"<h1>{_e(TITLE)}</h1>"
        "<nav>"
        '<a href="/">console</a>'
        '<a href="/api/catalog">/api/catalog</a>'
        '<a href="/api/audit">/api/audit</a>'
        '<a href="/api/health">/api/health</a>'
        "</nav>"
        "</header>"
        '<p class="banner" role="status"><strong>Demo fixture, in memory.</strong> '
        "The catalogue mirrors the committed <code>db/seed.sql</code> example data, "
        "and the items, stock, sales, and journals are process-local "
        f"<code>{_e(__package__)}</code> state rebuilt on every start. Nothing is "
        "read from or written to a database, no connected peer is involved, and "
        "everything on this page is gone when the process exits. Variant "
        "availability is the seed's per-warehouse example stock; the domain stock "
        "in the activity panel is item-level and separate from it.</p>"
    )


def _render_tiles(catalog: DemoCatalog, audit: AuditSnapshot) -> str:
    on_hand = sum(variant.on_hand for variant in catalog.variants)
    reserved = sum(variant.reserved for variant in catalog.variants)
    tiles = (
        ("Products", len(catalog.items)),
        ("Variants", len(catalog.variants)),
        ("Distinct images", len(catalog.image_urls)),
        ("Units on hand", on_hand),
        ("Units reserved", reserved),
        ("Units available", on_hand - reserved),
        ("Item-level stock", sum(level.quantity for level in audit.stock)),
        ("Manual sales", len(audit.sales)),
        ("Sale revenue", _money(sum(sale.total_cents for sale in audit.sales))),
        ("Journal entries", len(audit.journals)),
        (
            "Open purchase orders",
            sum(1 for order in catalog.purchase_orders if order.status == "open"),
        ),
    )
    return (
        '<section class="tiles">'
        + "".join(
            f'<div class="tile"><div class="value">{_e(value)}</div>'
            f'<div class="label">{_e(label)}</div></div>'
            for label, value in tiles
        )
        + "</section>"
    )


def _render_item_card(item: DemoItem, catalog: DemoCatalog) -> str:
    chips = "".join(
        _chip(variant_id) for variant_id in dict.fromkeys(
            f"{variant.color} / {variant.size}" for variant in item.variants
        )
    )
    rows = "".join(
        '<tr class="variant">'
        f'<td><img src="{_e(variant.image_url)}" alt="{_e(variant.alt_text)}" '
        'loading="lazy" width="35" height="35"></td>'
        f'<td><span class="variant-id">{_e(variant.variant_id)}</span></td>'
        f'<td class="num">{_e(variant.available)}</td>'
        "</tr>"
        for variant in item.variants
    )
    return (
        '<article class="card">'
        f'<img src="{_e(item.variants[0].image_url)}" '
        f'alt="{_e(item.variants[0].alt_text)}" loading="lazy" width="480" height="360">'
        '<div class="body">'
        f"<h3>{_e(item.name)}</h3>"
        f'<p class="meta">{_e(item.brand)} &middot; {_e(item.category)} &middot; '
        f'{len(item.variants)} variants in {len(catalog.warehouses)} warehouses</p>'
        f'<p class="price"><strong>{_e(_money(item.price_cents))}</strong> '
        f'<span class="muted">per unit &middot; {_e(item.available)} available</span></p>'
        f'<ul class="chips">{chips}</ul>'
        f'<table><thead><tr><th>Variant</th><th class="num">Available</th></tr></thead>'
        f"<tbody>{rows}</tbody></table>"
        "</div></article>"
    )


def _render_catalog(catalog: DemoCatalog) -> str:
    return (
        "<section><h2>Catalog</h2>"
        '<p class="muted">One Unsplash placeholder image per variant, shared per '
        "colourway. The images are remote links with no committed files, licences, "
        "or attribution.</p>"
        f'<div class="grid">'
        + "".join(_render_item_card(item, catalog) for item in catalog.items)
        + "</div></section>"
    )


def _render_stock(catalog: DemoCatalog) -> str:
    rows = []
    for warehouse in catalog.warehouses:
        levels = [
            level
            for variant in catalog.variants
            for level in variant.stock
            if level.warehouse_id == warehouse.warehouse_id
        ]
        rows.append(
            "<tr>"
            f'<th scope="row">{_e(warehouse.code)} <span class="muted">'
            f"{_e(warehouse.city)}, {_e(warehouse.country_code)}</span></th>"
            f"<td>{_e(warehouse.name)}</td>"
            f'<td class="num">{len(levels)}</td>'
            f'<td class="num">{sum(level.on_hand for level in levels)}</td>'
            f'<td class="num">{sum(level.reserved for level in levels)}</td>'
            f'<td class="num">{sum(level.available for level in levels)}</td>'
            "</tr>"
        )
    return (
        '<section><h2>Stock by warehouse</h2><div class="scroll"><table>'
        '<thead><tr><th>Code</th><th>Warehouse</th><th class="num">Variants</th>'
        '<th class="num">On hand</th><th class="num">Reserved</th>'
        f'<th class="num">Available</th></tr></thead><tbody>{"".join(rows)}</tbody>'
        "</table></div></section>"
    )


def _render_purchasing(catalog: DemoCatalog) -> str:
    rows = []
    for order in catalog.purchase_orders:
        supplier = catalog.supplier(order.supplier_id)
        warehouse = catalog.warehouse(order.warehouse_id)
        rows.append(
            "<tr>"
            f"<td>{_e(order.po_number)}</td>"
            f'<td>{_e(supplier.name)} <span class="muted">{_e(supplier.code)}</span></td>'
            f"<td>{_e(warehouse.code)}</td>"
            f'<td><span class="status status-{_e(order.status)}">'
            f"{_e(order.status)}</span></td>"
            f'<td class="num">{len(order.lines)}</td>'
            f'<td class="num">{_money(order.total_cents)}</td>'
            f'<td class="muted">{_e(order.ordered_at)} &rarr; '
            f'{_e(order.expected_at or "-")}</td>'
            "</tr>"
        )
    return (
        '<section><h2>Purchasing</h2><div class="scroll"><table>'
        '<thead><tr><th>Order</th><th>Supplier</th><th>To</th><th>Status</th>'
        '<th class="num">Lines</th><th class="num">Value</th>'
        "<th>Ordered &rarr; expected</th></tr></thead>"
        f'<tbody>{"".join(rows)}</tbody></table></div></section>'
    )


def _balanced(journal: JournalEntry) -> bool:
    return journal.total_debits_cents == journal.total_credits_cents


def _render_activity(audit: AuditSnapshot) -> str:
    names = {item.item_id: item.name or item.sku for item in audit.items}

    def sale_lines(sale: Sale) -> str:
        return ", ".join(
            f"{names.get(line.item_id, line.item_id)} x{line.quantity}"
            for line in sale.lines
        )

    sales = "".join(
        "<tr>"
        f"<td>{_e(sale.sale_id)}</td>"
        f"<td>{_e(sale.customer_id)}</td>"
        f'<td class="muted">{_e(sale_lines(sale))}</td>'
        f'<td class="num">{_money(sale.total_cents)}</td>'
        f'<td class="muted">{_e(sale.journal_id)}</td>'
        "</tr>"
        for sale in audit.sales[-RECENT_ACTIVITY_LIMIT:]
    )
    journals = "".join(
        "<tr>"
        f"<td>{_e(journal.journal_id)}</td>"
        f'<td class="muted">{_e(journal.sale_id or "-")}</td>'
        f'<td class="num">{_money(journal.total_debits_cents)}</td>'
        f'<td class="num">{_money(journal.total_credits_cents)}</td>'
        f'<td><span class="status status-{"good" if _balanced(journal) else "open"}">'
        f'{"balanced" if _balanced(journal) else "unbalanced"}</span></td>'
        "</tr>"
        for journal in audit.journals[-RECENT_ACTIVITY_LIMIT:]
    )
    return (
        "<section><h2>Recent sales and journals</h2>"
        '<p class="muted">Item-level process-local state from '
        "<code>Business</code>, newest last. Every sale posts a cash debit and a "
        "revenue credit for the same total.</p>"
        '<h3>Manual sales</h3><div class="scroll"><table><thead><tr><th>Sale</th>'
        '<th>Customer</th><th>Lines</th><th class="num">Total</th>'
        f"<th>Journal</th></tr></thead><tbody>{sales}</tbody></table></div>"
        '<h3>General ledger</h3><div class="scroll"><table><thead><tr><th>Entry</th>'
        '<th>Sale</th><th class="num">Debits</th><th class="num">Credits</th>'
        f"<th>Check</th></tr></thead><tbody>{journals}</tbody></table></div>"
        "</section>"
    )


def render_console(business: Business, catalog: DemoCatalog) -> str:
    """Render the whole console as one HTML document."""

    audit = business.audit_snapshot()
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{_e(TITLE)}</title>\n"
        f"<style>\n{_STYLE}\n</style>\n"
        "</head>\n<body>\n"
        f"{_render_header()}\n"
        f"{_render_tiles(catalog, audit)}\n"
        f"{_render_catalog(catalog)}\n"
        f"{_render_stock(catalog)}\n"
        f"{_render_purchasing(catalog)}\n"
        f"{_render_activity(audit)}\n"
        "<footer>Read-only demo surface. Prices and values are integer counts of "
        "minor units (cents) rendered without a currency. JSON: "
        '<a href="/api/health">health</a>, <a href="/api/catalog">catalog</a>, '
        '<a href="/api/audit">audit</a>.</footer>\n'
        "</body>\n</html>\n"
    )


class _WebHTTPServer(HTTPServer):
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        business: Business,
        catalog: DemoCatalog,
    ) -> None:
        self.business = business
        self.catalog = catalog
        super().__init__(server_address, handler)


class WebHandler(BaseHTTPRequestHandler):
    """Serve the console page and its read-only JSON routes.

    The socket is bound to loopback, so every peer that can reach this handler
    is already local and no per-request peer check is needed.
    """

    server: Any

    server_version = "AtlasERPConsole/0.1"

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        path = self._path()
        try:
            if path == "/":
                self._send(
                    200,
                    render_console(self.server.business, self.server.catalog).encode(
                        "utf-8"
                    ),
                    HTML_CONTENT_TYPE,
                )
                return
            if path == "/api/health":
                self._send_json(200, health_payload(self.server.business))
                return
            if path == "/api/catalog":
                self._send_json(200, self.server.catalog.to_dict())
                return
            if path == "/api/audit":
                self._send_json(
                    200, self.server.business.audit_snapshot().to_dict()
                )
                return
        except (TypeError, ValueError):
            # Never leak an internal failure detail to the browser.
            self._server_error()
            return
        self._not_found()

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler API
        self.do_GET()

    def __getattr__(self, name: str) -> Any:
        # BaseHTTPRequestHandler otherwise answers a less common method with an
        # HTML 501.  Every route here is read-only, so anything else is a 405.
        if name.startswith("do_"):
            return self._method_not_allowed
        raise AttributeError(name)

    def log_message(self, format: str, *args: object) -> None:
        # Do not log request lines or headers, where a caller could place data.
        return

    def _path(self) -> str:
        try:
            return urlsplit(self.path).path
        except ValueError:
            return self.path.split("?", 1)[0]

    def _method_not_allowed(self) -> None:
        allowed = _ROUTE_METHODS.get(self._path())
        if allowed is None:
            self._not_found()
            return
        self._send_json(
            405, {"error": "method not allowed"}, headers={"Allow": allowed}
        )

    def _not_found(self) -> None:
        self._send_json(404, {"error": "not found"})

    def _server_error(self) -> None:
        if self._path().startswith("/api/"):
            self._send_json(500, {"error": "internal server error"})
            return
        body = (
            "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            f"<title>{_e(TITLE)}</title></head><body>"
            "<h1>The console could not render this page</h1>"
            "<p>The in-memory demo state could not be rendered. "
            '<a href="/">Try again</a>.</p></body></html>'
        ).encode("utf-8")
        self._send(500, body, HTML_CONTENT_TYPE)

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
        self._send(status, body, JSON_CONTENT_TYPE, headers=headers)

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        # The page is entirely inline except for the seed's image host, so no
        # script source and no external stylesheet can be introduced.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; img-src https://images.unsplash.com; "
            "style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'",
        )
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


class WebServer:
    """A loopback console server around injected demo state.

    ``business`` and ``catalog`` default to freshly built demo state, and a test
    can inject its own instead.  The business state is only read after
    construction, so the console needs no lock around it.  An omitted ``port``
    falls back to ``ATLAS_ERP_WEB_PORT`` and then to :data:`DEFAULT_PORT`.
    """

    def __init__(
        self,
        *,
        port: int | None = None,
        business: Business | None = None,
        catalog: DemoCatalog | None = None,
    ) -> None:
        self.catalog = build_demo_catalog() if catalog is None else catalog
        self.business = (
            build_demo_business(self.catalog) if business is None else business
        )
        self._port = _normalise_port(_environment_port() if port is None else port)
        self._server = _WebHTTPServer(
            (DEFAULT_HOST, self._port), WebHandler, self.business, self.catalog
        )
        self._thread: threading.Thread | None = None
        self._closed = False

    @property
    def host(self) -> str:
        return DEFAULT_HOST

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def address(self) -> tuple[str, int]:
        return (self.host, self.port)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> WebServer:
        """Start serving in a daemon thread and return immediately."""

        if self._closed:
            raise RuntimeError("web server is closed")
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(
                target=self.serve_forever, name="atlas-erp-web", daemon=True
            )
            self._thread.start()
        return self

    def serve_forever(self) -> None:
        """Serve requests until :meth:`stop` or process termination."""

        if self._closed:
            raise RuntimeError("web server is closed")
        self._server.serve_forever(poll_interval=0.1)

    def stop(self) -> None:
        """Stop serving and join a thread started by :meth:`start`."""

        thread = self._thread
        if thread is not None and thread.is_alive():
            if thread is not threading.current_thread():
                self._server.shutdown()
                thread.join(timeout=5)
                if thread.is_alive():
                    raise TimeoutError("web server did not stop")
        self._thread = None
        self._close()

    def close(self) -> None:
        """Close the listening socket; prefer :meth:`stop` for a running server."""

        if self._thread is not None and self._thread.is_alive():
            self.stop()
        else:
            self._close()

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._server.server_close()

    def __enter__(self) -> WebServer:
        return self.start()

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()


def _environment_port() -> int:
    raw = os.environ.get(PORT_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_PORT
    try:
        return _normalise_port(int(raw))
    except ValueError as exc:
        raise ValueError(f"{PORT_ENV} must be an integer from 0 to 65535") from exc


def main() -> int:
    try:
        server = WebServer()
    except (OSError, ValueError) as exc:
        print(f"could not start the Atlas ERP console: {exc}", file=sys.stderr)
        return 2
    print(
        f"Atlas ERP console on {server.url} (demo fixture, in memory)",
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
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "HTML_CONTENT_TYPE",
    "JSON_CONTENT_TYPE",
    "PORT_ENV",
    "TITLE",
    "WebHandler",
    "WebServer",
    "build_demo_business",
    "health_payload",
    "main",
    "render_console",
]
