import { Badge } from "@/components/ui";
import type { BadgeVariants } from "@/components/ui/badge";
import { count, money } from "@/lib/format";
import type { Catalog } from "@/lib/schemas";
import {
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui";

/** An open order is a warning, a received order is a success; both are tokens. */
function statusVariant(status: string): BadgeVariants {
  return status === "received" ? "success" : "warning";
}

export function PurchasingPanel({ catalog }: { catalog: Catalog }) {
  const supplier = (id: string) => catalog.suppliers.find((s) => s.supplier_id === id);
  const warehouse = (id: string) => catalog.warehouses.find((w) => w.warehouse_id === id);
  return (
    <section aria-labelledby="purchasing-heading" className="grid gap-3">
      <div>
        <h2 id="purchasing-heading" className="text-lg font-semibold">
          Purchasing
        </h2>
        <p className="text-sm text-muted-foreground">
          Purchase orders from the catalogue fixture. Only a received order has posted stock.
        </p>
      </div>
      <Table>
        <TableCaption className="sr-only">
          Purchase orders with supplier, destination, status, and value.
        </TableCaption>
        <TableHeader>
          <TableRow>
            <TableHead scope="col">Order</TableHead>
            <TableHead scope="col">Supplier</TableHead>
            <TableHead scope="col">To</TableHead>
            <TableHead scope="col">Status</TableHead>
            <TableHead scope="col" className="text-right">
              Lines
            </TableHead>
            <TableHead scope="col" className="text-right">
              Value
            </TableHead>
            <TableHead scope="col">Ordered &rarr; expected</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {catalog.purchase_orders.map((order) => (
            <TableRow key={order.purchase_order_id}>
              <TableCell className="font-medium">{order.po_number}</TableCell>
              <TableCell>
                {supplier(order.supplier_id)?.name}{" "}
                <span className="text-muted-foreground">
                  {supplier(order.supplier_id)?.code}
                </span>
              </TableCell>
              <TableCell>{warehouse(order.warehouse_id)?.code}</TableCell>
              <TableCell>
                <Badge variant={statusVariant(order.status)}>{order.status}</Badge>
              </TableCell>
              <TableCell className="text-right tabular-nums">{count(order.lines.length)}</TableCell>
              <TableCell className="text-right tabular-nums">{money(order.total_cents)}</TableCell>
              <TableCell>
                {order.ordered_at} &rarr; {order.expected_at ?? "-"}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </section>
  );
}
