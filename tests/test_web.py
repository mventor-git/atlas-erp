"""Tests for the loopback web console and its demo catalogue fixture."""

import io
import json
import re
import unittest
from html import escape
from html.parser import HTMLParser
from http.client import HTTPConnection
from pathlib import Path
from typing import Any
from unittest import mock

from atlas_erp.business import Business
from atlas_erp.demo_catalog import (
    SOURCE_URL,
    DemoCatalog,
    DemoItem,
    DemoVariant,
    build_demo_catalog,
)
from atlas_erp.web import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_THEME,
    PORT_ENV,
    THEMES,
    WebServer,
    build_demo_business,
    health_payload,
    main,
    normalise_theme,
    render_console,
)

SEED_PATH = Path(__file__).resolve().parents[1] / "db" / "seed.sql"
# The seed quotes every text value and leaves counts as bare integers.
_SEED_VALUE = re.compile(r"'([^']*)'|(\d+)")
# Every six-digit hex literal in the style block must be a contract token value,
# so a colour hardcoded in a rule fails instead of shadowing the token table.
_HEX = re.compile(r"#[0-9A-Fa-f]{6}\b")

# contract.md v1.1.1, token name -> light value, dark value.
TOKENS = {
    "--atlas-bg-canvas": ("#F7F2EB", "#41444B"),
    "--atlas-bg-surface": ("#EAE2D6", "#52575D"),
    "--atlas-fg-default": ("#2D0000", "#DFD8C8"),
    "--atlas-fg-muted": ("#6A2F2F", "#B7B3A9"),
    "--atlas-accent": ("#8B9A6E", "#CABFAB"),
    "--atlas-link": ("#2D0000", "#DFD8C8"),
    "--atlas-border-divider": ("#EEEEEE", "#52575D"),
    "--atlas-border-control": ("#757D6F", "#9AA394"),
    "--atlas-on-accent": ("#2D0000", "#41444B"),
    "--atlas-focus-ring": ("#2D0000", "#DFD8C8"),
    "--atlas-success-fg": ("#2A7C13", "#2D0000"),
    "--atlas-success-bg": ("#C7D3C0", "#C7D3C0"),
    "--atlas-warning-fg": ("#2D0000", "#2D0000"),
    "--atlas-warning-bg": ("#C8A96B", "#C8A96B"),
    "--atlas-danger-fg": ("#6D0808", "#2D0000"),
    "--atlas-danger-bg": ("#FFDADA", "#FFDADA"),
    "--atlas-info-fg": ("#2D0000", "#2D0000"),
    "--atlas-info-bg": ("#FBE6C2", "#FBE6C2"),
}
_TOKEN_VALUES = {value for pair in TOKENS.values() for value in pair}


def style_block(document: str) -> str:
    start = document.index("<style>")
    end = document.index("</style>")
    return document[start : end + len("</style>")]


def seed_rows(table: str) -> list[dict[str, str]]:
    """Return one ``INSERT`` block of db/seed.sql as column/value mappings.

    ``db/seed.sql`` is the source of truth for the demo catalogue, and nothing
    in the package reads those tables, so this is how a test proves the
    in-memory fixture has not drifted away from the committed rows.
    """

    match = re.search(
        rf"INSERT INTO {table} \((?P<columns>[^)]*)\) VALUES(?P<rows>.*?);",
        SEED_PATH.read_text(encoding="utf-8"),
        re.S,
    )
    if match is None:
        raise AssertionError(f"no seed rows for {table}")
    columns = [column.strip() for column in match["columns"].split(",")]
    # The upsert clause follows the value list and its parenthesised column
    # lists are not rows.
    values = match["rows"].split("\nON CONFLICT", 1)[0]
    return [
        dict(zip(columns, [text or number for text, number in _SEED_VALUE.findall(row)]))
        for row in re.findall(r"\(([^()]*)\)", values)
    ]


class _TagBalance(HTMLParser):
    """Record unbalanced tags and attributes so a bad page cannot pass unnoticed."""

    VOID = frozenset(
        {
            "area", "base", "br", "col", "embed", "hr", "img", "input",
            "link", "meta", "source", "track", "wbr",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.unbalanced: list[str] = []
        self.tags: list[str] = []
        self.attributes: list[str] = []
        self.images: list[dict[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        self.attributes.extend(name for name, _ in attrs)
        if tag == "img":
            self.images.append(dict(attrs))
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in self.VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.unbalanced.append(tag)
            return
        self.stack.pop()

    def close(self) -> None:  # type: ignore[override]
        super().close()
        if self.stack:
            self.unbalanced.extend(self.stack)


def parse_html(document: str) -> _TagBalance:
    parser = _TagBalance()
    parser.feed(document)
    parser.close()
    return parser


def hostile_catalog() -> DemoCatalog:
    """Return a one-row catalogue whose every text field tries to inject HTML."""

    variant = DemoVariant(
        variant_id='<img src=x onerror="alert(1)">',
        item_id="item-hostile",
        color='"><script>alert(1)</script>',
        size="<b>one</b>",
        image_url="https://images.unsplash.com/photo-1?a=1&b=2",
        alt_text='<script>alert("alt")</script>',
    )
    return DemoCatalog(
        items=(
            DemoItem(
                item_id="item-hostile",
                sku="<script>sku</script>",
                name='<script>alert("name")</script>',
                brand="A & B <Co>",
                category='<em>cat</em>',
                price_cents=100,
                variants=(variant,),
            ),
        ),
        warehouses=(),
        suppliers=(),
        purchase_orders=(),
    )


class WebConsoleRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = WebServer(port=0)
        self.server.start()
        self.addCleanup(self.server.close)

    def get(
        self, path: str, method: str = "GET"
    ) -> tuple[int, dict[str, str], bytes]:
        connection = HTTPConnection(*self.server.address, timeout=5)
        try:
            connection.request(method, path)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def json_get(self, path: str) -> Any:
        status, headers, body = self.get(path)
        self.assertEqual(status, 200, path)
        self.assertEqual(headers["Content-Type"], "application/json")
        return json.loads(body)

    def test_server_binds_loopback_on_an_ephemeral_port_and_stops(self) -> None:
        self.assertEqual(self.server.host, DEFAULT_HOST)
        self.assertNotEqual(self.server.port, 0)
        self.assertEqual(self.server.address, ("127.0.0.1", self.server.port))
        self.assertEqual(self.server.url, f"http://127.0.0.1:{self.server.port}")

        second = WebServer(port=0)
        self.addCleanup(second.close)
        second.start()
        self.assertNotEqual(second.port, self.server.port)

    def test_stop_releases_the_port_and_refuses_further_serves(self) -> None:
        stopped = WebServer(port=0).start()
        address = stopped.address
        self.assertEqual(self.json_get("/api/health")["status"], "ok")
        stopped.stop()
        with self.assertRaises(OSError):
            connection = HTTPConnection(*address, timeout=2)
            try:
                connection.request("GET", "/api/health")
                connection.getresponse()
            finally:
                connection.close()
        with self.assertRaises(RuntimeError):
            stopped.start()

    def test_port_environment_override(self) -> None:
        with mock.patch.dict("os.environ", {PORT_ENV: "4402"}):
            server = WebServer()
            self.addCleanup(server.close)
        self.assertEqual(server.port, 4402)
        with mock.patch.dict("os.environ", {PORT_ENV: "nope"}):
            with self.assertRaises(ValueError):
                WebServer()
        with mock.patch.dict("os.environ", {PORT_ENV: "70000"}):
            with self.assertRaises(ValueError):
                WebServer()

    def test_console_page_is_html_with_no_script_and_no_external_assets(self) -> None:
        status, headers, body = self.get("/")
        document = body.decode("utf-8")

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertTrue(document.startswith("<!doctype html>"))
        self.assertIn("<title>Atlas ERP console</title>", document)
        self.assertNotIn("<script", document)
        self.assertNotIn("<link", document)
        # Only the seed's image host may be referenced off-origin.
        for attribute in re.findall(r'(?:src|href)="(https?://[^"]+)"', document):
            self.assertTrue(attribute.startswith("https://images.unsplash.com/"), attribute)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("img-src https://images.unsplash.com", headers["Content-Security-Policy"])
        self.assertEqual(parse_html(document).unbalanced, [])

    def test_console_page_states_the_data_is_an_in_memory_fixture(self) -> None:
        document = self.get("/")[2].decode("utf-8")

        self.assertIn("Demo fixture, in memory.", document)
        self.assertIn("db/seed.sql", document)
        self.assertIn("Nothing is read from or written to a database", document)

    def test_catalog_api_mirrors_the_fixture(self) -> None:
        payload = self.json_get("/api/catalog")

        self.assertEqual(payload["source"], "db/seed.sql")
        self.assertEqual(payload["data"], "demo-fixture")
        self.assertEqual(
            payload,
            json.loads(json.dumps(build_demo_catalog().to_dict())),
        )
        self.assertEqual(len(payload["items"]), 6)
        self.assertEqual(len(payload["warehouses"]), 2)
        self.assertEqual(len(payload["suppliers"]), 2)
        self.assertEqual(len(payload["purchase_orders"]), 2)

    def test_health_api_reports_the_app_and_the_demo_data_mode(self) -> None:
        payload = self.json_get("/api/health")

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["app_id"], "atlas-erp")
        self.assertEqual(payload["data"], "demo-fixture")
        self.assertEqual(payload["persistence"], "in-memory")
        self.assertEqual(payload["bind"], DEFAULT_HOST)
        self.assertEqual(payload, health_payload(self.server.business))

    def test_audit_api_is_the_domain_snapshot_not_a_second_truth(self) -> None:
        payload = self.json_get("/api/audit")

        self.assertEqual(payload, self.server.business.audit_snapshot().to_dict())
        self.assertEqual(
            [item["item_id"] for item in payload["items"]],
            sorted(item["item_id"] for item in payload["items"]),
        )
        self.assertEqual(len(payload["items"]), 6)
        self.assertEqual(len(payload["sales"]), 3)
        self.assertEqual(len(payload["journals"]), len(payload["sales"]))
        for journal in payload["journals"]:
            self.assertEqual(journal["total_debits_cents"], journal["total_credits_cents"])

    def test_unknown_route_is_404_and_a_write_method_is_405(self) -> None:
        status, _, body = self.get("/nope")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body), {"error": "not found"})

        status, headers, body = self.get("/api/audit", method="POST")
        self.assertEqual(status, 405)
        self.assertEqual(headers["Allow"], "GET")
        self.assertEqual(json.loads(body), {"error": "method not allowed"})

    def test_head_returns_headers_without_a_body(self) -> None:
        status, headers, body = self.get("/", method="HEAD")

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertEqual(body, b"")

    def test_query_strings_and_fragments_do_not_break_routing(self) -> None:
        self.assertEqual(self.get("/?refresh=1")[0], 200)
        self.assertEqual(self.get("/api/health?probe=1")[0], 200)


class WebConsoleRenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = build_demo_catalog()
        self.business = build_demo_business(self.catalog)
        self.document = render_console(self.business, self.catalog)
        self.parsed = parse_html(self.document)

    def test_every_item_renders_name_brand_category_and_price(self) -> None:
        for item in self.catalog.items:
            self.assertIn(escape(item.name), self.document)
            self.assertIn(escape(item.brand), self.document)
            self.assertIn(escape(item.category), self.document)
            self.assertIn(f'>{item.price_cents / 100:.2f}<', self.document)

    def test_every_variant_renders_an_image_alt_and_available_quantity(self) -> None:
        images = self.parsed.images
        # One card image per item plus one thumbnail per variant.
        self.assertEqual(
            len(images), len(self.catalog.items) + len(self.catalog.variants)
        )
        for variant in self.catalog.variants:
            self.assertIn(variant.variant_id, self.document)
            self.assertIn(variant.image_url.replace("&", "&amp;"), self.document)
            self.assertIn(f'alt="{variant.alt_text}"', self.document)
        for image in images:
            self.assertTrue(str(image["src"]).startswith("https://images.unsplash.com/"))
            self.assertTrue(image["alt"])
            self.assertEqual(image["loading"], "lazy")

    def test_variant_chips_and_availability_come_from_the_fixture(self) -> None:
        first = self.catalog.items[0]
        self.assertIn(f'<li class="chip">Sand / XS</li>', self.document)
        self.assertIn(f"<td class=\"num\">{first.variants[0].available}</td>", self.document)
        self.assertIn(f"{first.available} available", self.document)

    def test_stock_purchasing_and_activity_sections_are_present(self) -> None:
        for warehouse in self.catalog.warehouses:
            self.assertIn(warehouse.name, self.document)
            self.assertIn(warehouse.code, self.document)
        for order in self.catalog.purchase_orders:
            self.assertIn(order.po_number, self.document)
            self.assertIn(f'class="status status-{order.status}"', self.document)
        for sale in self.business.audit_snapshot().sales:
            self.assertIn(sale.sale_id, self.document)
            self.assertIn(sale.customer_id, self.document)
        self.assertEqual(self.document.count("balanced</span>"), 3)
        self.assertNotIn("unbalanced", self.document)

    def test_document_has_one_banner_one_catalog_and_no_script(self) -> None:
        self.assertEqual(self.parsed.unbalanced, [])
        self.assertEqual(self.document.count('class="banner"'), 1)
        self.assertEqual(self.document.count("<h2>Catalog</h2>"), 1)
        self.assertNotIn("<script", self.document)


def token_block(document: str, theme: str) -> str:
    """Return the one ``:root[data-theme]`` block that declares a mode."""

    marker = f':root[data-theme="{theme}"] {{'
    start = document.index(marker)
    return document[start : document.index("}", start)]


def http_get(address: tuple[str, int], path: str, method: str = "GET") -> bytes:
    connection = HTTPConnection(*address, timeout=5)
    try:
        connection.request(method, path)
        response = connection.getresponse()
        body = response.read()
        if response.status != 200:
            raise AssertionError(f"{method} {path} answered {response.status}")
        return body
    finally:
        connection.close()


class ThemeTokenTests(unittest.TestCase):
    """The console resolves every colour to a shared token in both modes."""

    def setUp(self) -> None:
        self.server = WebServer(port=0)
        self.server.start()
        self.addCleanup(self.server.close)
        self.document = self.get("/?theme=light")
        self.dark_document = self.get("/?theme=dark")

    def get(self, path: str) -> str:
        return http_get(self.server.address, path).decode("utf-8")

    def test_light_mode_carries_the_light_token_values(self) -> None:
        self.assertIn('<html lang="en" data-theme="light">', self.document)

        block = token_block(self.document, "light")
        self.assertIn("color-scheme: light;", block)
        for name, (light, _dark) in TOKENS.items():
            self.assertIn(f"{name}: {light};", block)

    def test_dark_mode_carries_the_dark_token_values(self) -> None:
        self.assertIn('<html lang="en" data-theme="dark">', self.dark_document)

        block = token_block(self.dark_document, "dark")
        self.assertIn("color-scheme: dark;", block)
        for name, (_light, dark) in TOKENS.items():
            self.assertIn(f"{name}: {dark};", block)

    def test_every_token_is_declared_once_in_each_mode_block(self) -> None:
        for document in (self.document, self.dark_document):
            for theme in THEMES:
                names = re.findall(
                    r"(--atlas-[\w-]+)\s*:", token_block(document, theme)
                )
                self.assertEqual(len(names), len(set(names)))
                self.assertEqual(set(names), set(TOKENS))

    def test_no_colour_is_hardcoded_outside_the_token_blocks(self) -> None:
        for document in (self.document, self.dark_document):
            style = style_block(document)
            for colour in _HEX.findall(style):
                self.assertIn(colour, _TOKEN_VALUES, colour)
            # No leftover custom properties from the pre-token stylesheet.
            self.assertEqual(
                set(re.findall(r"(--[\w-]+)\s*:", style)) - set(TOKENS), set()
            )
            for old in ("--bg", "--panel", "--line", "--ink", "--muted", "--good", "--warn"):
                self.assertNotIn(f"var({old})", style)
                self.assertNotIn(f"{old}:", style)

    def test_the_rules_use_tokens_for_every_role_the_contract_names(self) -> None:
        style = style_block(self.dark_document)

        for expected in (
            "background: var(--atlas-bg-canvas); color: var(--atlas-fg-default);",
            "background: var(--atlas-bg-surface)",
            "color: var(--atlas-fg-muted)",
            "a { color: var(--atlas-link); text-decoration: underline; }",
            ":focus-visible { outline: 2px solid var(--atlas-focus-ring);",
            "border: 1px solid var(--atlas-border-divider)",
            "border: 1px solid var(--atlas-border-control)",
            "background: var(--atlas-accent); color: var(--atlas-on-accent);",
            "background: var(--atlas-warning-bg); color: var(--atlas-warning-fg);",
            "background: var(--atlas-danger-bg); color: var(--atlas-danger-fg);",
            "background: var(--atlas-info-bg); color: var(--atlas-info-fg);",
            "background: var(--atlas-success-bg); border: 1px solid var(--atlas-success-fg);",
            # The muted token is only ever painted on bg.canvas, and a neutral
            # status chip is filled with it, so the control border is too.
            ".muted { color: var(--atlas-fg-muted); }",
            ".card .muted, .card .meta, .card th, .tile .label "
            "{ color: var(--atlas-fg-default); }",
            ".status { background: var(--atlas-bg-canvas); color: var(--atlas-fg-default); }",
        ):
            self.assertIn(expected, style)

    def test_the_accent_is_never_body_text_and_the_success_pair_is_not_text(self) -> None:
        style = style_block(self.dark_document)

        # The accent is a background for the current-mode pill, and the light
        # success pair is 3.38:1, so it is drawn as a marker, not as status text.
        self.assertIn(
            ".theme-current", style
        )
        self.assertNotIn("color: var(--atlas-accent)", style)
        self.assertNotIn("color: var(--atlas-success-fg)", style)
        self.assertIn('class="status status-good"', self.dark_document)

    def test_default_and_unusable_themes_render_light(self) -> None:
        for path in ("/", "/?theme=", "/?theme=sepia", "/?theme=DARK", "/?theme=a%20b"):
            with self.subTest(path=path):
                self.assertIn(
                    '<html lang="en" data-theme="light">', self.get(path)
                )

    def test_the_repeated_theme_parameter_takes_the_last_value(self) -> None:
        self.assertIn(
            '<html lang="en" data-theme="dark">', self.get("/?theme=light&theme=dark")
        )

    def test_the_header_offers_the_other_mode(self) -> None:
        self.assertIn('href="/?theme=dark">dark mode</a>', self.document)
        self.assertIn('<span class="theme-current">light mode</span>', self.document)
        self.assertIn('href="/?theme=light">light mode</a>', self.dark_document)
        self.assertIn('<span class="theme-current">dark mode</span>', self.dark_document)

    def test_a_mode_switch_is_still_a_terse_plain_document(self) -> None:
        for document in (self.document, self.dark_document):
            self.assertNotIn("<script", document)
            self.assertNotIn("<link", document)
            self.assertEqual(parse_html(document).unbalanced, [])
            for attribute in re.findall(r'(?:src|href)="(https?://[^"]+)"', document):
                self.assertTrue(
                    attribute.startswith("https://images.unsplash.com/"), attribute
                )

    def test_normalise_theme_only_accepts_a_supplied_mode(self) -> None:
        self.assertEqual(THEMES, ("light", "dark"))
        self.assertEqual(DEFAULT_THEME, "light")
        for requested in THEMES:
            self.assertEqual(normalise_theme(requested), requested)
        for unusable in (None, "", "Dark", "dark ", "light dark", 1, object()):
            with self.subTest(requested=unusable):
                self.assertEqual(normalise_theme(unusable), DEFAULT_THEME)

    def test_render_console_theme_argument_is_optional(self) -> None:
        catalog = self.server.catalog
        business = self.server.business

        self.assertEqual(
            render_console(business, catalog),
            render_console(business, catalog, DEFAULT_THEME),
        )
        self.assertEqual(
            render_console(business, catalog, "dark"),
            render_console(business, catalog, theme="dark"),
        )
        self.assertIn(
            'data-theme="light"', render_console(business, catalog, "sepia")
        )


def _srgb(value: int) -> float:
    channel = value / 255
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def luminance(colour: str) -> float:
    red, green, blue = (int(colour[index : index + 2], 16) for index in (1, 3, 5))
    return 0.2126 * _srgb(red) + 0.7152 * _srgb(green) + 0.0722 * _srgb(blue)


def contrast(fg: str, bg: str) -> float:
    lighter, darker = sorted((luminance(fg), luminance(bg)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


# Every text pair the stylesheet actually paints, as (token, background) role
# names resolved through the token table, so the check follows the tokens rather
# than restating hex values.
PAINTED_TEXT = {
    "body on canvas": ("--atlas-fg-default", "--atlas-bg-canvas"),
    "body on surface": ("--atlas-fg-default", "--atlas-bg-surface"),
    "muted on canvas": ("--atlas-fg-muted", "--atlas-bg-canvas"),
    "link on canvas": ("--atlas-link", "--atlas-bg-canvas"),
    "on accent": ("--atlas-on-accent", "--atlas-accent"),
    "warning": ("--atlas-warning-fg", "--atlas-warning-bg"),
    "danger": ("--atlas-danger-fg", "--atlas-danger-bg"),
    "info chip": ("--atlas-info-fg", "--atlas-info-bg"),
}
# Borders and focus rings that carry meaning are non-text UI, so WCAG 1.4.11
# asks for 3:1.  ``border.divider`` and the chip's own border are decorative by
# contract and exempt: a divider only separates content, and a chip is already
# identified by its fill and its 15:1 label.
PAINTED_NON_TEXT = {
    "focus ring on canvas": ("--atlas-focus-ring", "--atlas-bg-canvas"),
    "status border on canvas": ("--atlas-border-control", "--atlas-bg-canvas"),
}


class ThemeContrastTests(unittest.TestCase):
    """WCAG AA for the pairs the console paints, in both modes."""

    def _ratios(self, mode: int) -> dict[str, float]:
        values = {name: pair[mode] for name, pair in TOKENS.items()}
        return {
            role: contrast(values[fg], values[bg])
            for roles in (PAINTED_TEXT, PAINTED_NON_TEXT)
            for role, (fg, bg) in roles.items()
        }

    def test_text_pairs_meet_aa_in_both_modes(self) -> None:
        for mode, name in enumerate(THEMES):
            for role, ratio in self._ratios(mode).items():
                threshold = 4.5 if role in PAINTED_TEXT else 3.0
                with self.subTest(mode=name, role=role):
                    self.assertGreaterEqual(
                        ratio, threshold, f"{name} {role} is {ratio:.2f}:1"
                    )

    def test_the_dark_muted_token_is_only_safe_on_the_canvas(self) -> None:
        # The contract derived fg.muted against bg.canvas.  In dark mode it clears
        # AA there and misses it on the surface, which is the whole reason the
        # stylesheet keeps the muted token off cards and tiles.
        values = {token: pair[THEMES.index("dark")] for token, pair in TOKENS.items()}

        self.assertGreaterEqual(
            contrast(values["--atlas-fg-muted"], values["--atlas-bg-canvas"]), 4.5
        )
        self.assertLess(
            contrast(values["--atlas-fg-muted"], values["--atlas-bg-surface"]), 4.5
        )


class HtmlEscapingTests(unittest.TestCase):
    """Every value on the page is escaped, so fixture text can never inject HTML."""

    def setUp(self) -> None:
        self.catalog = hostile_catalog()
        # An empty Business keeps the hostile row out of the item master, so
        # this test only exercises the catalogue values.
        self.document = render_console(Business(), self.catalog)
        self.parsed = parse_html(self.document)

    def test_no_injected_tag_or_attribute_survives_rendering(self) -> None:
        self.assertNotIn("<script", self.document)
        self.assertNotIn("<em>", self.document)
        self.assertNotIn("<b>", self.document)
        self.assertNotIn("onerror", self.parsed.attributes)
        self.assertNotIn("script", self.parsed.tags)
        self.assertNotIn("em", self.parsed.tags)
        self.assertEqual(self.parsed.unbalanced, [])
        for image in self.parsed.images:
            self.assertEqual(image["src"], "https://images.unsplash.com/photo-1?a=1&b=2")
            self.assertEqual(image["alt"], '<script>alert("alt")</script>')

    def test_the_hostile_text_is_escaped_rather_than_dropped(self) -> None:
        # html.escape renders the payload as inert text, so the reader still
        # sees the value instead of a silently sanitised page.
        self.assertIn("&lt;script&gt;alert(&quot;name&quot;)&lt;/script&gt;", self.document)
        self.assertIn("&lt;img src=x onerror=&quot;alert(1)&quot;&gt;", self.document)
        self.assertIn("A &amp; B &lt;Co&gt;", self.document)
        self.assertIn("&lt;em&gt;cat&lt;/em&gt;", self.document)

    def test_ampersands_in_image_urls_survive_the_attribute(self) -> None:
        sources = [str(image["src"]) for image in self.parsed.images]

        self.assertEqual(len(sources), 2)
        for source in sources:
            self.assertNotIn("&amp;", source)

    def test_json_preserves_the_exact_value_and_is_served_as_json(self) -> None:
        # JSON is not HTML, so it must not be escaped; the protection is the
        # declared content type plus nosniff, which this test also pins.
        payload = json.loads(json.dumps(self.catalog.to_dict()))

        self.assertEqual(payload["items"][0]["name"], '<script>alert("name")</script>')
        server = WebServer(port=0, business=Business(), catalog=self.catalog)
        self.addCleanup(server.close)
        server.start()
        connection = HTTPConnection(*server.address, timeout=5)
        try:
            connection.request("GET", "/api/catalog")
            response = connection.getresponse()
            self.assertEqual(response.getheader("Content-Type"), "application/json")
            self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")
            served = json.loads(response.read())
        finally:
            connection.close()
        self.assertEqual(
            served["items"][0]["name"], '<script>alert("name")</script>'
        )


class DemoCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = build_demo_catalog()

    def test_shape_matches_the_committed_example_data(self) -> None:
        self.assertEqual(len(self.catalog.items), 6)
        self.assertEqual(len(self.catalog.variants), 37)
        self.assertEqual(len(self.catalog.image_urls), 13)
        self.assertEqual(len(self.catalog.warehouses), 2)
        self.assertEqual(len(self.catalog.suppliers), 2)
        self.assertEqual(
            sum(len(variant.stock) for variant in self.catalog.variants), 51
        )
        self.assertEqual(
            sum(len(order.lines) for order in self.catalog.purchase_orders), 10
        )

    def test_every_variant_has_exactly_one_unsplash_image(self) -> None:
        for variant in self.catalog.variants:
            self.assertTrue(variant.image_url.startswith("https://images.unsplash.com/"))
            self.assertTrue(variant.alt_text.strip())
            self.assertEqual(SOURCE_URL, "https://unsplash.com/")
            self.assertTrue(variant.stock)

    def test_variant_identity_is_derived_from_the_item(self) -> None:
        for item in self.catalog.items:
            for variant in item.variants:
                self.assertEqual(variant.item_id, item.item_id)
                self.assertTrue(variant.variant_id.startswith(f"{item.sku}-"))
        self.assertEqual(len({v.variant_id for v in self.catalog.variants}), 37)

    def test_available_never_exceeds_on_hand(self) -> None:
        for variant in self.catalog.variants:
            for level in variant.stock:
                self.assertLessEqual(level.reserved, level.on_hand)
                self.assertEqual(level.available, level.on_hand - level.reserved)
            self.assertEqual(variant.available, variant.on_hand - variant.reserved)

    def test_unknown_lookups_are_explicit(self) -> None:
        with self.assertRaises(LookupError):
            self.catalog.item("item-missing")
        with self.assertRaises(LookupError):
            self.catalog.variant("MISSING-XS")
        with self.assertRaises(LookupError):
            self.catalog.warehouse("wh-missing")
        with self.assertRaises(LookupError):
            self.catalog.supplier("sup-missing")


class SeedParityTests(unittest.TestCase):
    """db/seed.sql is the source of truth; the fixture must not drift from it."""

    def setUp(self) -> None:
        self.catalog = build_demo_catalog()

    def test_items_match_the_seed(self) -> None:
        seeded = {row["item_id"]: row for row in seed_rows("example_items")}

        self.assertEqual(set(seeded), {item.item_id for item in self.catalog.items})
        for item in self.catalog.items:
            row = seeded[item.item_id]
            self.assertEqual(
                (
                    row["sku"],
                    row["name"],
                    row["brand"],
                    row["category"],
                    int(row["price_cents"]),
                ),
                (item.sku, item.name, item.brand, item.category, item.price_cents),
            )

    def test_variants_and_images_match_the_seed(self) -> None:
        variants = {row["variant_id"]: row for row in seed_rows("example_item_variants")}
        images = {row["variant_id"]: row for row in seed_rows("example_variant_images")}

        self.assertEqual(set(variants), {v.variant_id for v in self.catalog.variants})
        self.assertEqual(set(images), set(variants))
        for variant in self.catalog.variants:
            row = variants[variant.variant_id]
            self.assertEqual(
                (row["item_id"], row["color"], row["size"], row["sku"]),
                (variant.item_id, variant.color, variant.size, variant.variant_id),
            )
            self.assertEqual(
                (images[variant.variant_id]["image_url"], images[variant.variant_id]["alt_text"]),
                (variant.image_url, variant.alt_text),
            )

    def test_stock_levels_match_the_seed(self) -> None:
        seeded: dict[str, dict[str, tuple[int, int]]] = {}
        for row in seed_rows("example_stock_levels"):
            seeded.setdefault(row["variant_id"], {})[row["warehouse_id"]] = (
                int(row["on_hand"]),
                int(row["reserved"]),
            )

        self.assertEqual(
            seeded,
            {
                variant.variant_id: {
                    level.warehouse_id: (level.on_hand, level.reserved)
                    for level in variant.stock
                }
                for variant in self.catalog.variants
            },
        )

    def test_warehouses_suppliers_and_purchase_orders_match_the_seed(self) -> None:
        warehouses = {row["warehouse_id"]: row for row in seed_rows("example_warehouses")}
        suppliers = {row["supplier_id"]: row for row in seed_rows("example_suppliers")}
        orders = {
            row["purchase_order_id"]: row
            for row in seed_rows("example_purchase_orders")
        }

        self.assertEqual(set(warehouses), {w.warehouse_id for w in self.catalog.warehouses})
        self.assertEqual(set(suppliers), {s.supplier_id for s in self.catalog.suppliers})
        self.assertEqual(set(orders), {o.purchase_order_id for o in self.catalog.purchase_orders})
        for warehouse in self.catalog.warehouses:
            row = warehouses[warehouse.warehouse_id]
            self.assertEqual(
                (row["code"], row["name"], row["city"], row["country_code"]),
                (warehouse.code, warehouse.name, warehouse.city, warehouse.country_code),
            )
        for supplier in self.catalog.suppliers:
            row = suppliers[supplier.supplier_id]
            self.assertEqual(
                (row["code"], row["name"], row["contact_email"], int(row["lead_time_days"])),
                (
                    supplier.code,
                    supplier.name,
                    supplier.contact_email,
                    supplier.lead_time_days,
                ),
            )
        for order in self.catalog.purchase_orders:
            row = orders[order.purchase_order_id]
            self.assertEqual(
                (
                    row["po_number"],
                    row["supplier_id"],
                    row["warehouse_id"],
                    row["status"],
                    row["ordered_at"],
                    row["expected_at"],
                ),
                (
                    order.po_number,
                    order.supplier_id,
                    order.warehouse_id,
                    order.status,
                    order.ordered_at,
                    order.expected_at,
                ),
            )

    def test_purchase_order_lines_match_the_seed(self) -> None:
        seeded = sorted(
            (
                row["purchase_order_id"],
                int(row["line_no"]),
                row["variant_id"],
                int(row["quantity"]),
                int(row["unit_cost_cents"]),
            )
            for row in seed_rows("example_purchase_order_lines")
        )

        self.assertEqual(
            seeded,
            sorted(
                (
                    order.purchase_order_id,
                    line.line_no,
                    line.variant_id,
                    line.quantity,
                    line.unit_cost_cents,
                )
                for order in self.catalog.purchase_orders
                for line in order.lines
            ),
        )


class DemoBusinessTests(unittest.TestCase):
    """The console's business state is real domain state, not a hand-built dict."""

    def setUp(self) -> None:
        self.catalog = build_demo_catalog()
        self.business = build_demo_business(self.catalog)

    def test_items_come_from_the_catalogue_at_their_own_prices(self) -> None:
        audit = self.business.audit_snapshot()

        self.assertEqual(len(audit.items), 6)
        for item in self.catalog.items:
            registered = self.business.get_item(item.item_id)
            self.assertEqual(registered.sku, item.sku)
            self.assertEqual(registered.price_cents, item.price_cents)

    def test_the_received_order_posts_stock_and_the_open_one_does_not(self) -> None:
        received = next(
            order for order in self.catalog.purchase_orders if order.status == "received"
        )
        order = self.business.get_purchase_order(received.purchase_order_id)

        self.assertTrue(order.is_received)
        self.assertEqual(order.supplier_id, received.supplier_id)
        open_order = next(
            order
            for order in self.catalog.purchase_orders
            if order.status == "open"
        )
        with self.assertRaises(Exception):
            self.business.get_purchase_order(open_order.purchase_order_id)

    def test_sales_decrement_stock_and_post_balanced_journals(self) -> None:
        audit = self.business.audit_snapshot()

        self.assertEqual(len(audit.sales), 3)
        for sale in audit.sales:
            journal = self.business.get_journal(sale.journal_id)
            self.assertEqual(journal.sale_id, sale.sale_id)
            self.assertEqual(journal.total_debits_cents, sale.total_cents)
            self.assertEqual(journal.total_credits_cents, sale.total_cents)
        for level in audit.stock:
            self.assertGreaterEqual(level.quantity, 0)
        self.assertEqual(
            sum(level.quantity for level in audit.stock),
            sum(
                business_level.quantity
                for business_level in self.business.audit_snapshot().stock
            ),
        )

    def test_the_fixture_is_deterministic(self) -> None:
        other = build_demo_business(build_demo_catalog())

        self.assertEqual(
            other.audit_snapshot().to_dict(), self.business.audit_snapshot().to_dict()
        )

    def test_it_never_touches_a_database(self) -> None:
        catalog_module = __import__(
            "atlas_erp.demo_catalog", fromlist=["build_demo_catalog"]
        )
        source = (Path(catalog_module.__file__)).read_text(encoding="utf-8")
        web_source = (
            Path(__import__("atlas_erp.web", fromlist=["WebServer"]).__file__)
        ).read_text(encoding="utf-8")

        for text in (source, web_source):
            self.assertNotIn("psycopg", text)
            self.assertNotIn("sqlite", text.lower())


class EntrypointTests(unittest.TestCase):
    """`python -m atlas_erp.web` boots, prints its URL, and stops cleanly."""

    def test_main_serves_until_interrupt_then_returns_zero(self) -> None:
        with mock.patch.dict("os.environ", {PORT_ENV: "0"}), mock.patch.object(
            WebServer, "serve_forever", side_effect=KeyboardInterrupt
        ), mock.patch("builtins.print") as printed:
            self.assertEqual(main(), 0)

        announced = " ".join(str(call) for call in printed.call_args_list)
        self.assertIn("Atlas ERP console on http://127.0.0.1:", announced)
        self.assertIn("demo fixture, in memory", announced)

    def test_main_refuses_an_unusable_port(self) -> None:
        with mock.patch.dict("os.environ", {PORT_ENV: "not-a-port"}), mock.patch(
            "sys.stderr", io.StringIO()
        ) as stderr:
            self.assertEqual(main(), 2)

        self.assertIn(PORT_ENV, stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
