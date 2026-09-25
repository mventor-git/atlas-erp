-- Atlas ERP example catalog seed.
--
-- Idempotent: every statement is an upsert, so this file can be re-run
-- after db/schema.sql to bring an example database back to this exact
-- state. Load it into a database that already has db/schema.sql applied.
--
-- Catalog identity (item_id, variant_id, variant sku) is the shared
-- contract with the sibling atlas-ecom example seed, so both sides can
-- name the same item and variant rows. `variant_id` is
-- `<ITEM SKU>-<COLOR>-<SIZE>` and the variant `sku` is that same string.
-- See db/README.md.

INSERT INTO example_items (item_id, sku, name, brand, category, description, price_cents) VALUES
    ('item-aurora-linen-shirt', 'AUR-LIN-SHIRT', 'Aurora Everyday Linen Shirt', 'Northline', 'Apparel', 'Mid-weight European flax linen shirt with a relaxed collar and shell buttons. Wears well open over a tee or buttoned for the office, and it is pre-washed so the fabric softens further with every wash.', 8900),
    ('item-meridian-court-sneaker', 'MER-COURT-SNEAK', 'Meridian Court Sneaker', 'Meridian', 'Footwear', 'Low-profile leather court sneaker on a vulcanised rubber cup sole. The removable cork-blend footbed and padded collar carry a long day, and the toe box runs slightly narrow, so size up if you are between widths.', 12900),
    ('item-harbor-wool-throw', 'HAR-WOOL-THROW', 'Harbor Wool Throw', 'Hearthline', 'Home', 'Heavy lambswool throw woven in a herringbone finish with hand-twisted fringe. Roughly 130 by 180 cm: warm enough for a sofa arm, light enough to fold into a weekend bag.', 11900),
    ('item-terra-pour-over-set', 'TER-POUR-01', 'Terra Pour-Over Set', 'Kiln & Coast', 'Kitchen', 'Stoneware dripper, 600 ml carafe, and a reusable stainless filter in one set. The cone brews a single large cup up to two mugs, and the carafe goes in the dishwasher.', 6400),
    ('item-solstice-trail-bottle', 'SOL-TRAIL-BTL', 'Solstice Trail Bottle', 'Fieldcraft', 'Outdoors', 'Single-wall stainless bottle with a copper-lined vacuum layer that holds cold drinks cold for a full day. The wide mouth takes ice and a bottle brush, and the cap seals with a quarter turn.', 3200),
    ('item-loom-everyday-tote', 'LOOM-TOTE-01', 'Loom Everyday Tote', 'Loom', 'Bags', 'Organic canvas tote with a reinforced base and short webbing handles. The unlined interior pocket set fits a 15-inch laptop, and the canvas softens and slouches with use.', 7400)
ON CONFLICT (item_id) DO UPDATE
SET sku = EXCLUDED.sku,
    name = EXCLUDED.name,
    brand = EXCLUDED.brand,
    category = EXCLUDED.category,
    description = EXCLUDED.description,
    price_cents = EXCLUDED.price_cents;

INSERT INTO example_item_variants (variant_id, item_id, sku, color, size) VALUES
    ('AUR-LIN-SHIRT-SAND-XS', 'item-aurora-linen-shirt', 'AUR-LIN-SHIRT-SAND-XS', 'Sand', 'XS'),
    ('AUR-LIN-SHIRT-SAND-S', 'item-aurora-linen-shirt', 'AUR-LIN-SHIRT-SAND-S', 'Sand', 'S'),
    ('AUR-LIN-SHIRT-SAND-M', 'item-aurora-linen-shirt', 'AUR-LIN-SHIRT-SAND-M', 'Sand', 'M'),
    ('AUR-LIN-SHIRT-SAND-L', 'item-aurora-linen-shirt', 'AUR-LIN-SHIRT-SAND-L', 'Sand', 'L'),
    ('AUR-LIN-SHIRT-SAND-XL', 'item-aurora-linen-shirt', 'AUR-LIN-SHIRT-SAND-XL', 'Sand', 'XL'),
    ('AUR-LIN-SHIRT-NAVY-XS', 'item-aurora-linen-shirt', 'AUR-LIN-SHIRT-NAVY-XS', 'Navy', 'XS'),
    ('AUR-LIN-SHIRT-NAVY-S', 'item-aurora-linen-shirt', 'AUR-LIN-SHIRT-NAVY-S', 'Navy', 'S'),
    ('AUR-LIN-SHIRT-NAVY-M', 'item-aurora-linen-shirt', 'AUR-LIN-SHIRT-NAVY-M', 'Navy', 'M'),
    ('AUR-LIN-SHIRT-NAVY-L', 'item-aurora-linen-shirt', 'AUR-LIN-SHIRT-NAVY-L', 'Navy', 'L'),
    ('AUR-LIN-SHIRT-NAVY-XL', 'item-aurora-linen-shirt', 'AUR-LIN-SHIRT-NAVY-XL', 'Navy', 'XL'),
    ('AUR-LIN-SHIRT-OLIVE-XS', 'item-aurora-linen-shirt', 'AUR-LIN-SHIRT-OLIVE-XS', 'Olive', 'XS'),
    ('AUR-LIN-SHIRT-OLIVE-S', 'item-aurora-linen-shirt', 'AUR-LIN-SHIRT-OLIVE-S', 'Olive', 'S'),
    ('AUR-LIN-SHIRT-OLIVE-M', 'item-aurora-linen-shirt', 'AUR-LIN-SHIRT-OLIVE-M', 'Olive', 'M'),
    ('AUR-LIN-SHIRT-OLIVE-L', 'item-aurora-linen-shirt', 'AUR-LIN-SHIRT-OLIVE-L', 'Olive', 'L'),
    ('AUR-LIN-SHIRT-OLIVE-XL', 'item-aurora-linen-shirt', 'AUR-LIN-SHIRT-OLIVE-XL', 'Olive', 'XL'),
    ('MER-COURT-SNEAK-CHALK-40', 'item-meridian-court-sneaker', 'MER-COURT-SNEAK-CHALK-40', 'Chalk', '40'),
    ('MER-COURT-SNEAK-CHALK-41', 'item-meridian-court-sneaker', 'MER-COURT-SNEAK-CHALK-41', 'Chalk', '41'),
    ('MER-COURT-SNEAK-CHALK-42', 'item-meridian-court-sneaker', 'MER-COURT-SNEAK-CHALK-42', 'Chalk', '42'),
    ('MER-COURT-SNEAK-CHALK-43', 'item-meridian-court-sneaker', 'MER-COURT-SNEAK-CHALK-43', 'Chalk', '43'),
    ('MER-COURT-SNEAK-CHALK-44', 'item-meridian-court-sneaker', 'MER-COURT-SNEAK-CHALK-44', 'Chalk', '44'),
    ('MER-COURT-SNEAK-CHALK-45', 'item-meridian-court-sneaker', 'MER-COURT-SNEAK-CHALK-45', 'Chalk', '45'),
    ('MER-COURT-SNEAK-INK-40', 'item-meridian-court-sneaker', 'MER-COURT-SNEAK-INK-40', 'Ink', '40'),
    ('MER-COURT-SNEAK-INK-41', 'item-meridian-court-sneaker', 'MER-COURT-SNEAK-INK-41', 'Ink', '41'),
    ('MER-COURT-SNEAK-INK-42', 'item-meridian-court-sneaker', 'MER-COURT-SNEAK-INK-42', 'Ink', '42'),
    ('MER-COURT-SNEAK-INK-43', 'item-meridian-court-sneaker', 'MER-COURT-SNEAK-INK-43', 'Ink', '43'),
    ('MER-COURT-SNEAK-INK-44', 'item-meridian-court-sneaker', 'MER-COURT-SNEAK-INK-44', 'Ink', '44'),
    ('MER-COURT-SNEAK-INK-45', 'item-meridian-court-sneaker', 'MER-COURT-SNEAK-INK-45', 'Ink', '45'),
    ('HAR-WOOL-THROW-FOG-ONESIZE', 'item-harbor-wool-throw', 'HAR-WOOL-THROW-FOG-ONESIZE', 'Fog', 'One Size'),
    ('HAR-WOOL-THROW-RUST-ONESIZE', 'item-harbor-wool-throw', 'HAR-WOOL-THROW-RUST-ONESIZE', 'Rust', 'One Size'),
    ('TER-POUR-01-SAND-1L', 'item-terra-pour-over-set', 'TER-POUR-01-SAND-1L', 'Sand', '1L'),
    ('TER-POUR-01-CLAY-1L', 'item-terra-pour-over-set', 'TER-POUR-01-CLAY-1L', 'Clay', '1L'),
    ('SOL-TRAIL-BTL-GLACIER-500ML', 'item-solstice-trail-bottle', 'SOL-TRAIL-BTL-GLACIER-500ML', 'Glacier', '500ml'),
    ('SOL-TRAIL-BTL-GLACIER-750ML', 'item-solstice-trail-bottle', 'SOL-TRAIL-BTL-GLACIER-750ML', 'Glacier', '750ml'),
    ('SOL-TRAIL-BTL-EMBER-500ML', 'item-solstice-trail-bottle', 'SOL-TRAIL-BTL-EMBER-500ML', 'Ember', '500ml'),
    ('SOL-TRAIL-BTL-EMBER-750ML', 'item-solstice-trail-bottle', 'SOL-TRAIL-BTL-EMBER-750ML', 'Ember', '750ml'),
    ('LOOM-TOTE-01-INK-ONESIZE', 'item-loom-everyday-tote', 'LOOM-TOTE-01-INK-ONESIZE', 'Ink', 'One Size'),
    ('LOOM-TOTE-01-CLAY-ONESIZE', 'item-loom-everyday-tote', 'LOOM-TOTE-01-CLAY-ONESIZE', 'Clay', 'One Size')
ON CONFLICT (variant_id) DO UPDATE
SET item_id = EXCLUDED.item_id,
    sku = EXCLUDED.sku,
    color = EXCLUDED.color,
    size = EXCLUDED.size;

INSERT INTO example_variant_images (variant_id, image_url, source_url, alt_text) VALUES
    ('AUR-LIN-SHIRT-SAND-XS', 'https://images.unsplash.com/photo-1521572163474-6864f9cf17ab?auto=format&fit=crop&w=1200&q=80', 'https://unsplash.com/', 'Aurora Everyday Linen Shirt in Sand, size XS'),
    ('AUR-LIN-SHIRT-SAND-S', 'https://images.unsplash.com/photo-1521572163474-6864f9cf17ab?auto=format&fit=crop&w=1200&q=80', 'https://unsplash.com/', 'Aurora Everyday Linen Shirt in Sand, size S'),
    ('AUR-LIN-SHIRT-SAND-M', 'https://images.unsplash.com/photo-1521572163474-6864f9cf17ab?auto=format&fit=crop&w=1200&q=80', 'https://unsplash.com/', 'Aurora Everyday Linen Shirt in Sand, size M'),
    ('AUR-LIN-SHIRT-SAND-L', 'https://images.unsplash.com/photo-1521572163474-6864f9cf17ab?auto=format&fit=crop&w=1200&q=80', 'https://unsplash.com/', 'Aurora Everyday Linen Shirt in Sand, size L'),
    ('AUR-LIN-SHIRT-SAND-XL', 'https://images.unsplash.com/photo-1521572163474-6864f9cf17ab?auto=format&fit=crop&w=1200&q=80', 'https://unsplash.com/', 'Aurora Everyday Linen Shirt in Sand, size XL'),
    ('AUR-LIN-SHIRT-NAVY-XS', 'https://images.unsplash.com/photo-1596755094514-f87e34085b2c?auto=format&fit=crop&w=1200&q=80', 'https://unsplash.com/', 'Aurora Everyday Linen Shirt in Navy, size XS'),
    ('AUR-LIN-SHIRT-NAVY-S', 'https://images.unsplash.com/photo-1596755094514-f87e34085b2c?auto=format&fit=crop&w=1200&q=80', 'https://unsplash.com/', 'Aurora Everyday Linen Shirt in Navy, size S'),
    ('AUR-LIN-SHIRT-NAVY-M', 'https://images.unsplash.com/photo-1596755094514-f87e34085b2c?auto=format&fit=crop&w=1200&q=80', 'https://unsplash.com/', 'Aurora Everyday Linen Shirt in Navy, size M'),
    ('AUR-LIN-SHIRT-NAVY-L', 'https://images.unsplash.com/photo-1596755094514-f87e34085b2c?auto=format&fit=crop&w=1200&q=80', 'https://unsplash.com/', 'Aurora Everyday Linen Shirt in Navy, size L'),
    ('AUR-LIN-SHIRT-NAVY-XL', 'https://images.unsplash.com/photo-1596755094514-f87e34085b2c?auto=format&fit=crop&w=1200&q=80', 'https://unsplash.com/', 'Aurora Everyday Linen Shirt in Navy, size XL'),
    ('AUR-LIN-SHIRT-OLIVE-XS', 'https://images.unsplash.com/photo-1602810318383-e386cc2a3ccf?auto=format&fit=crop&w=1200&q=80', 'https://unsplash.com/', 'Aurora Everyday Linen Shirt in Olive, size XS'),
    ('AUR-LIN-SHIRT-OLIVE-S', 'https://images.unsplash.com/photo-1602810318383-e386cc2a3ccf?auto=format&fit=crop&w=1200&q=80', 'https://unsplash.com/', 'Aurora Everyday Linen Shirt in Olive, size S'),
    ('AUR-LIN-SHIRT-OLIVE-M', 'https://images.unsplash.com/photo-1602810318383-e386cc2a3ccf?auto=format&fit=crop&w=1200&q=80', 'https://unsplash.com/', 'Aurora Everyday Linen Shirt in Olive, size M'),
    ('AUR-LIN-SHIRT-OLIVE-L', 'https://images.unsplash.com/photo-1602810318383-e386cc2a3ccf?auto=format&fit=crop&w=1200&q=80', 'https://unsplash.com/', 'Aurora Everyday Linen Shirt in Olive, size L'),
    ('AUR-LIN-SHIRT-OLIVE-XL', 'https://images.unsplash.com/photo-1602810318383-e386cc2a3ccf?auto=format&fit=crop&w=1200&q=80', 'https://unsplash.com/', 'Aurora Everyday Linen Shirt in Olive, size XL'),
    ('MER-COURT-SNEAK-CHALK-40', 'https://images.unsplash.com/photo-1595950653106-6c9ebd614d3a?auto=format&fit=crop&w=1200&q=80', 'https://unsplash.com/', 'Meridian Court Sneaker in Chalk, size 40'),
    ('MER-COURT-SNEAK-CHALK-41', 'https://images.unsplash.com/photo-1595950653106-6c9ebd614d3a?auto=format&fit=crop&w=1200&q=80', 'https://unsplash.com/', 'Meridian Court Sneaker in Chalk, size 41'),
    ('MER-COURT-SNEAK-CHALK-42', 'https://images.unsplash.com/photo-1595950653106-6c9ebd614d3a?auto=format&fit=crop&w=1200&q=80', 'https://unsplash.com/', 'Meridian Court Sneaker in Chalk, size 42'),
    ('MER-COURT-SNEAK-CHALK-43', 'https://images.unsplash.com/photo-1595950653106-6c9ebd614d3a?auto=format&fit=crop&w=1200&q=80', 'https://unsplash.com/', 'Meridian Court Sneaker in Chalk, size 43'),
    ('MER-COURT-SNEAK-CHALK-44', 'https://images.unsplash.com/photo-1595950653106-6c9ebd614d3a?auto=format&fit=crop&w=1200&q=80', 'https://unsplash.com/', 'Meridian Court Sneaker in Chalk, size 44'),
    ('MER-COURT-SNEAK-CHALK-45', 'https://images.unsplash.com/photo-1595950653106-6c9ebd614d3a?auto=format&fit=crop&w=1200&q=80', 'https://unsplash.com/', 'Meridian Court Sneaker in Chalk, size 45'),
    ('MER-COURT-SNEAK-INK-40', 'https://images.unsplash.com/photo-1542291026-7eec264c27ff?auto=format&fit=crop&w=1200&q=80', 'https://unsplash.com/', 'Meridian Court Sneaker in Ink, size 40'),
    ('MER-COURT-SNEAK-INK-41', 'https://images.unsplash.com/photo-1542291026-7eec264c27ff?auto=format&fit=crop&w=1200&q=80', 'https://unsplash.com/', 'Meridian Court Sneaker in Ink, size 41'),
    ('MER-COURT-SNEAK-INK-42', 'https://images.unsplash.com/photo-1542291026-7eec264c27ff?auto=format&fit=crop&w=1200&q=80', 'https://unsplash.com/', 'Meridian Court Sneaker in Ink, size 42'),
    ('MER-COURT-SNEAK-INK-43', 'https://images.unsplash.com/photo-1542291026-7eec264c27ff?auto=format&fit=crop&w=1200&q=80', 'https://unsplash.com/', 'Meridian Court Sneaker in Ink, size 43'),
    ('MER-COURT-SNEAK-INK-44', 'https://images.unsplash.com/photo-1542291026-7eec264c27ff?auto=format&fit=crop&w=1200&q=80', 'https://unsplash.com/', 'Meridian Court Sneaker in Ink, size 44'),
    ('MER-COURT-SNEAK-INK-45', 'https://images.unsplash.com/photo-1542291026-7eec264c27ff?auto=format&fit=crop&w=1200&q=80', 'https://unsplash.com/', 'Meridian Court Sneaker in Ink, size 45'),
    ('HAR-WOOL-THROW-FOG-ONESIZE', 'https://images.unsplash.com/photo-1519710164239-da123dc03ef4?auto=format&fit=crop&w=1200&q=80', 'https://unsplash.com/', 'Harbor Wool Throw in Fog, one size'),
    ('HAR-WOOL-THROW-RUST-ONESIZE', 'https://images.unsplash.com/photo-1616486338812-3dadae4b4ace?auto=format&fit=crop&w=1200&q=80', 'https://unsplash.com/', 'Harbor Wool Throw in Rust, one size'),
    ('TER-POUR-01-SAND-1L', 'https://images.unsplash.com/photo-1514432324607-a09d9b4aefdd?auto=format&fit=crop&w=1200&q=80', 'https://unsplash.com/', 'Terra Pour-Over Set in Sand, 1 litre'),
    ('TER-POUR-01-CLAY-1L', 'https://images.unsplash.com/photo-1447933601403-0c6688de566e?auto=format&fit=crop&w=1200&q=80', 'https://unsplash.com/', 'Terra Pour-Over Set in Clay, 1 litre'),
    ('SOL-TRAIL-BTL-GLACIER-500ML', 'https://images.unsplash.com/photo-1602143407151-7111542de6e8?auto=format&fit=crop&w=1200&q=80', 'https://unsplash.com/', 'Solstice Trail Bottle in Glacier, 500 ml'),
    ('SOL-TRAIL-BTL-GLACIER-750ML', 'https://images.unsplash.com/photo-1602143407151-7111542de6e8?auto=format&fit=crop&w=1200&q=80', 'https://unsplash.com/', 'Solstice Trail Bottle in Glacier, 750 ml'),
    ('SOL-TRAIL-BTL-EMBER-500ML', 'https://images.unsplash.com/photo-1523362628745-0c100150b504?auto=format&fit=crop&w=1200&q=80', 'https://unsplash.com/', 'Solstice Trail Bottle in Ember, 500 ml'),
    ('SOL-TRAIL-BTL-EMBER-750ML', 'https://images.unsplash.com/photo-1523362628745-0c100150b504?auto=format&fit=crop&w=1200&q=80', 'https://unsplash.com/', 'Solstice Trail Bottle in Ember, 750 ml'),
    ('LOOM-TOTE-01-INK-ONESIZE', 'https://images.unsplash.com/photo-1594223274512-ad4803739b7c?auto=format&fit=crop&w=1200&q=80', 'https://unsplash.com/', 'Loom Everyday Tote in Ink, one size'),
    ('LOOM-TOTE-01-CLAY-ONESIZE', 'https://images.unsplash.com/photo-1590874103328-eac38a683ce7?auto=format&fit=crop&w=1200&q=80', 'https://unsplash.com/', 'Loom Everyday Tote in Clay, one size')
ON CONFLICT (variant_id) DO UPDATE
SET image_url = EXCLUDED.image_url,
    source_url = EXCLUDED.source_url,
    alt_text = EXCLUDED.alt_text;

INSERT INTO example_warehouses (warehouse_id, code, name, city, country_code) VALUES
    ('wh-ams', 'AMS', 'Amsterdam Fulfilment Centre', 'Amsterdam', 'NL'),
    ('wh-ldn', 'LDN', 'London Fulfilment Centre', 'London', 'GB')
ON CONFLICT (warehouse_id) DO UPDATE
SET code = EXCLUDED.code,
    name = EXCLUDED.name,
    city = EXCLUDED.city,
    country_code = EXCLUDED.country_code;

INSERT INTO example_suppliers (supplier_id, code, name, contact_email, lead_time_days) VALUES
    ('sup-hollowbay', 'HOLLOWBAY', 'Hollow Bay Textile Mill', 'orders@hollowbay-mills.example', 21),
    ('sup-northport', 'NORTHPORT', 'Northport Supply Group', 'purchasing@northport-supply.example', 14)
ON CONFLICT (supplier_id) DO UPDATE
SET code = EXCLUDED.code,
    name = EXCLUDED.name,
    contact_email = EXCLUDED.contact_email,
    lead_time_days = EXCLUDED.lead_time_days;

INSERT INTO example_stock_levels (variant_id, warehouse_id, on_hand, reserved) VALUES
    ('AUR-LIN-SHIRT-SAND-XS', 'wh-ams', 34, 4),
    ('AUR-LIN-SHIRT-SAND-S', 'wh-ams', 61, 7),
    ('AUR-LIN-SHIRT-SAND-M', 'wh-ams', 88, 11),
    ('AUR-LIN-SHIRT-SAND-M', 'wh-ldn', 24, 3),
    ('AUR-LIN-SHIRT-SAND-L', 'wh-ams', 79, 9),
    ('AUR-LIN-SHIRT-SAND-L', 'wh-ldn', 20, 2),
    ('AUR-LIN-SHIRT-SAND-XL', 'wh-ams', 47, 5),
    ('AUR-LIN-SHIRT-NAVY-XS', 'wh-ams', 28, 3),
    ('AUR-LIN-SHIRT-NAVY-S', 'wh-ams', 55, 6),
    ('AUR-LIN-SHIRT-NAVY-M', 'wh-ams', 74, 9),
    ('AUR-LIN-SHIRT-NAVY-M', 'wh-ldn', 18, 2),
    ('AUR-LIN-SHIRT-NAVY-L', 'wh-ams', 66, 8),
    ('AUR-LIN-SHIRT-NAVY-L', 'wh-ldn', 15, 2),
    ('AUR-LIN-SHIRT-NAVY-XL', 'wh-ams', 38, 4),
    ('AUR-LIN-SHIRT-OLIVE-XS', 'wh-ams', 12, 1),
    ('AUR-LIN-SHIRT-OLIVE-S', 'wh-ams', 24, 2),
    ('AUR-LIN-SHIRT-OLIVE-M', 'wh-ams', 33, 4),
    ('AUR-LIN-SHIRT-OLIVE-M', 'wh-ldn', 7, 1),
    ('AUR-LIN-SHIRT-OLIVE-L', 'wh-ams', 21, 2),
    ('AUR-LIN-SHIRT-OLIVE-L', 'wh-ldn', 5, 0),
    ('AUR-LIN-SHIRT-OLIVE-XL', 'wh-ams', 9, 1),
    ('MER-COURT-SNEAK-CHALK-40', 'wh-ams', 22, 2),
    ('MER-COURT-SNEAK-CHALK-41', 'wh-ams', 31, 3),
    ('MER-COURT-SNEAK-CHALK-42', 'wh-ams', 38, 5),
    ('MER-COURT-SNEAK-CHALK-42', 'wh-ldn', 12, 1),
    ('MER-COURT-SNEAK-CHALK-43', 'wh-ams', 35, 4),
    ('MER-COURT-SNEAK-CHALK-43', 'wh-ldn', 9, 1),
    ('MER-COURT-SNEAK-CHALK-44', 'wh-ams', 26, 3),
    ('MER-COURT-SNEAK-CHALK-44', 'wh-ldn', 6, 1),
    ('MER-COURT-SNEAK-CHALK-45', 'wh-ams', 15, 1),
    ('MER-COURT-SNEAK-INK-40', 'wh-ams', 9, 1),
    ('MER-COURT-SNEAK-INK-41', 'wh-ams', 14, 1),
    ('MER-COURT-SNEAK-INK-42', 'wh-ams', 17, 2),
    ('MER-COURT-SNEAK-INK-43', 'wh-ams', 12, 1),
    ('MER-COURT-SNEAK-INK-44', 'wh-ams', 8, 1),
    ('MER-COURT-SNEAK-INK-45', 'wh-ams', 4, 0),
    ('HAR-WOOL-THROW-FOG-ONESIZE', 'wh-ams', 44, 5),
    ('HAR-WOOL-THROW-FOG-ONESIZE', 'wh-ldn', 11, 1),
    ('HAR-WOOL-THROW-RUST-ONESIZE', 'wh-ams', 19, 2),
    ('TER-POUR-01-SAND-1L', 'wh-ams', 52, 6),
    ('TER-POUR-01-SAND-1L', 'wh-ldn', 16, 2),
    ('TER-POUR-01-CLAY-1L', 'wh-ams', 27, 3),
    ('SOL-TRAIL-BTL-GLACIER-500ML', 'wh-ams', 120, 14),
    ('SOL-TRAIL-BTL-GLACIER-500ML', 'wh-ldn', 48, 6),
    ('SOL-TRAIL-BTL-GLACIER-750ML', 'wh-ams', 58, 7),
    ('SOL-TRAIL-BTL-GLACIER-750ML', 'wh-ldn', 22, 3),
    ('SOL-TRAIL-BTL-EMBER-500ML', 'wh-ams', 64, 8),
    ('SOL-TRAIL-BTL-EMBER-750ML', 'wh-ams', 31, 4),
    ('LOOM-TOTE-01-INK-ONESIZE', 'wh-ams', 57, 6),
    ('LOOM-TOTE-01-INK-ONESIZE', 'wh-ldn', 18, 2),
    ('LOOM-TOTE-01-CLAY-ONESIZE', 'wh-ams', 23, 2)
ON CONFLICT (variant_id, warehouse_id) DO UPDATE
SET on_hand = EXCLUDED.on_hand,
    reserved = EXCLUDED.reserved,
    updated_at = now();

INSERT INTO example_purchase_orders (purchase_order_id, po_number, supplier_id, warehouse_id, status, ordered_at, expected_at) VALUES
    ('po-2026-0041', 'PO-2026-0041', 'sup-hollowbay', 'wh-ams', 'received', '2026-08-18', '2026-08-28'),
    ('po-2026-0058', 'PO-2026-0058', 'sup-northport', 'wh-ldn', 'open', '2026-09-08', '2026-09-22')
ON CONFLICT (purchase_order_id) DO UPDATE
SET po_number = EXCLUDED.po_number,
    supplier_id = EXCLUDED.supplier_id,
    warehouse_id = EXCLUDED.warehouse_id,
    status = EXCLUDED.status,
    ordered_at = EXCLUDED.ordered_at,
    expected_at = EXCLUDED.expected_at;

INSERT INTO example_purchase_order_lines (purchase_order_id, line_no, variant_id, quantity, unit_cost_cents) VALUES
    ('po-2026-0041', 1, 'AUR-LIN-SHIRT-SAND-M', 120, 4100),
    ('po-2026-0041', 2, 'AUR-LIN-SHIRT-NAVY-L', 80, 4100),
    ('po-2026-0041', 3, 'AUR-LIN-SHIRT-OLIVE-S', 30, 4100),
    ('po-2026-0041', 4, 'HAR-WOOL-THROW-FOG-ONESIZE', 60, 5900),
    ('po-2026-0041', 5, 'HAR-WOOL-THROW-RUST-ONESIZE', 24, 5900),
    ('po-2026-0058', 1, 'MER-COURT-SNEAK-CHALK-42', 40, 6400),
    ('po-2026-0058', 2, 'MER-COURT-SNEAK-INK-43', 16, 6400),
    ('po-2026-0058', 3, 'TER-POUR-01-SAND-1L', 48, 2700),
    ('po-2026-0058', 4, 'SOL-TRAIL-BTL-GLACIER-500ML', 150, 1350),
    ('po-2026-0058', 5, 'LOOM-TOTE-01-INK-ONESIZE', 60, 3100)
ON CONFLICT (purchase_order_id, line_no) DO UPDATE
SET variant_id = EXCLUDED.variant_id,
    quantity = EXCLUDED.quantity,
    unit_cost_cents = EXCLUDED.unit_cost_cents;
