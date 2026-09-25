# Atlas ERP example database

`db/` holds a **reproducible example dataset** for local development, demos, and
the connected-peer work with the sibling `atlas-ecom` repository.

It is **not** the production migration history and **not** a backup. Every object
is prefixed `example_`, every statement is idempotent, and the contents are
fictional. Nothing here is a schema decision of record; the real `atlas_erp`
database is still to be built.

- `db/schema.sql` — the eight `example_*` tables. Run first.
- `db/seed.sql` — six fictional products with colour/size variants, one image
  per variant, stock, suppliers, warehouses, and two purchase orders.

## Requirements

PostgreSQL 12 or newer (`createdb` and `psql` on your `PATH`). The files use only
`CREATE TABLE IF NOT EXISTS` and `INSERT ... ON CONFLICT`, and no extension.

## Load order

Run all commands from the repository root. The database name is yours to choose;
`atlas_erp_example` below is only a suggestion, and it is deliberately *not*
`atlas_erp` so example data can never be confused with the real database the
contract reserves for this product.

Create the database once:

```sh
createdb atlas_erp_example
```

Then load `schema.sql` before `seed.sql` — the seed inserts rows that the schema
defines and will fail without it:

```sh
psql -d atlas_erp_example -v ON_ERROR_STOP=1 -1 -f db/schema.sql
psql -d atlas_erp_example -v ON_ERROR_STOP=1 -1 -f db/seed.sql
```

Both in one session, which is the same result:

```sh
psql -d atlas_erp_example -v ON_ERROR_STOP=1 -1 -f db/schema.sql -f db/seed.sql
```

Add `-h`, `-p`, and `-U` as your server needs. `-1` wraps the load in one
transaction so a failure leaves nothing half-applied; `ON_ERROR_STOP=1` makes
`psql` exit non-zero on the first error instead of carrying on.

Both files are safe to re-run. Re-running them brings the database back to
exactly this state and changes no row counts, so a reset is either a re-run or:

```sh
dropdb atlas_erp_example
```

The only value that is not reproducible across loads is
`example_stock_levels.updated_at`, which the seed sets to `now()` on purpose so
a reload shows that it was reloaded.

## Shared catalog identity

The `item_id`, `variant_id`, and variant `sku` values are the contract that lets
the `atlas-ecom` example seed name the same catalogue rows. Item IDs and prices
are fixed:

| `item_id`                     | `sku`            | Name                        | Brand       | Category | `price_cents` |
| ----------------------------- | ---------------- | --------------------------- | ----------- | -------- | ------------- |
| `item-aurora-linen-shirt`     | `AUR-LIN-SHIRT`  | Aurora Everyday Linen Shirt | Northline   | Apparel  | 8900          |
| `item-meridian-court-sneaker` | `MER-COURT-SNEAK`| Meridian Court Sneaker      | Meridian    | Footwear | 12900         |
| `item-harbor-wool-throw`      | `HAR-WOOL-THROW` | Harbor Wool Throw           | Hearthline  | Home     | 11900         |
| `item-terra-pour-over-set`    | `TER-POUR-01`    | Terra Pour-Over Set         | Kiln & Coast| Kitchen  | 6400          |
| `item-solstice-trail-bottle`  | `SOL-TRAIL-BTL`  | Solstice Trail Bottle       | Fieldcraft  | Outdoors | 3200          |
| `item-loom-everyday-tote`     | `LOOM-TOTE-01`   | Loom Everyday Tote          | Loom        | Bags     | 7400          |

Variants are the full cartesian product of an item's colours and sizes, and both
identifiers are derived from the item rather than invented per row:

- `variant_id` = item `sku` + `-` + uppercased colour + `-` + uppercased size,
  e.g. `AUR-LIN-SHIRT-SAND-XS`
- variant `sku` = the same string, and the schema enforces `CHECK (sku =
  variant_id)` so the two cannot drift

Each colour and size is uppercased with its own non-alphanumeric characters
dropped, while the two stay dash-joined: `One Size` becomes `ONESIZE`, `500ml`
becomes `500ML`, and `1L` stays `1L`.

**If the atlas-ecom example seed changes, change this one in the same commit.**
The two files are only comparable while their identity agrees, and nothing in
either repository enforces that across the pair.

An example database loaded from an earlier revision of this seed keeps its old
`var-`-prefixed rows alongside the new ones, because every insert upserts on the
primary key and nothing deletes. `dropdb` and reload rather than re-running the
seed over it.

Two more rows are shared by name: warehouses `wh-ams` and `wh-ldn`, and suppliers
`sup-hollowbay` and `sup-northport`. Purchase orders are ERP-local
(`po-2026-0041`, `po-2026-0058`) and have no counterpart in a storefront.

## Images

`example_variant_images` has the `variant_id` as its primary key, so a variant
cannot have more than one image row and the seed gives every variant exactly one.
`image_url` is a plain `https://images.unsplash.com/photo-...` CDN link and
`source_url` is `https://unsplash.com/`; `CHECK` constraints in the schema pin
both so example data cannot drift onto another host or a local file path.

Sizes within one colour share that colour's photo, and each colour of each item
has its own photo, which keeps the 37 variants on 13 distinct images.

**These are placeholder images and no image files are committed.** They are remote
Unsplash CDN links, and this repository carries no photographer attribution, so
nothing here is cleared for production use. Before any real deployment: confirm
each photo's licence through normal Unsplash review, record the photographer and
required attribution per image, and replace `source_url` with the real photo page
if you keep these URLs. Alt text names the product and colourway only, because the
photographs are placeholders rather than accurate product shots.

## Verify a load

Expected: 6 items, 37 variants, 37 images, 2 warehouses, 2 suppliers, 51 stock
rows, 2 purchase orders, 10 purchase-order lines.

Row counts, everything in one result set:

```sql
SELECT 'items' AS entity, count(*) FROM example_items
UNION ALL SELECT 'variants', count(*) FROM example_item_variants
UNION ALL SELECT 'variant_images', count(*) FROM example_variant_images
UNION ALL SELECT 'warehouses', count(*) FROM example_warehouses
UNION ALL SELECT 'suppliers', count(*) FROM example_suppliers
UNION ALL SELECT 'stock_levels', count(*) FROM example_stock_levels
UNION ALL SELECT 'purchase_orders', count(*) FROM example_purchase_orders
UNION ALL SELECT 'purchase_order_lines', count(*) FROM example_purchase_order_lines
ORDER BY entity;
```

Variants and images per item, which also shows the cartesian product is complete:

```sql
SELECT i.item_id,
       count(DISTINCT v.variant_id) AS variants,
       count(DISTINCT im.variant_id) AS images
FROM example_items i
LEFT JOIN example_item_variants v ON v.item_id = i.item_id
LEFT JOIN example_variant_images im ON im.variant_id = v.variant_id
GROUP BY i.item_id
ORDER BY i.item_id;
```

The three checks that must come back empty, i.e. zero rows:

```sql
-- a variant with no image, or more than one image
SELECT v.variant_id, count(im.variant_id) AS images
FROM example_item_variants v
LEFT JOIN example_variant_images im ON im.variant_id = v.variant_id
GROUP BY v.variant_id
HAVING count(im.variant_id) <> 1;

-- an image that is not an Unsplash CDN link, or whose source is not unsplash.com
SELECT variant_id, image_url, source_url
FROM example_variant_images
WHERE image_url NOT LIKE 'https://images.unsplash.com/%'
   OR source_url <> 'https://unsplash.com/'
   OR btrim(alt_text) = '';

-- a variant id that is not <ITEM SKU>-<COLOR>-<SIZE>, or a sku that is not its
-- variant id, i.e. an identity the atlas-ecom example seed could not join on
SELECT v.variant_id, v.sku
FROM example_item_variants v
JOIN example_items i ON i.item_id = v.item_id
WHERE v.sku <> v.variant_id
   OR v.variant_id <> i.sku
       || '-' || upper(regexp_replace(v.color, '[^A-Za-z0-9]', '', 'g'))
       || '-' || upper(regexp_replace(v.size,  '[^A-Za-z0-9]', '', 'g'));
```

Distinct images actually in use, to confirm the 13 shared photo IDs:

```sql
SELECT split_part(split_part(image_url, '/photo-', 2), '?', 1) AS photo_id,
       count(*) AS variants
FROM example_variant_images
GROUP BY 1
ORDER BY 2 DESC, 1;
```

Stock and purchasing, to eyeball that the data is plausible:

```sql
SELECT w.code, count(*) AS stock_rows,
       sum(s.on_hand) AS units_on_hand,
       sum(s.reserved) AS units_reserved
FROM example_stock_levels s
JOIN example_warehouses w ON w.warehouse_id = s.warehouse_id
GROUP BY w.code
ORDER BY w.code;

SELECT po.po_number, po.status, sup.name AS supplier, count(l.line_no) AS lines,
       sum(l.quantity * l.unit_cost_cents) / 100.0 AS order_value
FROM example_purchase_orders po
JOIN example_suppliers sup ON sup.supplier_id = po.supplier_id
LEFT JOIN example_purchase_order_lines l
  ON l.purchase_order_id = po.purchase_order_id
GROUP BY po.po_number, po.status, sup.name
ORDER BY po.po_number;
```

## Not in scope

No `atlas_erp` production schema, no migration tool, no Docker or dependency
changes, and no code that reads these tables. The only table the application
itself owns today is `connected_sale_commands`, created by
`atlas_erp.sale_store`. Wiring the real ERP state store to `db/` is a later
decision, not part of this dataset.
