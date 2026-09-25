import { render } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";
import type { ReactElement } from "react";

import type { Audit, Catalog, Health } from "@/lib/schemas";
import { ThemeProvider } from "@/lib/theme";

/** Trimmed but shape-exact copies of the three Python routes. */
export const health: Health = {
  app_id: "atlas-erp",
  status: "ok",
  surface: "console",
  connect_api: "separate process on port 4310",
  data: "demo-fixture",
  catalog_source: "db/seed.sql",
  persistence: "in-memory",
  capabilities: 5,
  bind: "127.0.0.1",
};

export const catalog: Catalog = {
  source: "db/seed.sql",
  data: "demo-fixture",
  items: [
    {
      item_id: "item-aurora-linen-shirt",
      sku: "AUR-LIN-SHIRT",
      name: "Aurora Everyday Linen Shirt",
      brand: "Northline",
      category: "Apparel",
      price_cents: 8900,
      available: 5,
      variants: [
        {
          variant_id: "AUR-LIN-SHIRT-SAND-M",
          color: "Sand",
          size: "M",
          image_url: "https://images.unsplash.com/photo-1521572163474-6864f9cf17ab?auto=format",
          source_url: "https://unsplash.com/",
          alt_text: "Aurora Everyday Linen Shirt in Sand, size M",
          on_hand: 6,
          reserved: 1,
          available: 5,
        },
        {
          variant_id: "AUR-LIN-SHIRT-NAVY-M",
          color: "Navy",
          size: "M",
          image_url: "https://images.unsplash.com/photo-1596755094514-f87e34085b2c?auto=format",
          source_url: "https://unsplash.com/",
          alt_text: "Aurora Everyday Linen Shirt in Navy, size M",
          on_hand: 0,
          reserved: 0,
          available: 0,
        },
      ],
    },
    {
      item_id: "item-harbor-wool-throw",
      sku: "HAR-WOOL-THROW",
      name: "Harbor Wool Throw",
      brand: "Hearthline",
      category: "Home",
      price_cents: 11900,
      available: 47,
      variants: [
        {
          variant_id: "HAR-WOOL-THROW-FOG-ONESIZE",
          color: "Fog",
          size: "One Size",
          image_url: "https://images.unsplash.com/photo-1519710164239-da123dc03ef4?auto=format",
          source_url: "https://unsplash.com/",
          alt_text: "Harbor Wool Throw in Fog, one size",
          on_hand: 55,
          reserved: 6,
          available: 49,
        },
      ],
    },
  ],
  warehouses: [
    {
      warehouse_id: "wh-ams",
      code: "AMS",
      name: "Amsterdam Fulfilment Centre",
      city: "Amsterdam",
      country_code: "NL",
    },
  ],
  suppliers: [
    {
      supplier_id: "sup-northport",
      code: "NORTHPORT",
      name: "Northport Supply Group",
      contact_email: "purchasing@northport-supply.example",
      lead_time_days: 14,
    },
  ],
  purchase_orders: [
    {
      purchase_order_id: "po-2026-0058",
      po_number: "PO-2026-0058",
      supplier_id: "sup-northport",
      warehouse_id: "wh-ams",
      status: "open",
      ordered_at: "2026-09-08",
      expected_at: "2026-09-22",
      total_cents: 75400,
      lines: [
        { line_no: 1, variant_id: "AUR-LIN-SHIRT-SAND-M", quantity: 120, unit_cost_cents: 4100, total_cents: 492000 },
      ],
    },
  ],
};

export const audit: Audit = {
  items: [
    { item_id: "item-aurora-linen-shirt", sku: "AUR-LIN-SHIRT", name: "Aurora Everyday Linen Shirt", price_cents: 8900 },
    { item_id: "item-harbor-wool-throw", sku: "HAR-WOOL-THROW", name: "Harbor Wool Throw", price_cents: 11900 },
  ],
  stock: [
    { item_id: "item-aurora-linen-shirt", quantity: 4 },
    { item_id: "item-harbor-wool-throw", quantity: 58 },
  ],
  sales: [
    {
      sale_id: "sale-2026-0918",
      customer_id: "counter-breda",
      journal_id: "journal-sale-2026-0918",
      total_cents: 17800,
      lines: [{ item_id: "item-aurora-linen-shirt", quantity: 2, unit_price_cents: 8900, total_cents: 17800 }],
    },
  ],
  journals: [
    {
      journal_id: "journal-sale-2026-0918",
      sale_id: "sale-2026-0918",
      total_debits_cents: 17800,
      total_credits_cents: 17800,
      lines: [
        { account_code: "1000", debit_cents: 17800, credit_cents: 0 },
        { account_code: "4000", debit_cents: 0, credit_cents: 17800 },
      ],
    },
    {
      journal_id: "journal-unbalanced",
      sale_id: null,
      total_debits_cents: 100,
      total_credits_cents: 250,
      lines: [{ account_code: "1000", debit_cents: 100, credit_cents: 0 }],
    },
  ],
  stock_movements: [
    {
      movement_id: "movement-1",
      item_id: "item-aurora-linen-shirt",
      quantity_delta: -2,
      reason: "manual_sale",
      reference_id: "sale-2026-0918",
    },
  ],
};

type Routes = Partial<Record<"/api/health" | "/api/catalog" | "/api/audit", unknown>>;

function response(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

/** Stub the three GETs. A route mapped to `null` answers 500. */
export function mockApi(routes: Routes = { "/api/health": health, "/api/catalog": catalog, "/api/audit": audit }) {
  const fetchMock = vi.fn(async (input: string, _init?: RequestInit) => {
    const body = routes[input as keyof Routes];
    if (body === null) return response({ error: "internal server error" }, 500);
    if (body === undefined) return response({ error: "not found" }, 404);
    return response(body);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

export function renderConsole(element: ReactElement) {
  return render(
    <MemoryRouter>
      <ThemeProvider>{element}</ThemeProvider>
    </MemoryRouter>,
  );
}
