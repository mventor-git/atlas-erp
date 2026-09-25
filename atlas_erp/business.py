"""Process-local Atlas ERP business domain slice.

This module contains the small standalone purchasing-to-stock and
manual-sale-to-ledger seam for the product.  It intentionally has no database,
UI, or transport; its state is in memory and is not production persistence.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import cast
from uuid import uuid4

from .registry import DuplicateRegistrationError, Registry


class BusinessError(ValueError):
    """Base error for the business domain slice."""


class DuplicateItemError(BusinessError):
    """Raised when an item ID or SKU is already registered."""


class UnknownItemError(BusinessError):
    """Raised when an operation refers to an unregistered item."""


class InvalidQuantityError(BusinessError):
    """Raised when a line quantity is not a positive integer."""


class InvalidPriceError(BusinessError):
    """Raised when a cent price is not an integer in its allowed range."""


class InvalidOrderError(BusinessError):
    """Raised when a purchase order has no usable lines."""


class DuplicateOrderError(BusinessError):
    """Raised when a purchase order ID is already registered."""


class UnknownOrderError(BusinessError):
    """Raised when a receipt refers to an unknown purchase order."""


class DuplicateReceiptError(BusinessError):
    """Raised when an order is received more than once or a receipt ID repeats."""


class UnknownReceiptError(BusinessError):
    """Raised when a receipt lookup has no match."""


class InvalidSaleError(BusinessError):
    """Raised when a manual sale has no usable lines."""


class DuplicateSaleError(BusinessError):
    """Raised when a manual sale ID is already registered."""


class UnknownSaleError(BusinessError):
    """Raised when a sale lookup has no match."""


class InsufficientStockError(BusinessError):
    """Raised when a manual sale would make an item's stock negative."""


class InvalidJournalEntryError(BusinessError):
    """Raised when a journal entry or one of its lines is malformed."""


class UnbalancedJournalEntryError(InvalidJournalEntryError):
    """Raised when journal debits and credits do not match."""


class DuplicateJournalError(BusinessError):
    """Raised when a journal entry ID is already registered."""


class UnknownJournalError(BusinessError):
    """Raised when a journal entry lookup has no match."""


# The slice deliberately has only the two accounts needed for a cash sale.
CASH_ACCOUNT_CODE = "cash"
REVENUE_ACCOUNT_CODE = "revenue"
CASH_ACCOUNT = CASH_ACCOUNT_CODE
REVENUE_ACCOUNT = REVENUE_ACCOUNT_CODE
JOURNAL_ACCOUNT_CODES = frozenset(
    {CASH_ACCOUNT_CODE, REVENUE_ACCOUNT_CODE}
)


@dataclass(frozen=True)
class MasterItem:
    """A locally registered master item with its local selling price.

    ``price_cents`` defaults to ``0`` so existing item registrations stay
    valid, and it is the only price a connected peer may submit for the item.
    """

    item_id: str
    sku: str
    name: str = ""
    price_cents: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "price_cents", _item_price(self.price_cents))


@dataclass(frozen=True)
class PurchaseOrderLine:
    """An item and its ordered quantity."""

    item_id: str
    quantity: int


@dataclass(frozen=True)
class PurchaseOrder:
    """The state of a purchase order in the local domain."""

    order_id: str
    supplier_id: str
    lines: tuple[PurchaseOrderLine, ...]
    state: str = "open"
    receipt_id: str | None = None

    @property
    def is_received(self) -> bool:
        return self.state == "received"


@dataclass(frozen=True)
class Receipt:
    """The immutable movement created when an order is received."""

    receipt_id: str
    order_id: str
    lines: tuple[PurchaseOrderLine, ...]
    state: str = "received"


@dataclass(frozen=True)
class SaleLine:
    """One known item, quantity, and integer-cent selling price on a sale."""

    item_id: str
    quantity: int
    unit_price_cents: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "item_id", _text(self.item_id, "item_id"))
        object.__setattr__(self, "quantity", _quantity(self.quantity))
        object.__setattr__(self, "unit_price_cents", _price(self.unit_price_cents))

    @property
    def total_cents(self) -> int:
        return self.quantity * self.unit_price_cents


@dataclass(frozen=True)
class Sale:
    """The immutable local record of a posted manual cash sale."""

    sale_id: str
    customer_id: str
    lines: tuple[SaleLine, ...]
    journal_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "sale_id", _text(self.sale_id, "sale_id"))
        object.__setattr__(self, "customer_id", _text(self.customer_id, "customer_id"))
        object.__setattr__(self, "journal_id", _text(self.journal_id, "journal_id"))
        try:
            lines = tuple(self.lines)
        except TypeError as exc:
            raise InvalidSaleError("sale lines must be an iterable") from exc
        if not lines or not all(isinstance(line, SaleLine) for line in lines):
            raise InvalidSaleError("sale must contain valid sale lines")
        object.__setattr__(self, "lines", lines)

    @property
    def total_cents(self) -> int:
        return sum(line.total_cents for line in self.lines)


@dataclass(frozen=True)
class JournalLine:
    """One debit-or-credit line in a posted general-ledger entry."""

    account_code: str
    debit_cents: int = 0
    credit_cents: int = 0

    def __post_init__(self) -> None:
        account_code = _text(self.account_code, "account_code")
        if account_code not in JOURNAL_ACCOUNT_CODES:
            raise InvalidJournalEntryError(
                f"unsupported journal account code: {account_code}"
            )
        debit_cents = _journal_cents(self.debit_cents, "debit_cents")
        credit_cents = _journal_cents(self.credit_cents, "credit_cents")
        if (debit_cents == 0) == (credit_cents == 0):
            raise InvalidJournalEntryError(
                "journal line must contain exactly one of debit_cents or credit_cents"
            )
        object.__setattr__(self, "account_code", account_code)
        object.__setattr__(self, "debit_cents", debit_cents)
        object.__setattr__(self, "credit_cents", credit_cents)

    @property
    def amount_cents(self) -> int:
        return self.debit_cents or self.credit_cents

    @property
    def side(self) -> str:
        return "debit" if self.debit_cents else "credit"


@dataclass(frozen=True)
class JournalEntry:
    """A minimal immutable posted journal entry."""

    journal_id: str
    sale_id: str | None
    lines: tuple[JournalLine, ...]
    state: str = "posted"

    def __post_init__(self) -> None:
        object.__setattr__(self, "journal_id", _text(self.journal_id, "journal_id"))
        if self.sale_id is not None:
            object.__setattr__(self, "sale_id", _text(self.sale_id, "sale_id"))
        state = _text(self.state, "journal state")
        if state != "posted":
            raise InvalidJournalEntryError("journal entries in this slice are posted")
        try:
            raw_lines = tuple(self.lines)
        except TypeError as exc:
            raise InvalidJournalEntryError("journal lines must be an iterable") from exc
        if len(raw_lines) < 2:
            raise InvalidJournalEntryError(
                "journal entry must contain at least two lines"
            )
        lines = tuple(_journal_line(line) for line in raw_lines)
        debits = sum(line.debit_cents for line in lines)
        credits = sum(line.credit_cents for line in lines)
        if debits != credits:
            raise UnbalancedJournalEntryError(
                f"journal is unbalanced: debits={debits}, credits={credits}"
            )
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "lines", lines)

    @property
    def entry_id(self) -> str:
        return self.journal_id

    @property
    def is_posted(self) -> bool:
        return self.state == "posted"

    @property
    def total_debits_cents(self) -> int:
        return sum(line.debit_cents for line in self.lines)

    @property
    def total_credits_cents(self) -> int:
        return sum(line.credit_cents for line in self.lines)

    @property
    def total_cents(self) -> int:
        return self.total_debits_cents


@dataclass(frozen=True)
class StockMovement:
    """An immutable stock delta derived from a receipt or sale."""

    movement_id: str
    item_id: str
    quantity_delta: int
    reason: str
    reference_id: str

    def __post_init__(self) -> None:
        movement_id = _text(self.movement_id, "movement_id")
        item_id = _text(self.item_id, "item_id")
        if isinstance(self.quantity_delta, bool) or not isinstance(
            self.quantity_delta, int
        ) or self.quantity_delta == 0:
            raise BusinessError("stock movement quantity_delta must be a non-zero integer")
        reason = _text(self.reason, "movement reason")
        reference_id = _text(self.reference_id, "movement reference_id")
        object.__setattr__(self, "movement_id", movement_id)
        object.__setattr__(self, "item_id", item_id)
        object.__setattr__(self, "quantity_delta", self.quantity_delta)
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "reference_id", reference_id)

    @property
    def quantity(self) -> int:
        return self.quantity_delta


@dataclass(frozen=True)
class StockLevel:
    """An immutable per-item stock level for the read/audit projection."""

    item_id: str
    quantity: int


@dataclass(frozen=True)
class AuditSnapshot:
    """A point-in-time, read-only view of local business state.

    Every field is a tuple of frozen values, so a snapshot cannot be used to
    mutate :class:`Business` state.  It is a projection of in-memory state
    only; it is not a durable record.
    """

    items: tuple[MasterItem, ...]
    stock: tuple[StockLevel, ...]
    sales: tuple[Sale, ...]
    journals: tuple[JournalEntry, ...]
    stock_movements: tuple[StockMovement, ...]

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-friendly mapping of the snapshot.

        The shape is spelled out here on purpose: this is the local read
        boundary, so these field names are part of the contract and a later
        adapter should reuse them instead of re-deriving values.
        """

        return {
            "items": [
                {
                    "item_id": item.item_id,
                    "sku": item.sku,
                    "name": item.name,
                    "price_cents": item.price_cents,
                }
                for item in self.items
            ],
            "stock": [
                {"item_id": level.item_id, "quantity": level.quantity}
                for level in self.stock
            ],
            "sales": [
                {
                    "sale_id": sale.sale_id,
                    "customer_id": sale.customer_id,
                    "journal_id": sale.journal_id,
                    "total_cents": sale.total_cents,
                    "lines": [
                        {
                            "item_id": line.item_id,
                            "quantity": line.quantity,
                            "unit_price_cents": line.unit_price_cents,
                            "total_cents": line.total_cents,
                        }
                        for line in sale.lines
                    ],
                }
                for sale in self.sales
            ],
            "journals": [
                {
                    "journal_id": journal.journal_id,
                    "sale_id": journal.sale_id,
                    "total_debits_cents": journal.total_debits_cents,
                    "total_credits_cents": journal.total_credits_cents,
                    "lines": [
                        {
                            "account_code": line.account_code,
                            "debit_cents": line.debit_cents,
                            "credit_cents": line.credit_cents,
                        }
                        for line in journal.lines
                    ],
                }
                for journal in self.journals
            ],
            "stock_movements": [
                {
                    "movement_id": movement.movement_id,
                    "item_id": movement.item_id,
                    "quantity_delta": movement.quantity_delta,
                    "reason": movement.reason,
                    "reference_id": movement.reference_id,
                }
                for movement in self.stock_movements
            ],
        }


_BUSINESS_MODULES = (
    ("masterdata.items", "masterdata", "catalog", "items"),
    ("purchasing.purchase_orders", "purchasing", "orders", "purchase-orders"),
    ("inventory.stock", "inventory", "stock", "availability"),
    ("sales.manual_sales", "sales", "sales", "manual-sales"),
    ("finance.journals", "finance", "journals", "general-ledger"),
)


def register_business_capabilities(registry: Registry) -> Registry:
    """Register the capabilities exercised by :class:`Business`.

    Existing capabilities are left alone so the domain can use a registry that
    already has an equivalent local module, such as the original smoke fixture.
    """

    existing_capabilities = cast(Iterable[object], registry.manifest()["capabilities"])
    for capability, cluster_name, plugin_name, module_name in _BUSINESS_MODULES:
        if capability in existing_capabilities:
            continue
        if cluster_name not in registry.cluster_names:
            registry.register_cluster(cluster_name)
        try:
            registry.register_plugin(cluster_name, plugin_name)
        except DuplicateRegistrationError:
            pass
        registry.register_module(
            cluster_name,
            plugin_name,
            module_name,
            {capability: {"read", "write"}},
        )
    return registry


def _text(value: object, kind: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BusinessError(f"{kind} must be a non-empty string")
    return value.strip()


def _new_or_given_id(value: str | None, kind: str) -> str:
    return _text(value, kind) if value is not None else uuid4().hex


def _quantity(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise InvalidQuantityError("quantity must be a positive integer")
    return value


def _price(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise InvalidPriceError("unit_price_cents must be a positive integer")
    return value


def _item_price(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidPriceError("item price_cents must be a non-negative integer")
    return value


def _journal_cents(value: object, kind: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidJournalEntryError(f"{kind} must be a non-negative integer")
    return value


def _line(value: object) -> PurchaseOrderLine:
    if isinstance(value, PurchaseOrderLine):
        return PurchaseOrderLine(
            _text(value.item_id, "item_id"),
            _quantity(value.quantity),
        )
    if isinstance(value, Mapping):
        if "item_id" not in value or "quantity" not in value:
            raise InvalidOrderError("line mappings require item_id and quantity")
        item_id = value["item_id"]
        quantity = value["quantity"]
    else:
        if isinstance(value, (str, bytes)):
            raise InvalidOrderError("line must contain item_id and quantity")
        try:
            item_id, quantity = value  # type: ignore[misc]
        except (TypeError, ValueError) as exc:
            raise InvalidOrderError("line must contain item_id and quantity") from exc
    return PurchaseOrderLine(_text(item_id, "item_id"), _quantity(quantity))


def _sale_line(value: object) -> SaleLine:
    if isinstance(value, SaleLine):
        return SaleLine(
            _text(value.item_id, "item_id"),
            _quantity(value.quantity),
            _price(value.unit_price_cents),
        )
    if isinstance(value, Mapping):
        if "item_id" not in value or "quantity" not in value:
            raise InvalidSaleError(
                "sale line mappings require item_id, quantity, and unit_price_cents"
            )
        item_id = value["item_id"]
        quantity = value["quantity"]
        if "unit_price_cents" in value:
            unit_price_cents = value["unit_price_cents"]
        elif "unit_price" in value:
            unit_price_cents = value["unit_price"]
        else:
            raise InvalidSaleError(
                "sale line mappings require item_id, quantity, and unit_price_cents"
            )
    else:
        if isinstance(value, (str, bytes)):
            raise InvalidSaleError(
                "sale line must contain item_id, quantity, and unit_price_cents"
            )
        try:
            item_id, quantity, unit_price_cents = value  # type: ignore[misc]
        except (TypeError, ValueError) as exc:
            raise InvalidSaleError(
                "sale line must contain item_id, quantity, and unit_price_cents"
            ) from exc
    return SaleLine(
        _text(item_id, "item_id"),
        _quantity(quantity),
        _price(unit_price_cents),
    )


def _journal_line(value: object) -> JournalLine:
    if isinstance(value, JournalLine):
        return value
    if not isinstance(value, Mapping):
        raise InvalidJournalEntryError("journal lines must be JournalLine values")
    if "account_code" in value:
        account_code = value["account_code"]
    elif "account" in value:
        account_code = value["account"]
    else:
        raise InvalidJournalEntryError("journal line mappings require account_code")
    return JournalLine(
        account_code,
        value.get("debit_cents", 0),
        value.get("credit_cents", 0),
    )


class Business:
    """A small in-memory purchasing, stock, sales, and finance domain object.

    The object owns local business state only.  It does not require or create
    a peer, protocol transport, or persistence layer.
    """

    def __init__(self, registry: Registry | None = None) -> None:
        self.registry = register_business_capabilities(
            registry if registry is not None else Registry()
        )
        self._items: dict[str, MasterItem] = {}
        self._items_by_sku: dict[str, MasterItem] = {}
        self._orders: dict[str, PurchaseOrder] = {}
        self._receipts: dict[str, Receipt] = {}
        self._receipts_by_order: dict[str, str] = {}
        self._sales: dict[str, Sale] = {}
        self._journals: dict[str, JournalEntry] = {}
        self._stock_movements: dict[str, StockMovement] = {}

    def register_item(
        self,
        item_id: str,
        sku: str,
        name: str | None = None,
        price_cents: int = 0,
    ) -> MasterItem:
        """Register a master item with a unique ID, SKU, and local price."""

        item_id = _text(item_id, "item_id")
        sku = _text(sku, "sku")
        normalized_name = "" if name is None else _text(name, "item name")
        if item_id in self._items:
            raise DuplicateItemError(f"item already registered: {item_id}")
        if sku in self._items_by_sku:
            raise DuplicateItemError(f"item SKU already registered: {sku}")
        item = MasterItem(item_id, sku, normalized_name, _item_price(price_cents))
        self._items[item.item_id] = item
        self._items_by_sku[item.sku] = item
        return item

    def get_item(self, item_id: str) -> MasterItem:
        item_id = _text(item_id, "item_id")
        try:
            return self._items[item_id]
        except KeyError as exc:
            raise UnknownItemError(f"unknown item: {item_id}") from exc

    def create_purchase_order(
        self,
        supplier_id: str,
        lines: Iterable[object],
        order_id: str | None = None,
    ) -> PurchaseOrder:
        """Create an open order for one or more known item lines."""

        supplier_id = _text(supplier_id, "supplier_id")
        order_id = _new_or_given_id(order_id, "order_id")
        if order_id in self._orders:
            raise DuplicateOrderError(f"purchase order already exists: {order_id}")
        try:
            raw_lines = tuple(lines)
        except TypeError as exc:
            raise InvalidOrderError("lines must be an iterable") from exc
        if not raw_lines:
            raise InvalidOrderError("purchase order must contain at least one line")
        normalized_lines = tuple(_line(line) for line in raw_lines)
        for line in normalized_lines:
            self.get_item(line.item_id)
        order = PurchaseOrder(order_id, supplier_id, normalized_lines)
        self._orders[order.order_id] = order
        return order

    def get_purchase_order(self, order_id: str) -> PurchaseOrder:
        order_id = _text(order_id, "order_id")
        try:
            return self._orders[order_id]
        except KeyError as exc:
            raise UnknownOrderError(f"unknown purchase order: {order_id}") from exc

    def receive_purchase_order(
        self, order_id: str, receipt_id: str | None = None
    ) -> Receipt:
        """Receive all lines in an order once and record one stock movement."""

        order = self.get_purchase_order(order_id)
        if order.is_received:
            raise DuplicateReceiptError(f"purchase order already received: {order.order_id}")
        receipt_id = _new_or_given_id(receipt_id, "receipt_id")
        if receipt_id in self._receipts:
            raise DuplicateReceiptError(f"receipt already exists: {receipt_id}")
        receipt = Receipt(receipt_id, order.order_id, order.lines)
        movements = self._movements_for(
            "receipt", receipt_id, order.lines, receipt_id, sign=1
        )
        if any(movement.movement_id in self._stock_movements for movement in movements):
            raise DuplicateReceiptError(f"stock movement already exists: {receipt_id}")
        self._receipts[receipt.receipt_id] = receipt
        self._receipts_by_order[order.order_id] = receipt.receipt_id
        self._orders[order.order_id] = replace(
            order, state="received", receipt_id=receipt.receipt_id
        )
        self._stock_movements.update(
            {movement.movement_id: movement for movement in movements}
        )
        return receipt

    def get_receipt(self, receipt_id: str) -> Receipt:
        receipt_id = _text(receipt_id, "receipt_id")
        try:
            return self._receipts[receipt_id]
        except KeyError as exc:
            raise UnknownReceiptError(f"unknown receipt: {receipt_id}") from exc

    def get_receipt_for_order(self, order_id: str) -> Receipt | None:
        order = self.get_purchase_order(order_id)
        receipt_id = self._receipts_by_order.get(order.order_id)
        return None if receipt_id is None else self._receipts[receipt_id]

    def create_manual_sale(
        self,
        customer_id: str,
        lines: Iterable[object],
        sale_id: str | None = None,
    ) -> Sale:
        """Record one manual cash sale, its stock movements, and its journal."""

        customer_id = _text(customer_id, "customer_id")
        sale_id = _new_or_given_id(sale_id, "sale_id")
        if sale_id in self._sales:
            raise DuplicateSaleError(f"manual sale already exists: {sale_id}")
        try:
            raw_lines = tuple(lines)
        except TypeError as exc:
            raise InvalidSaleError("sale lines must be an iterable") from exc
        if not raw_lines:
            raise InvalidSaleError("manual sale must contain at least one line")
        normalized_lines = tuple(_sale_line(line) for line in raw_lines)

        quantities: dict[str, int] = {}
        total_cents = 0
        for line in normalized_lines:
            self.get_item(line.item_id)
            quantities[line.item_id] = quantities.get(line.item_id, 0) + line.quantity
            total_cents += line.total_cents
        for item_id, quantity in quantities.items():
            available = self.stock_for(item_id)
            if available < quantity:
                raise InsufficientStockError(
                    f"insufficient stock for {item_id}: available={available}, requested={quantity}"
                )

        journal_id = f"journal-{sale_id}"
        if journal_id in self._journals:
            raise DuplicateJournalError(f"journal entry already exists: {journal_id}")
        journal = JournalEntry(
            journal_id,
            sale_id,
            (
                JournalLine(CASH_ACCOUNT_CODE, debit_cents=total_cents),
                JournalLine(REVENUE_ACCOUNT_CODE, credit_cents=total_cents),
            ),
        )
        sale = Sale(sale_id, customer_id, normalized_lines, journal_id)
        movements = self._movements_for(
            "sale", sale_id, normalized_lines, sale_id, sign=-1
        )
        if any(movement.movement_id in self._stock_movements for movement in movements):
            raise DuplicateSaleError(f"stock movement already exists for sale: {sale_id}")

        # All validation and object construction happens before this commit.
        self._sales[sale.sale_id] = sale
        self._journals[journal.journal_id] = journal
        self._stock_movements.update(
            {movement.movement_id: movement for movement in movements}
        )
        return sale

    def get_sale(self, sale_id: str) -> Sale:
        sale_id = _text(sale_id, "sale_id")
        try:
            return self._sales[sale_id]
        except KeyError as exc:
            raise UnknownSaleError(f"unknown sale: {sale_id}") from exc

    def get_journal(self, journal_id: str) -> JournalEntry:
        journal_id = _text(journal_id, "journal_id")
        try:
            return self._journals[journal_id]
        except KeyError as exc:
            raise UnknownJournalError(f"unknown journal entry: {journal_id}") from exc

    def get_journal_for_sale(self, sale_id: str) -> JournalEntry:
        sale = self.get_sale(sale_id)
        return self.get_journal(sale.journal_id)

    def _movements_for(
        self,
        prefix: str,
        reference_id: str,
        lines: Iterable[PurchaseOrderLine | SaleLine],
        movement_reference: str,
        *,
        sign: int,
    ) -> tuple[StockMovement, ...]:
        return tuple(
            StockMovement(
                movement_id=f"{prefix}:{reference_id}:{index}",
                item_id=line.item_id,
                quantity_delta=line.quantity * sign,
                reason=prefix,
                reference_id=movement_reference,
            )
            for index, line in enumerate(lines)
        )

    def stock_for(self, item_id: str) -> int:
        """Return stock derived from all recorded stock movements."""

        item = self.get_item(item_id)
        return sum(
            movement.quantity_delta
            for movement in self._stock_movements.values()
            if movement.item_id == item.item_id
        )

    @property
    def items(self) -> Mapping[str, MasterItem]:
        return dict(self._items)

    @property
    def purchase_orders(self) -> Mapping[str, PurchaseOrder]:
        return dict(self._orders)

    @property
    def receipts(self) -> Mapping[str, Receipt]:
        return dict(self._receipts)

    @property
    def stock_movements(self) -> Mapping[str, StockMovement]:
        return dict(self._stock_movements)

    @property
    def sales(self) -> Mapping[str, Sale]:
        return dict(self._sales)

    @property
    def journals(self) -> Mapping[str, JournalEntry]:
        return dict(self._journals)

    @property
    def stock(self) -> Mapping[str, int]:
        return {item_id: self.stock_for(item_id) for item_id in self._items}

    def audit_snapshot(self) -> AuditSnapshot:
        """Return an immutable read-only view of local business state.

        Items and stock levels are ordered by item ID so the projection is
        stable, and the other collections keep the order in which they were
        recorded.  A snapshot does not observe later changes.
        """

        return AuditSnapshot(
            items=tuple(self._items[item_id] for item_id in sorted(self._items)),
            stock=tuple(
                StockLevel(item_id, quantity)
                for item_id, quantity in sorted(self.stock.items())
            ),
            sales=tuple(self._sales.values()),
            journals=tuple(self._journals.values()),
            stock_movements=tuple(self._stock_movements.values()),
        )


__all__ = [
    "AuditSnapshot",
    "Business",
    "BusinessError",
    "CASH_ACCOUNT",
    "CASH_ACCOUNT_CODE",
    "DuplicateItemError",
    "DuplicateJournalError",
    "DuplicateOrderError",
    "DuplicateReceiptError",
    "DuplicateSaleError",
    "InvalidJournalEntryError",
    "InvalidOrderError",
    "InvalidPriceError",
    "InvalidQuantityError",
    "InvalidSaleError",
    "InsufficientStockError",
    "JOURNAL_ACCOUNT_CODES",
    "JournalEntry",
    "JournalLine",
    "MasterItem",
    "PurchaseOrder",
    "PurchaseOrderLine",
    "REVENUE_ACCOUNT",
    "REVENUE_ACCOUNT_CODE",
    "Receipt",
    "Sale",
    "SaleLine",
    "StockLevel",
    "StockMovement",
    "UnbalancedJournalEntryError",
    "UnknownItemError",
    "UnknownJournalError",
    "UnknownOrderError",
    "UnknownReceiptError",
    "UnknownSaleError",
    "register_business_capabilities",
]
