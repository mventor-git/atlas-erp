import { z } from "zod";

/**
 * Wire shapes of the three read-only Python routes, validated at the boundary.
 *
 * The Python side owns these shapes (`atlas_erp/web.py` health_payload and
 * to_dict, `atlas_erp/demo_catalog.py` to_dict, `Business.audit_snapshot`
 * to_dict). The console parses and renders; it never derives a value the API did
 * not send, so a schema change fails here instead of rendering `undefined`.
 */

const cents = z.number().int();

export const healthSchema = z.object({
  app_id: z.string(),
  status: z.string(),
  surface: z.string(),
  connect_api: z.string(),
  data: z.string(),
  catalog_source: z.string(),
  persistence: z.string(),
  capabilities: z.number().int(),
  bind: z.string(),
});

export const catalogSchema = z.object({
  source: z.string(),
  data: z.string(),
  items: z.array(
    z.object({
      item_id: z.string(),
      sku: z.string(),
      name: z.string(),
      brand: z.string(),
      category: z.string(),
      price_cents: cents,
      available: z.number().int(),
      variants: z.array(
        z.object({
          variant_id: z.string(),
          color: z.string(),
          size: z.string(),
          image_url: z.string(),
          source_url: z.string(),
          alt_text: z.string(),
          on_hand: z.number().int(),
          reserved: z.number().int(),
          available: z.number().int(),
        }),
      ),
    }),
  ),
  warehouses: z.array(
    z.object({
      warehouse_id: z.string(),
      code: z.string(),
      name: z.string(),
      city: z.string(),
      country_code: z.string(),
    }),
  ),
  suppliers: z.array(
    z.object({
      supplier_id: z.string(),
      code: z.string(),
      name: z.string(),
      contact_email: z.string(),
      lead_time_days: z.number().int(),
    }),
  ),
  purchase_orders: z.array(
    z.object({
      purchase_order_id: z.string(),
      po_number: z.string(),
      supplier_id: z.string(),
      warehouse_id: z.string(),
      status: z.string(),
      ordered_at: z.string(),
      expected_at: z.string().nullable(),
      total_cents: cents,
      lines: z.array(
        z.object({
          line_no: z.number().int(),
          variant_id: z.string(),
          quantity: z.number().int(),
          unit_cost_cents: cents,
          total_cents: cents,
        }),
      ),
    }),
  ),
});

export const auditSchema = z.object({
  items: z.array(
    z.object({
      item_id: z.string(),
      sku: z.string(),
      name: z.string(),
      price_cents: cents,
    }),
  ),
  stock: z.array(z.object({ item_id: z.string(), quantity: z.number().int() })),
  sales: z.array(
    z.object({
      sale_id: z.string(),
      customer_id: z.string(),
      journal_id: z.string(),
      total_cents: cents,
      lines: z.array(
        z.object({
          item_id: z.string(),
          quantity: z.number().int(),
          unit_price_cents: cents,
          total_cents: cents,
        }),
      ),
    }),
  ),
  journals: z.array(
    z.object({
      journal_id: z.string(),
      sale_id: z.string().nullable(),
      total_debits_cents: cents,
      total_credits_cents: cents,
      lines: z.array(
        z.object({
          account_code: z.string(),
          debit_cents: cents,
          credit_cents: cents,
        }),
      ),
    }),
  ),
  stock_movements: z.array(
    z.object({
      movement_id: z.string(),
      item_id: z.string(),
      quantity_delta: z.number().int(),
      reason: z.string(),
      reference_id: z.string(),
    }),
  ),
});

export type Health = z.infer<typeof healthSchema>;
export type Catalog = z.infer<typeof catalogSchema>;
export type CatalogItem = Catalog["items"][number];
export type Audit = z.infer<typeof auditSchema>;
