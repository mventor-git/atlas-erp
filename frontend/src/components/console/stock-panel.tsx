import { count } from "@/lib/format";
import type { Audit, Catalog } from "@/lib/schemas";
import {
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui";

/**
 * Stock, and the warehouses it is booked against.
 *
 * `/api/catalog` carries per-variant totals and `/api/audit` carries item-level
 * quantities; neither carries a per-warehouse split, so the console shows the
 * two views the API does supply and lists the fulfilment centres without
 * inventing a distribution between them. Widening `DemoCatalog.to_dict()` to
 * carry `stock` per variant per warehouse would be a Python change; until then
 * this section cannot be a real per-warehouse matrix.
 */
export function StockPanel({ catalog, audit }: { catalog: Catalog; audit: Audit }) {
  const name = new Map(audit.items.map((item) => [item.item_id, item.name || item.sku]));
  const variants = catalog.items.flatMap((item) => item.variants);
  const onHand = variants.reduce((sum, variant) => sum + variant.on_hand, 0);
  const reserved = variants.reduce((sum, variant) => sum + variant.reserved, 0);

  return (
    <section aria-labelledby="stock-heading" className="grid gap-6">
      <div className="grid gap-3">
        <div>
          <h2 id="stock-heading" className="text-lg font-semibold">
            Item-level stock
          </h2>
          <p className="text-sm text-muted-foreground">
            Domain stock from <code>/api/audit</code>, ordered by item id. Across the catalogue
            fixture that is {count(onHand)} units on hand, {count(reserved)} reserved,{" "}
            {count(onHand - reserved)} available over {count(variants.length)} variants.
          </p>
        </div>
        <Table>
          <TableCaption className="sr-only">Units held per item.</TableCaption>
          <TableHeader>
            <TableRow>
              <TableHead scope="col">Item</TableHead>
              <TableHead scope="col">Name</TableHead>
              <TableHead scope="col" className="text-right">
                Quantity
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {audit.stock.map((level) => (
              <TableRow key={level.item_id}>
                <TableCell className="font-mono text-xs">{level.item_id}</TableCell>
                <TableCell>{name.get(level.item_id) ?? "-"}</TableCell>
                <TableCell className="text-right tabular-nums">{count(level.quantity)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>

      <div className="grid gap-3">
        <div>
          <h3 className="text-base font-semibold">Warehouses</h3>
          <p className="text-sm text-muted-foreground">
            The fulfilment centres the API reports. The API sends per-variant totals, not a
            per-warehouse split, so this table does not allocate stock between them.
          </p>
        </div>
        <Table>
          <TableCaption className="sr-only">Fulfilment centres.</TableCaption>
          <TableHeader>
            <TableRow>
              <TableHead scope="col">Code</TableHead>
              <TableHead scope="col">Warehouse</TableHead>
              <TableHead scope="col">Location</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {catalog.warehouses.map((warehouse) => (
              <TableRow key={warehouse.warehouse_id}>
                <TableHead scope="row">{warehouse.code}</TableHead>
                <TableCell>{warehouse.name}</TableCell>
                <TableCell>
                  {warehouse.city}, {warehouse.country_code}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    </section>
  );
}
