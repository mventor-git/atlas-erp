"""Small standalone smoke entrypoint for the Atlas ERP package."""

import json

from .business import Business
from .protocol import ProtocolKernel


def main() -> None:
    business = Business()
    item = business.register_item("item-1", "SKU-1", "Widget")
    order = business.create_purchase_order(
        "supplier-1", [(item.item_id, 2)], order_id="po-1"
    )
    business.receive_purchase_order(order.order_id, receipt_id="receipt-1")
    purchased_stock = business.stock_for(item.item_id)
    sale = business.create_manual_sale(
        "customer-1",
        [{"item_id": item.item_id, "quantity": 1, "unit_price_cents": 1250}],
        sale_id="sale-1",
    )
    journal = business.get_journal_for_sale(sale.sale_id)
    protocol = ProtocolKernel(business.registry)
    print("atlas-erp standalone registry: booted")
    print(json.dumps(protocol.manifest(), sort_keys=True))
    print(f"purchasing -> stock: {purchased_stock}")
    print(
        f"manual sale -> stock: {business.stock_for(item.item_id)}; "
        f"journal: {journal.journal_id}; balanced: "
        f"{journal.total_debits_cents == journal.total_credits_cents}"
    )
    print(
        "audit snapshot: "
        f"{json.dumps(business.audit_snapshot().to_dict(), sort_keys=True)}"
    )


if __name__ == "__main__":
    main()
