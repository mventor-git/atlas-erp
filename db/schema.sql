-- Atlas ERP example catalog schema.
--
-- This is example data for local development and the connected-peer demos, not
-- the production migration history. Every object is prefixed `example_` so it
-- can never be confused with real ERP masterdata, every statement is
-- idempotent, and every money column is an integer count of minor units
-- (cents).
--
-- Load order and verification queries: db/README.md

CREATE TABLE IF NOT EXISTS example_items (
    item_id     text    NOT NULL PRIMARY KEY,
    sku         text    NOT NULL UNIQUE,
    name        text    NOT NULL,
    brand       text    NOT NULL,
    category    text    NOT NULL,
    description text    NOT NULL,
    price_cents integer NOT NULL CHECK (price_cents > 0)
);

-- `variant_id` is the deterministic composite `<ITEM SKU>-<COLOR>-<SIZE>`, and the
-- variant `sku` is that same string, so the CHECK keeps the two from drifting
-- apart. Both values are the shared catalog identity with the sibling
-- atlas-ecom example seed, which carries the identical value in
-- `product_variants.variant_id`; that is what lets the two example databases be
-- joined or diffed on id instead of on name or price.
CREATE TABLE IF NOT EXISTS example_item_variants (
    variant_id text NOT NULL PRIMARY KEY,
    item_id    text NOT NULL REFERENCES example_items (item_id) ON DELETE CASCADE,
    sku        text NOT NULL UNIQUE,
    color      text NOT NULL CHECK (btrim(color) <> ''),
    size       text NOT NULL CHECK (btrim(size) <> ''),
    CHECK (sku = variant_id),
    UNIQUE (item_id, color, size)
);

-- Exactly one image per variant: the primary key is the variant, so a second
-- image for the same variant cannot be stored. Both URLs are pinned to the
-- Unsplash CDN and its source page so example data can never drift onto a
-- third-party or local asset host.
CREATE TABLE IF NOT EXISTS example_variant_images (
    variant_id text NOT NULL PRIMARY KEY
        REFERENCES example_item_variants (variant_id) ON DELETE CASCADE,
    image_url  text NOT NULL CHECK (image_url LIKE 'https://images.unsplash.com/%'),
    source_url text NOT NULL CHECK (source_url = 'https://unsplash.com/'),
    alt_text   text NOT NULL CHECK (btrim(alt_text) <> '')
);

CREATE TABLE IF NOT EXISTS example_warehouses (
    warehouse_id text NOT NULL PRIMARY KEY,
    code         text NOT NULL UNIQUE,
    name         text NOT NULL,
    city         text NOT NULL,
    country_code text NOT NULL CHECK (country_code ~ '^[A-Z]{2}$')
);

CREATE TABLE IF NOT EXISTS example_suppliers (
    supplier_id    text NOT NULL PRIMARY KEY,
    code           text NOT NULL UNIQUE,
    name           text NOT NULL,
    contact_email  text NOT NULL,
    lead_time_days integer NOT NULL CHECK (lead_time_days > 0)
);

CREATE TABLE IF NOT EXISTS example_stock_levels (
    variant_id   text    NOT NULL
        REFERENCES example_item_variants (variant_id) ON DELETE CASCADE,
    warehouse_id text    NOT NULL
        REFERENCES example_warehouses (warehouse_id) ON DELETE CASCADE,
    on_hand      integer NOT NULL CHECK (on_hand >= 0),
    reserved     integer NOT NULL DEFAULT 0 CHECK (reserved >= 0),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (variant_id, warehouse_id),
    CHECK (reserved <= on_hand)
);

CREATE TABLE IF NOT EXISTS example_purchase_orders (
    purchase_order_id text    NOT NULL PRIMARY KEY,
    po_number         text    NOT NULL UNIQUE,
    supplier_id       text    NOT NULL REFERENCES example_suppliers (supplier_id),
    warehouse_id      text    NOT NULL REFERENCES example_warehouses (warehouse_id),
    status            text    NOT NULL
        CHECK (status IN ('draft', 'open', 'received', 'cancelled')),
    ordered_at        date    NOT NULL,
    expected_at       date,
    CHECK (expected_at IS NULL OR expected_at >= ordered_at)
);

CREATE TABLE IF NOT EXISTS example_purchase_order_lines (
    purchase_order_id text    NOT NULL
        REFERENCES example_purchase_orders (purchase_order_id) ON DELETE CASCADE,
    line_no           integer NOT NULL CHECK (line_no > 0),
    variant_id        text    NOT NULL
        REFERENCES example_item_variants (variant_id),
    quantity          integer NOT NULL CHECK (quantity > 0),
    unit_cost_cents   integer NOT NULL CHECK (unit_cost_cents > 0),
    PRIMARY KEY (purchase_order_id, line_no),
    UNIQUE (purchase_order_id, variant_id)
);
