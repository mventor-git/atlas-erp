import json
import unittest
from contextlib import redirect_stdout
from dataclasses import FrozenInstanceError
from io import StringIO
from typing import cast

from atlas_erp import (
    CASH_ACCOUNT_CODE,
    REVENUE_ACCOUNT_CODE,
    Business,
    DuplicateItemError,
    DuplicateReceiptError,
    DuplicateSaleError,
    InsufficientStockError,
    InvalidJournalEntryError,
    InvalidOrderError,
    InvalidPriceError,
    InvalidQuantityError,
    InvalidSaleError,
    JournalEntry,
    JournalLine,
    MasterItem,
    Registry,
    StockLevel,
    UnbalancedJournalEntryError,
    UnknownItemError,
    UnknownOrderError,
)
from atlas_erp.__main__ import main as standalone_main


class BusinessPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.business = Business()
        self.first_item = self.business.register_item("item-1", "SKU-1", "First")
        self.second_item = self.business.register_item("item-2", "SKU-2", "Second")

    def test_receipt_moves_ordered_lines_into_stock_once(self) -> None:
        order = self.business.create_purchase_order(
            "supplier-1",
            [(self.first_item.item_id, 2), {"item_id": self.second_item.item_id, "quantity": 3}],
            order_id="po-1",
        )

        self.assertEqual(order.state, "open")
        self.assertIsNone(order.receipt_id)
        self.assertEqual(self.business.stock_for(self.first_item.item_id), 0)

        receipt = self.business.receive_purchase_order(order.order_id, "receipt-1")

        self.assertEqual(receipt.state, "received")
        self.assertEqual(receipt.lines, order.lines)
        self.assertEqual(self.business.get_purchase_order(order.order_id).state, "received")
        self.assertEqual(self.business.get_receipt(receipt.receipt_id), receipt)
        self.assertEqual(self.business.get_receipt_for_order(order.order_id), receipt)
        self.assertEqual(self.business.stock_for(self.first_item.item_id), 2)
        self.assertEqual(self.business.stock_for(self.second_item.item_id), 3)

    def test_duplicate_receipt_is_rejected_without_double_counting(self) -> None:
        order = self.business.create_purchase_order(
            "supplier-1", [(self.first_item.item_id, 4)], order_id="po-1"
        )
        self.business.receive_purchase_order(order.order_id, "receipt-1")

        with self.assertRaises(DuplicateReceiptError):
            self.business.receive_purchase_order(order.order_id, "receipt-1")
        with self.assertRaises(DuplicateReceiptError):
            self.business.receive_purchase_order(order.order_id, "receipt-2")

        self.assertEqual(self.business.stock_for(self.first_item.item_id), 4)
        self.assertEqual(len(self.business.receipts), 1)

    def test_unknown_items_and_invalid_quantities_are_rejected(self) -> None:
        with self.assertRaises(UnknownItemError):
            self.business.create_purchase_order(
                "supplier-1", [("missing-item", 1)], order_id="bad-item"
            )
        for quantity in (0, -1, 1.5, True, "", None):
            with self.subTest(quantity=quantity):
                with self.assertRaises(InvalidQuantityError):
                    self.business.create_purchase_order(
                        "supplier-1",
                        [(self.first_item.item_id, quantity)],
                        order_id=f"bad-quantity-{quantity!r}",
                    )
        with self.assertRaises(InvalidOrderError):
            self.business.create_purchase_order("supplier-1", [], order_id="empty")
        with self.assertRaises(UnknownItemError):
            self.business.stock_for("missing-item")
        self.assertEqual(self.business.purchase_orders, {})

    def test_duplicate_item_sku_is_rejected(self) -> None:
        with self.assertRaises(DuplicateItemError):
            self.business.register_item("item-3", "SKU-1")
        with self.assertRaises(UnknownItemError):
            self.business.get_item("item-3")

    def test_item_price_defaults_to_zero_and_rejects_invalid_cents(self) -> None:
        self.assertEqual(self.second_item.price_cents, 0)
        self.assertEqual(
            self.business.register_item("item-3", "SKU-3", "Third", 2500).price_cents,
            2500,
        )
        for price in (-1, 1.5, True, "100", None):
            with self.subTest(price=price):
                with self.assertRaises(InvalidPriceError):
                    self.business.register_item("item-4", "SKU-4", "Fourth", price)  # type: ignore[arg-type]
        self.assertNotIn("item-4", self.business.items)

    def test_receipt_before_order_is_rejected(self) -> None:
        with self.assertRaises(UnknownOrderError):
            self.business.receive_purchase_order("missing-order", "receipt-1")
        self.assertEqual(self.business.receipts, {})

    def test_generated_order_and_receipt_ids_are_resolvable(self) -> None:
        order = self.business.create_purchase_order(
            "supplier-1", [(self.first_item.item_id, 1)]
        )
        receipt = self.business.receive_purchase_order(order.order_id)

        self.assertTrue(order.order_id)
        self.assertTrue(receipt.receipt_id)
        stored_order = self.business.get_purchase_order(order.order_id)
        self.assertEqual(stored_order.order_id, order.order_id)
        self.assertEqual(stored_order.lines, order.lines)
        self.assertEqual(stored_order.state, "received")
        self.assertEqual(self.business.get_receipt(receipt.receipt_id), receipt)

    def test_generated_sale_id_is_resolvable(self) -> None:
        order = self.business.create_purchase_order(
            "supplier-1", [(self.first_item.item_id, 1)], order_id="po-generated-sale"
        )
        self.business.receive_purchase_order(order.order_id, "receipt-generated-sale")

        sale = self.business.create_manual_sale(
            "customer-1", [(self.first_item.item_id, 1, 500)]
        )

        self.assertTrue(sale.sale_id)
        self.assertEqual(self.business.get_sale(sale.sale_id), sale)
        self.assertEqual(
            self.business.get_journal_for_sale(sale.sale_id).sale_id, sale.sale_id
        )

    def test_business_boot_registers_local_capabilities_without_a_peer(self) -> None:
        registry = Registry()
        business = Business(registry)

        self.assertIs(business.registry, registry)
        capabilities = cast(list[str], registry.manifest()["capabilities"])
        self.assertEqual(
            set(capabilities),
            {
                "masterdata.items",
                "purchasing.purchase_orders",
                "inventory.stock",
                "sales.manual_sales",
                "finance.journals",
            },
        )
        self.assertEqual(business.purchase_orders, {})
        self.assertEqual(business.receipts, {})
        self.assertEqual(business.sales, {})
        self.assertEqual(business.journals, {})

    def test_manual_sale_decrements_stock_and_posts_balanced_journal(self) -> None:
        order = self.business.create_purchase_order(
            "supplier-1",
            [(self.first_item.item_id, 2), (self.second_item.item_id, 3)],
            order_id="po-sale-1",
        )
        self.business.receive_purchase_order(order.order_id, "receipt-sale-1")

        sale = self.business.create_manual_sale(
            "customer-1",
            [
                {"item_id": self.first_item.item_id, "quantity": 1, "unit_price_cents": 1250},
                {"item_id": self.second_item.item_id, "quantity": 1, "unit_price_cents": 500},
            ],
            sale_id="sale-1",
        )
        journal = self.business.get_journal(sale.journal_id)

        self.assertEqual(self.business.stock_for(self.first_item.item_id), 1)
        self.assertEqual(self.business.stock_for(self.second_item.item_id), 2)
        self.assertEqual(sale.total_cents, 1750)
        self.assertEqual(sale.customer_id, "customer-1")
        self.assertIs(self.business.get_sale(sale.sale_id), sale)
        self.assertIs(self.business.get_journal_for_sale(sale.sale_id), journal)
        self.assertEqual(self.business.sales, {sale.sale_id: sale})
        self.assertEqual(self.business.journals, {journal.journal_id: journal})
        self.assertTrue(journal.is_posted)
        self.assertEqual(journal.sale_id, sale.sale_id)
        self.assertEqual(journal.total_debits_cents, journal.total_credits_cents)
        self.assertEqual(journal.total_cents, sale.total_cents)
        cash = next(line for line in journal.lines if line.account_code == CASH_ACCOUNT_CODE)
        revenue = next(
            line for line in journal.lines if line.account_code == REVENUE_ACCOUNT_CODE
        )
        self.assertEqual((cash.debit_cents, cash.credit_cents), (1750, 0))
        self.assertEqual((revenue.debit_cents, revenue.credit_cents), (0, 1750))
        self.assertEqual(
            [movement.quantity_delta for movement in self.business.stock_movements.values() if movement.reason == "sale"],
            [-1, -1],
        )

        with self.assertRaises(FrozenInstanceError):
            journal.state = "draft"  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            journal.lines[0].debit_cents = 0  # type: ignore[misc]

    def test_oversell_fails_without_sale_journal_or_stock_mutation(self) -> None:
        order = self.business.create_purchase_order(
            "supplier-1", [(self.first_item.item_id, 1)], order_id="po-oversell"
        )
        self.business.receive_purchase_order(order.order_id, "receipt-oversell")
        movements_before = dict(self.business.stock_movements)

        with self.assertRaises(InsufficientStockError):
            self.business.create_manual_sale(
                "customer-1",
                [(self.first_item.item_id, 2, 500)],
                sale_id="sale-oversell",
            )

        self.assertEqual(self.business.stock_for(self.first_item.item_id), 1)
        self.assertEqual(self.business.stock_movements, movements_before)
        self.assertEqual(self.business.sales, {})
        self.assertEqual(self.business.journals, {})

    def test_duplicate_sale_id_fails_without_a_second_posting(self) -> None:
        order = self.business.create_purchase_order(
            "supplier-1", [(self.first_item.item_id, 1)], order_id="po-duplicate-sale"
        )
        self.business.receive_purchase_order(order.order_id, "receipt-duplicate-sale")
        sale = self.business.create_manual_sale(
            "customer-1", [(self.first_item.item_id, 1, 500)], sale_id="sale-1"
        )
        movements_before = dict(self.business.stock_movements)

        with self.assertRaises(DuplicateSaleError):
            self.business.create_manual_sale(
                "customer-2", [(self.first_item.item_id, 1, 700)], sale_id=sale.sale_id
            )

        self.assertEqual(self.business.get_sale(sale.sale_id), sale)
        self.assertEqual(self.business.stock_movements, movements_before)
        self.assertEqual(len(self.business.sales), 1)
        self.assertEqual(len(self.business.journals), 1)
        self.assertEqual(self.business.stock_for(self.first_item.item_id), 0)

    def test_invalid_manual_sale_input_fails_without_mutation(self) -> None:
        with self.assertRaises(UnknownItemError):
            self.business.create_manual_sale(
                "customer-1",
                [{"item_id": "missing-item", "quantity": 1, "unit_price_cents": 500}],
                sale_id="sale-unknown-item",
            )
        for quantity in (0, -1, 1.5, True, None):
            with self.subTest(quantity=quantity):
                with self.assertRaises(InvalidQuantityError):
                    self.business.create_manual_sale(
                        "customer-1",
                        [{"item_id": self.first_item.item_id, "quantity": quantity, "unit_price_cents": 500}],
                        sale_id=f"sale-bad-quantity-{quantity!r}",
                    )
        for price in (0, -1, 1.5, True, "500", None):
            with self.subTest(price=price):
                with self.assertRaises(InvalidPriceError):
                    self.business.create_manual_sale(
                        "customer-1",
                        [{"item_id": self.first_item.item_id, "quantity": 1, "unit_price_cents": price}],
                        sale_id=f"sale-bad-price-{price!r}",
                    )
        with self.assertRaises(InvalidSaleError):
            self.business.create_manual_sale("customer-1", [], sale_id="sale-empty")

        self.assertEqual(self.business.sales, {})
        self.assertEqual(self.business.journals, {})
        self.assertEqual(self.business.stock_movements, {})

    def test_journal_rejects_malformed_and_unbalanced_lines(self) -> None:
        for line in (
            (CASH_ACCOUNT_CODE, 0, 0),
            (CASH_ACCOUNT_CODE, 100, 100),
            (CASH_ACCOUNT_CODE, -1, 0),
            ("unknown", 100, 0),
        ):
            with self.subTest(line=line):
                with self.assertRaises(InvalidJournalEntryError):
                    JournalLine(*line)
        with self.assertRaises(UnbalancedJournalEntryError):
            JournalEntry(
                "journal-bad",
                "sale-bad",
                (
                    JournalLine(CASH_ACCOUNT_CODE, debit_cents=100),
                    JournalLine(REVENUE_ACCOUNT_CODE, credit_cents=99),
                ),
            )
        with self.assertRaises(InvalidJournalEntryError):
            JournalEntry("journal-missing-side", "sale-bad", ())

    def test_standalone_smoke_includes_manual_sale_and_journal(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            standalone_main()

        printed = output.getvalue()
        self.assertIn("purchasing -> stock: 2", printed)
        self.assertIn("manual sale -> stock: 1", printed)
        self.assertIn("journal: journal-sale-1", printed)
        self.assertIn("balanced: True", printed)


class AuditSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.business = Business()
        self.first_item = self.business.register_item("item-1", "SKU-1", "First", 1250)
        # Registered second but sorts first, so the projection order is asserted.
        # The second item keeps the default zero price.
        self.second_item = self.business.register_item("item-0", "SKU-0", "Second")
        order = self.business.create_purchase_order(
            "supplier-1",
            [(self.first_item.item_id, 2), (self.second_item.item_id, 3)],
            order_id="po-audit",
        )
        self.business.receive_purchase_order(order.order_id, "receipt-audit")

    def sell_first_item(self, sale_id: str, price_cents: int = 1250) -> None:
        self.business.create_manual_sale(
            "customer-1",
            [(self.first_item.item_id, 1, price_cents)],
            sale_id=sale_id,
        )

    def test_snapshot_projects_items_stock_sales_journals_and_movements(self) -> None:
        self.sell_first_item("sale-audit")

        snapshot = self.business.audit_snapshot()

        self.assertEqual(
            snapshot.items,
            (
                MasterItem("item-0", "SKU-0", "Second", 0),
                MasterItem("item-1", "SKU-1", "First", 1250),
            ),
        )
        self.assertEqual(
            snapshot.stock, (StockLevel("item-0", 3), StockLevel("item-1", 1))
        )
        self.assertEqual([sale.sale_id for sale in snapshot.sales], ["sale-audit"])
        self.assertEqual(
            [journal.sale_id for journal in snapshot.journals], ["sale-audit"]
        )
        self.assertTrue(all(journal.is_posted for journal in snapshot.journals))
        self.assertEqual(
            [
                (movement.item_id, movement.reason, movement.quantity_delta)
                for movement in snapshot.stock_movements
            ],
            [
                ("item-1", "receipt", 2),
                ("item-0", "receipt", 3),
                ("item-1", "sale", -1),
            ],
        )
        self.assertEqual(sum(level.quantity for level in snapshot.stock), 4)
        self.assertEqual(
            sum(movement.quantity_delta for movement in snapshot.stock_movements), 4
        )

    def test_snapshot_values_are_frozen_and_copies_isolate_business_state(self) -> None:
        self.sell_first_item("sale-immutable")

        snapshot = self.business.audit_snapshot()

        with self.assertRaises(FrozenInstanceError):
            snapshot.stock = ()  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            snapshot.items[0].price_cents = 99  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            snapshot.stock[0].quantity = 99  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            snapshot.sales[0].customer_id = "someone-else"  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            snapshot.journals[0].sale_id = "other-sale"  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            snapshot.stock_movements[0].quantity_delta = 99  # type: ignore[misc]

        payload = snapshot.to_dict()
        payload["stock"][0]["quantity"] = 999  # type: ignore[index]
        payload["items"][1]["price_cents"] = 1  # type: ignore[index]
        payload["sales"][0]["total_cents"] = 0  # type: ignore[index]

        self.assertEqual(
            snapshot.to_dict()["stock"],
            [{"item_id": "item-0", "quantity": 3}, {"item_id": "item-1", "quantity": 1}],
        )
        self.assertEqual(
            snapshot.to_dict()["items"][1]["price_cents"], 1250  # type: ignore[index]
        )
        self.assertEqual(snapshot.sales[0].total_cents, 1250)
        self.assertEqual(self.business.stock, {"item-0": 3, "item-1": 1})
        self.assertEqual(self.business.stock_for(self.first_item.item_id), 1)

    def test_snapshot_does_not_observe_later_changes(self) -> None:
        before = self.business.audit_snapshot()

        self.sell_first_item("sale-later")

        self.assertEqual(
            before.stock, (StockLevel("item-0", 3), StockLevel("item-1", 2))
        )
        self.assertEqual(before.sales, ())
        self.assertEqual(before.journals, ())
        self.assertEqual(len(before.stock_movements), 2)
        self.assertEqual(len(before.items), 2)

        after = self.business.audit_snapshot()
        self.assertEqual(len(after.sales), 1)
        self.assertEqual(len(after.journals), 1)
        self.assertEqual(len(after.stock_movements), 3)
        self.assertEqual(self.business.stock_for(self.first_item.item_id), 1)

    def test_to_dict_is_explicit_json_friendly_projection(self) -> None:
        self.sell_first_item("sale-json")

        payload = self.business.audit_snapshot().to_dict()

        self.assertEqual(
            payload,
            {
                "items": [
                    {
                        "item_id": "item-0",
                        "sku": "SKU-0",
                        "name": "Second",
                        "price_cents": 0,
                    },
                    {
                        "item_id": "item-1",
                        "sku": "SKU-1",
                        "name": "First",
                        "price_cents": 1250,
                    },
                ],
                "stock": [
                    {"item_id": "item-0", "quantity": 3},
                    {"item_id": "item-1", "quantity": 1},
                ],
                "sales": [
                    {
                        "sale_id": "sale-json",
                        "customer_id": "customer-1",
                        "journal_id": "journal-sale-json",
                        "total_cents": 1250,
                        "lines": [
                            {
                                "item_id": "item-1",
                                "quantity": 1,
                                "unit_price_cents": 1250,
                                "total_cents": 1250,
                            }
                        ],
                    }
                ],
                "journals": [
                    {
                        "journal_id": "journal-sale-json",
                        "sale_id": "sale-json",
                        "total_debits_cents": 1250,
                        "total_credits_cents": 1250,
                        "lines": [
                            {
                                "account_code": CASH_ACCOUNT_CODE,
                                "debit_cents": 1250,
                                "credit_cents": 0,
                            },
                            {
                                "account_code": REVENUE_ACCOUNT_CODE,
                                "debit_cents": 0,
                                "credit_cents": 1250,
                            },
                        ],
                    }
                ],
                "stock_movements": [
                    {
                        "movement_id": "receipt:receipt-audit:0",
                        "item_id": "item-1",
                        "quantity_delta": 2,
                        "reason": "receipt",
                        "reference_id": "receipt-audit",
                    },
                    {
                        "movement_id": "receipt:receipt-audit:1",
                        "item_id": "item-0",
                        "quantity_delta": 3,
                        "reason": "receipt",
                        "reference_id": "receipt-audit",
                    },
                    {
                        "movement_id": "sale:sale-json:0",
                        "item_id": "item-1",
                        "quantity_delta": -1,
                        "reason": "sale",
                        "reference_id": "sale-json",
                    },
                ],
            },
        )
        self.assertEqual(json.loads(json.dumps(payload)), payload)

    def test_snapshot_is_empty_before_any_business_activity(self) -> None:
        business = Business()

        snapshot = business.audit_snapshot()

        self.assertEqual(snapshot.items, ())
        self.assertEqual(snapshot.stock, ())
        self.assertEqual(snapshot.sales, ())
        self.assertEqual(snapshot.journals, ())
        self.assertEqual(snapshot.stock_movements, ())
        self.assertEqual(
            snapshot.to_dict(),
            {
                "items": [],
                "stock": [],
                "sales": [],
                "journals": [],
                "stock_movements": [],
            },
        )


if __name__ == "__main__":
    unittest.main()
