import { count, money } from "@/lib/format";
import type { Audit, Catalog } from "@/lib/schemas";

type Tile = { label: string; value: string };

/**
 * Counts and sums over what the API already sent. Nothing here is a business
 * rule: every figure is a row count or an arithmetic total of a field the API
 * returned, so the console cannot disagree with the domain about what happened.
 */
export function kpiTiles(catalog: Catalog, audit: Audit): Tile[] {
  const variants = catalog.items.flatMap((item) => item.variants);
  const onHand = variants.reduce((sum, variant) => sum + variant.on_hand, 0);
  const reserved = variants.reduce((sum, variant) => sum + variant.reserved, 0);
  return [
    { label: "Products", value: count(catalog.items.length) },
    { label: "Variants", value: count(variants.length) },
    { label: "Distinct images", value: count(new Set(variants.map((v) => v.image_url)).size) },
    { label: "Units on hand", value: count(onHand) },
    { label: "Units reserved", value: count(reserved) },
    { label: "Units available", value: count(onHand - reserved) },
    { label: "Item-level stock", value: count(audit.stock.reduce((s, l) => s + l.quantity, 0)) },
    { label: "Manual sales", value: count(audit.sales.length) },
    { label: "Sale revenue", value: money(audit.sales.reduce((s, sale) => s + sale.total_cents, 0)) },
    { label: "Journal entries", value: count(audit.journals.length) },
    {
      label: "Open purchase orders",
      value: count(catalog.purchase_orders.filter((order) => order.status === "open").length),
    },
  ];
}

export function KpiTiles({ tiles }: { tiles: Tile[] }) {
  return (
    <ul
      aria-label="Key figures"
      className="grid list-none grid-cols-2 gap-2 p-0 sm:grid-cols-3 lg:grid-cols-4"
    >
      {tiles.map((tile) => (
        <li
          key={tile.label}
          data-surface="true"
          className="rounded-lg border border-border bg-card px-3 py-2"
        >
          <div className="text-xl font-semibold tabular-nums">{tile.value}</div>
          <div className="text-xs text-muted-foreground">{tile.label}</div>
        </li>
      ))}
    </ul>
  );
}
