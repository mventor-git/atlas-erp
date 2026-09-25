"""Demo catalogue fixture derived from the committed ``db/seed.sql``.

``db/seed.sql`` is the committed example catalogue and the source of truth for
everything here: six fictional products, 37 colour/size variants, one Unsplash
image per variant, per-warehouse stock, two warehouses, two suppliers, and two
purchase orders.  Nothing in the package reads those ``example_*`` tables, so
this module mirrors the committed rows in memory for the console to render.

This is demo fixture data, not production masterdata.  It stays in step with
the seed in the same commit, and ``tests/test_web.py`` fails when the two drift.

The seed derives a variant identity from its item rather than inventing one per
row, so this module does the same: ``variant_id`` is the item ``sku`` plus the
uppercased colour and size with non-alphanumerics dropped.  The seed's variant
``sku`` column is the same string, which ``db/schema.sql`` enforces with
``CHECK (sku = variant_id)``, so it is not repeated here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

IMAGE_URL_TEMPLATE = (
    "https://images.unsplash.com/{photo_id}?auto=format&fit=crop&w=1200&q=80"
)
SOURCE_URL = "https://unsplash.com/"


@dataclass(frozen=True)
class DemoStockLevel:
    """One variant's on-hand and reserved units in one warehouse."""

    warehouse_id: str
    on_hand: int
    reserved: int

    @property
    def available(self) -> int:
        return self.on_hand - self.reserved


@dataclass(frozen=True)
class DemoVariant:
    """One colour/size variant with its single image and stock levels."""

    variant_id: str
    item_id: str
    color: str
    size: str
    image_url: str
    alt_text: str
    stock: tuple[DemoStockLevel, ...] = ()

    @property
    def on_hand(self) -> int:
        return sum(level.on_hand for level in self.stock)

    @property
    def reserved(self) -> int:
        return sum(level.reserved for level in self.stock)

    @property
    def available(self) -> int:
        return self.on_hand - self.reserved

    def level_for(self, warehouse_id: str) -> DemoStockLevel | None:
        for level in self.stock:
            if level.warehouse_id == warehouse_id:
                return level
        return None


@dataclass(frozen=True)
class DemoItem:
    """One product with its full cartesian product of colour/size variants."""

    item_id: str
    sku: str
    name: str
    brand: str
    category: str
    price_cents: int
    variants: tuple[DemoVariant, ...] = ()

    @property
    def available(self) -> int:
        return sum(variant.available for variant in self.variants)


@dataclass(frozen=True)
class DemoWarehouse:
    warehouse_id: str
    code: str
    name: str
    city: str
    country_code: str


@dataclass(frozen=True)
class DemoSupplier:
    supplier_id: str
    code: str
    name: str
    contact_email: str
    lead_time_days: int


@dataclass(frozen=True)
class DemoPurchaseOrderLine:
    line_no: int
    variant_id: str
    quantity: int
    unit_cost_cents: int

    @property
    def total_cents(self) -> int:
        return self.quantity * self.unit_cost_cents


@dataclass(frozen=True)
class DemoPurchaseOrder:
    purchase_order_id: str
    po_number: str
    supplier_id: str
    warehouse_id: str
    status: str
    ordered_at: str
    expected_at: str | None
    lines: tuple[DemoPurchaseOrderLine, ...] = ()

    @property
    def total_cents(self) -> int:
        return sum(line.total_cents for line in self.lines)


# item_id, sku, name, brand, category, price_cents
_ITEMS: tuple[tuple[str, str, str, str, str, int], ...] = (
    (
        "item-aurora-linen-shirt",
        "AUR-LIN-SHIRT",
        "Aurora Everyday Linen Shirt",
        "Northline",
        "Apparel",
        8900,
    ),
    (
        "item-meridian-court-sneaker",
        "MER-COURT-SNEAK",
        "Meridian Court Sneaker",
        "Meridian",
        "Footwear",
        12900,
    ),
    (
        "item-harbor-wool-throw",
        "HAR-WOOL-THROW",
        "Harbor Wool Throw",
        "Hearthline",
        "Home",
        11900,
    ),
    (
        "item-terra-pour-over-set",
        "TER-POUR-01",
        "Terra Pour-Over Set",
        "Kiln & Coast",
        "Kitchen",
        6400,
    ),
    (
        "item-solstice-trail-bottle",
        "SOL-TRAIL-BTL",
        "Solstice Trail Bottle",
        "Fieldcraft",
        "Outdoors",
        3200,
    ),
    (
        "item-loom-everyday-tote",
        "LOOM-TOTE-01",
        "Loom Everyday Tote",
        "Loom",
        "Bags",
        7400,
    ),
)

# One Unsplash photo per item and colourway, shared by that colour's sizes.
# The seed shares one photo per colourway, which is its 13 distinct images.
_IMAGES: dict[tuple[str, str], str] = {
    ("item-aurora-linen-shirt", "Sand"): "photo-1521572163474-6864f9cf17ab",
    ("item-aurora-linen-shirt", "Navy"): "photo-1596755094514-f87e34085b2c",
    ("item-aurora-linen-shirt", "Olive"): "photo-1602810318383-e386cc2a3ccf",
    ("item-meridian-court-sneaker", "Chalk"): "photo-1595950653106-6c9ebd614d3a",
    ("item-meridian-court-sneaker", "Ink"): "photo-1542291026-7eec264c27ff",
    ("item-harbor-wool-throw", "Fog"): "photo-1519710164239-da123dc03ef4",
    ("item-harbor-wool-throw", "Rust"): "photo-1616486338812-3dadae4b4ace",
    ("item-terra-pour-over-set", "Sand"): "photo-1514432324607-a09d9b4aefdd",
    ("item-terra-pour-over-set", "Clay"): "photo-1447933601403-0c6688de566e",
    ("item-solstice-trail-bottle", "Glacier"): "photo-1602143407151-7111542de6e8",
    ("item-solstice-trail-bottle", "Ember"): "photo-1523362628745-0c100150b504",
    ("item-loom-everyday-tote", "Ink"): "photo-1594223274512-ad4803739b7c",
    ("item-loom-everyday-tote", "Clay"): "photo-1590874103328-eac38a683ce7",
}

_COLOURS_AND_SIZES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "item-aurora-linen-shirt": (
        ("Sand", "Navy", "Olive"),
        ("XS", "S", "M", "L", "XL"),
    ),
    "item-meridian-court-sneaker": (
        ("Chalk", "Ink"),
        ("40", "41", "42", "43", "44", "45"),
    ),
    "item-harbor-wool-throw": (("Fog", "Rust"), ("One Size",)),
    "item-terra-pour-over-set": (("Sand", "Clay"), ("1L",)),
    "item-solstice-trail-bottle": (("Glacier", "Ember"), ("500ml", "750ml")),
    "item-loom-everyday-tote": (("Ink", "Clay"), ("One Size",)),
}

# variant_id -> {warehouse_id: (on_hand, reserved)}
_STOCK: dict[str, dict[str, tuple[int, int]]] = {
    "AUR-LIN-SHIRT-SAND-XS": {"wh-ams": (34, 4)},
    "AUR-LIN-SHIRT-SAND-S": {"wh-ams": (61, 7)},
    "AUR-LIN-SHIRT-SAND-M": {"wh-ams": (88, 11), "wh-ldn": (24, 3)},
    "AUR-LIN-SHIRT-SAND-L": {"wh-ams": (79, 9), "wh-ldn": (20, 2)},
    "AUR-LIN-SHIRT-SAND-XL": {"wh-ams": (47, 5)},
    "AUR-LIN-SHIRT-NAVY-XS": {"wh-ams": (28, 3)},
    "AUR-LIN-SHIRT-NAVY-S": {"wh-ams": (55, 6)},
    "AUR-LIN-SHIRT-NAVY-M": {"wh-ams": (74, 9), "wh-ldn": (18, 2)},
    "AUR-LIN-SHIRT-NAVY-L": {"wh-ams": (66, 8), "wh-ldn": (15, 2)},
    "AUR-LIN-SHIRT-NAVY-XL": {"wh-ams": (38, 4)},
    "AUR-LIN-SHIRT-OLIVE-XS": {"wh-ams": (12, 1)},
    "AUR-LIN-SHIRT-OLIVE-S": {"wh-ams": (24, 2)},
    "AUR-LIN-SHIRT-OLIVE-M": {"wh-ams": (33, 4), "wh-ldn": (7, 1)},
    "AUR-LIN-SHIRT-OLIVE-L": {"wh-ams": (21, 2), "wh-ldn": (5, 0)},
    "AUR-LIN-SHIRT-OLIVE-XL": {"wh-ams": (9, 1)},
    "MER-COURT-SNEAK-CHALK-40": {"wh-ams": (22, 2)},
    "MER-COURT-SNEAK-CHALK-41": {"wh-ams": (31, 3)},
    "MER-COURT-SNEAK-CHALK-42": {"wh-ams": (38, 5), "wh-ldn": (12, 1)},
    "MER-COURT-SNEAK-CHALK-43": {"wh-ams": (35, 4), "wh-ldn": (9, 1)},
    "MER-COURT-SNEAK-CHALK-44": {"wh-ams": (26, 3), "wh-ldn": (6, 1)},
    "MER-COURT-SNEAK-CHALK-45": {"wh-ams": (15, 1)},
    "MER-COURT-SNEAK-INK-40": {"wh-ams": (9, 1)},
    "MER-COURT-SNEAK-INK-41": {"wh-ams": (14, 1)},
    "MER-COURT-SNEAK-INK-42": {"wh-ams": (17, 2)},
    "MER-COURT-SNEAK-INK-43": {"wh-ams": (12, 1)},
    "MER-COURT-SNEAK-INK-44": {"wh-ams": (8, 1)},
    "MER-COURT-SNEAK-INK-45": {"wh-ams": (4, 0)},
    "HAR-WOOL-THROW-FOG-ONESIZE": {"wh-ams": (44, 5), "wh-ldn": (11, 1)},
    "HAR-WOOL-THROW-RUST-ONESIZE": {"wh-ams": (19, 2)},
    "TER-POUR-01-SAND-1L": {"wh-ams": (52, 6), "wh-ldn": (16, 2)},
    "TER-POUR-01-CLAY-1L": {"wh-ams": (27, 3)},
    "SOL-TRAIL-BTL-GLACIER-500ML": {"wh-ams": (120, 14), "wh-ldn": (48, 6)},
    "SOL-TRAIL-BTL-GLACIER-750ML": {"wh-ams": (58, 7), "wh-ldn": (22, 3)},
    "SOL-TRAIL-BTL-EMBER-500ML": {"wh-ams": (64, 8)},
    "SOL-TRAIL-BTL-EMBER-750ML": {"wh-ams": (31, 4)},
    "LOOM-TOTE-01-INK-ONESIZE": {"wh-ams": (57, 6), "wh-ldn": (18, 2)},
    "LOOM-TOTE-01-CLAY-ONESIZE": {"wh-ams": (23, 2)},
}

_WAREHOUSES: tuple[DemoWarehouse, ...] = (
    DemoWarehouse("wh-ams", "AMS", "Amsterdam Fulfilment Centre", "Amsterdam", "NL"),
    DemoWarehouse("wh-ldn", "LDN", "London Fulfilment Centre", "London", "GB"),
)

_SUPPLIERS: tuple[DemoSupplier, ...] = (
    DemoSupplier(
        "sup-hollowbay",
        "HOLLOWBAY",
        "Hollow Bay Textile Mill",
        "orders@hollowbay-mills.example",
        21,
    ),
    DemoSupplier(
        "sup-northport",
        "NORTHPORT",
        "Northport Supply Group",
        "purchasing@northport-supply.example",
        14,
    ),
)

# purchase_order_id, po_number, supplier_id, warehouse_id, status, ordered_at,
# expected_at, lines as (line_no, variant_id, quantity, unit_cost_cents)
_PURCHASE_ORDERS: tuple[tuple[str, str, str, str, str, str, str, tuple], ...] = (
    (
        "po-2026-0041",
        "PO-2026-0041",
        "sup-hollowbay",
        "wh-ams",
        "received",
        "2026-08-18",
        "2026-08-28",
        (
            (1, "AUR-LIN-SHIRT-SAND-M", 120, 4100),
            (2, "AUR-LIN-SHIRT-NAVY-L", 80, 4100),
            (3, "AUR-LIN-SHIRT-OLIVE-S", 30, 4100),
            (4, "HAR-WOOL-THROW-FOG-ONESIZE", 60, 5900),
            (5, "HAR-WOOL-THROW-RUST-ONESIZE", 24, 5900),
        ),
    ),
    (
        "po-2026-0058",
        "PO-2026-0058",
        "sup-northport",
        "wh-ldn",
        "open",
        "2026-09-08",
        "2026-09-22",
        (
            (1, "MER-COURT-SNEAK-CHALK-42", 40, 6400),
            (2, "MER-COURT-SNEAK-INK-43", 16, 6400),
            (3, "TER-POUR-01-SAND-1L", 48, 2700),
            (4, "SOL-TRAIL-BTL-GLACIER-500ML", 150, 1350),
            (5, "LOOM-TOTE-01-INK-ONESIZE", 60, 3100),
        ),
    ),
)

# The seed's alt text spells out only the sizes that are not plain labels.
_ALT_SIZE_TEXT = {
    "ONESIZE": "one size",
    "1L": "1 litre",
    "500ML": "500 ml",
    "750ML": "750 ml",
}


class UnknownDemoRowError(LookupError):
    """Raised when a demo lookup refers to a row the fixture does not carry."""


def _identity_token(value: str) -> str:
    """Return the seed's identity token: non-alphanumerics dropped, uppercased."""

    return re.sub(r"[^A-Za-z0-9]", "", value).upper()


def _alt_text(name: str, color: str, size_token: str) -> str:
    return f"{name} in {color}, {_ALT_SIZE_TEXT.get(size_token, f'size {size_token}')}"


def _build_variants(item_id: str, sku: str, name: str) -> tuple[DemoVariant, ...]:
    colors, sizes = _COLOURS_AND_SIZES[item_id]
    variants = []
    for color in colors:
        for size in sizes:
            variant_id = f"{sku}-{_identity_token(color)}-{_identity_token(size)}"
            variants.append(
                DemoVariant(
                    variant_id=variant_id,
                    item_id=item_id,
                    color=color,
                    size=size,
                    image_url=IMAGE_URL_TEMPLATE.format(
                        photo_id=_IMAGES[(item_id, color)]
                    ),
                    alt_text=_alt_text(name, color, _identity_token(size)),
                    stock=tuple(
                        DemoStockLevel(warehouse_id, on_hand, reserved)
                        for warehouse_id, (on_hand, reserved) in _STOCK[
                            variant_id
                        ].items()
                    ),
                )
            )
    return tuple(variants)


class DemoCatalog:
    """An immutable in-memory mirror of the committed example catalogue."""

    def __init__(
        self,
        items: tuple[DemoItem, ...],
        warehouses: tuple[DemoWarehouse, ...],
        suppliers: tuple[DemoSupplier, ...],
        purchase_orders: tuple[DemoPurchaseOrder, ...],
    ) -> None:
        self.items = items
        self.warehouses = warehouses
        self.suppliers = suppliers
        self.purchase_orders = purchase_orders
        self._items_by_id = {item.item_id: item for item in items}
        self._variants = tuple(
            variant for item in items for variant in item.variants
        )
        self._variants_by_id = {variant.variant_id: variant for variant in self._variants}
        if len(self._variants_by_id) != len(self._variants):
            raise ValueError("duplicate variant identity in the demo catalogue")

    @property
    def variants(self) -> tuple[DemoVariant, ...]:
        return self._variants

    @property
    def image_urls(self) -> tuple[str, ...]:
        """Return the distinct image URLs in use across every variant."""

        return tuple(dict.fromkeys(variant.image_url for variant in self._variants))

    def item(self, item_id: str) -> DemoItem:
        try:
            return self._items_by_id[item_id]
        except KeyError as exc:
            raise UnknownDemoRowError(f"unknown demo item: {item_id}") from exc

    def variant(self, variant_id: str) -> DemoVariant:
        try:
            return self._variants_by_id[variant_id]
        except KeyError as exc:
            raise UnknownDemoRowError(f"unknown demo variant: {variant_id}") from exc

    def warehouse(self, warehouse_id: str) -> DemoWarehouse:
        for warehouse in self.warehouses:
            if warehouse.warehouse_id == warehouse_id:
                return warehouse
        raise UnknownDemoRowError(f"unknown demo warehouse: {warehouse_id}")

    def supplier(self, supplier_id: str) -> DemoSupplier:
        for supplier in self.suppliers:
            if supplier.supplier_id == supplier_id:
                return supplier
        raise UnknownDemoRowError(f"unknown demo supplier: {supplier_id}")

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-friendly mirror of the fixture for ``/api/catalog``."""

        return {
            "source": "db/seed.sql",
            "data": "demo-fixture",
            "items": [
                {
                    "item_id": item.item_id,
                    "sku": item.sku,
                    "name": item.name,
                    "brand": item.brand,
                    "category": item.category,
                    "price_cents": item.price_cents,
                    "available": item.available,
                    "variants": [
                        {
                            "variant_id": variant.variant_id,
                            "color": variant.color,
                            "size": variant.size,
                            "image_url": variant.image_url,
                            "source_url": SOURCE_URL,
                            "alt_text": variant.alt_text,
                            "on_hand": variant.on_hand,
                            "reserved": variant.reserved,
                            "available": variant.available,
                        }
                        for variant in item.variants
                    ],
                }
                for item in self.items
            ],
            "warehouses": [
                {
                    "warehouse_id": warehouse.warehouse_id,
                    "code": warehouse.code,
                    "name": warehouse.name,
                    "city": warehouse.city,
                    "country_code": warehouse.country_code,
                }
                for warehouse in self.warehouses
            ],
            "suppliers": [
                {
                    "supplier_id": supplier.supplier_id,
                    "code": supplier.code,
                    "name": supplier.name,
                    "contact_email": supplier.contact_email,
                    "lead_time_days": supplier.lead_time_days,
                }
                for supplier in self.suppliers
            ],
            "purchase_orders": [
                {
                    "purchase_order_id": order.purchase_order_id,
                    "po_number": order.po_number,
                    "supplier_id": order.supplier_id,
                    "warehouse_id": order.warehouse_id,
                    "status": order.status,
                    "ordered_at": order.ordered_at,
                    "expected_at": order.expected_at,
                    "total_cents": order.total_cents,
                    "lines": [
                        {
                            "line_no": line.line_no,
                            "variant_id": line.variant_id,
                            "quantity": line.quantity,
                            "unit_cost_cents": line.unit_cost_cents,
                            "total_cents": line.total_cents,
                        }
                        for line in order.lines
                    ],
                }
                for order in self.purchase_orders
            ],
        }


def build_demo_catalog() -> DemoCatalog:
    """Build the committed example catalogue as an in-memory fixture."""

    items = tuple(
        DemoItem(
            item_id,
            sku,
            name,
            brand,
            category,
            price_cents,
            _build_variants(item_id, sku, name),
        )
        for item_id, sku, name, brand, category, price_cents in _ITEMS
    )
    purchase_orders = tuple(
        DemoPurchaseOrder(
            purchase_order_id,
            po_number,
            supplier_id,
            warehouse_id,
            status,
            ordered_at,
            expected_at,
            tuple(
                DemoPurchaseOrderLine(*line) for line in lines
            ),
        )
        for (
            purchase_order_id,
            po_number,
            supplier_id,
            warehouse_id,
            status,
            ordered_at,
            expected_at,
            lines,
        ) in _PURCHASE_ORDERS
    )
    return DemoCatalog(items, _WAREHOUSES, _SUPPLIERS, purchase_orders)


__all__ = [
    "IMAGE_URL_TEMPLATE",
    "SOURCE_URL",
    "DemoCatalog",
    "DemoItem",
    "DemoPurchaseOrder",
    "DemoPurchaseOrderLine",
    "DemoStockLevel",
    "DemoSupplier",
    "DemoVariant",
    "DemoWarehouse",
    "UnknownDemoRowError",
    "build_demo_catalog",
]
