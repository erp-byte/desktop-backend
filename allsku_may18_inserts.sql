-- ═══════════════════════════════════════════════════════════════
-- New SKUs from GST Rate Setup CFPL & CDPL 18th May.xlsx
-- Case-insensitive match against all_sku.particulars
-- 23 rows to insert
-- ═══════════════════════════════════════════════════════════════

BEGIN;

INSERT INTO all_sku (particulars, item_type, item_group, sub_group, uom, sale_group, gst, created_at) VALUES ('PM24-Noice Date Bites 21gm Pouch Roll 150x95mm', 'pm', 'Packaging', 'Roll', 0.0, 'PM', 18.0, NOW());
INSERT INTO all_sku (particulars, item_type, item_group, sub_group, uom, sale_group, gst, created_at) VALUES ('PM24-Rhine Valley Almond & Cranberry Bar Pouch Roll 35gm', 'pm', 'Packaging', 'Pouch Roll', 0.0, 'PM', 18.0, NOW());
INSERT INTO all_sku (particulars, item_type, item_group, sub_group, uom, sale_group, gst, created_at) VALUES ('PM24-Rhine Valley Almond & Orange D Chocolate Date Bar Pouch Roll 35gm', 'pm', 'Packaging', 'Pouch Roll', 0.0, 'PM', 18.0, NOW());
INSERT INTO all_sku (particulars, item_type, item_group, sub_group, uom, sale_group, gst, created_at) VALUES ('PM24-Rhine Valley Barbeque Trail Mix 60gm Pouch Digital', 'pm', 'Packaging', 'Pouch Roll', 0.0, 'PM', 18.0, NOW());
INSERT INTO all_sku (particulars, item_type, item_group, sub_group, uom, sale_group, gst, created_at) VALUES ('PM24-Rhine Valley Black Pepper Trail Mix 60gm Pouch Digital', 'pm', 'Packaging', 'Pouch Roll', 0.0, 'PM', 18.0, NOW());
INSERT INTO all_sku (particulars, item_type, item_group, sub_group, uom, sale_group, gst, created_at) VALUES ('PM24-Rhine Valley Fruit & Nut Bar Pouch Roll 35gm', 'pm', 'Packaging', 'Pouch Roll', 0.0, 'PM', 18.0, NOW());
INSERT INTO all_sku (particulars, item_type, item_group, sub_group, uom, sale_group, gst, created_at) VALUES ('PM24-Rhine Valley Himalayan Pink Salt Trail Mix 60gm Pouch Digital', 'pm', 'Packaging', 'Pouch Roll', 0.0, 'PM', 18.0, NOW());
INSERT INTO all_sku (particulars, item_type, item_group, sub_group, uom, sale_group, gst, created_at) VALUES ('PM24-Rhine Valley Peri Peri Trail Mix 60gm Pouch Digital', 'pm', 'Packaging', 'Pouch Roll', 0.0, 'PM', 18.0, NOW());
INSERT INTO all_sku (particulars, item_type, item_group, sub_group, uom, sale_group, gst, created_at) VALUES ('PM24-Rhine Valley Raspberry, Almond & D Chocolate Bar Pouch Roll 35gm', 'pm', 'Packaging', 'Pouch Roll', 0.0, 'PM', 18.0, NOW());
INSERT INTO all_sku (particulars, item_type, item_group, sub_group, uom, sale_group, gst, created_at) VALUES ('Thermo. Bottom Web Unprinted 323 mm X 200 Mic', 'pm', 'Packaging', 'Bottom', 0.0, 'PM', 18.0, NOW());
INSERT INTO all_sku (particulars, item_type, item_group, sub_group, uom, sale_group, gst, created_at) VALUES ('Thermo. Top Web Unprinted 312mm X 80 mic', 'pm', 'Packaging', 'Top', 0.0, 'PM', 18.0, NOW());
INSERT INTO all_sku (particulars, item_type, item_group, sub_group, uom, sale_group, gst, created_at) VALUES ('Carnival Black Currant 250 Gm', 'fg', 'BLACKCURRANT', 'Dried Blackcurrant', 0.25, 'RPC', 5.0, NOW());
INSERT INTO all_sku (particulars, item_type, item_group, sub_group, uom, sale_group, gst, created_at) VALUES ('Cashew Paste', 'rm', 'CASHEW', 'Cashew - Paste', 1.0, 'Bulk', 5.0, NOW());
INSERT INTO all_sku (particulars, item_type, item_group, sub_group, uom, sale_group, gst, created_at) VALUES ('Coated Peanuts Pudina Patakha Bulk', 'fg', 'PEANUTS', 'Peanut - Coated', 1.0, 'VA', 5.0, NOW());
INSERT INTO all_sku (particulars, item_type, item_group, sub_group, uom, sale_group, gst, created_at) VALUES ('Cococart Almond & Cranberry Date Bar 35gm', 'fg', 'BARS & CEREALS', 'Energy Bars', 0.035, 'VA', 5.0, NOW());
INSERT INTO all_sku (particulars, item_type, item_group, sub_group, uom, sale_group, gst, created_at) VALUES ('Cococart Almond & Orange Dark Choco Date Bar 35gm', 'fg', 'BARS & CEREALS', 'Energy Bars', 0.035, 'VA', 5.0, NOW());
INSERT INTO all_sku (particulars, item_type, item_group, sub_group, uom, sale_group, gst, created_at) VALUES ('Cococart Fruit & Nut Date Bar 35gm', 'fg', 'BARS & CEREALS', 'Energy Bars', 0.035, 'VA', 5.0, NOW());
INSERT INTO all_sku (particulars, item_type, item_group, sub_group, uom, sale_group, gst, created_at) VALUES ('Cococart Himalayan Salt Trail Mix 60gm', 'fg', 'TRAIL MIX', 'Trail Mix', 0.06, 'VA', 5.0, NOW());
INSERT INTO all_sku (particulars, item_type, item_group, sub_group, uom, sale_group, gst, created_at) VALUES ('Cococart Raspberry Almond Dark Choco Date Bar 35gm', 'fg', 'BARS & CEREALS', 'Energy Bars', 0.035, 'VA', 5.0, NOW());
INSERT INTO all_sku (particulars, item_type, item_group, sub_group, uom, sale_group, gst, created_at) VALUES ('Dark Chocolate Coated Almond Stuffed Khalas(No Use)', 'fg', 'TRAIL MIX', 'Trail Mix', 0.011, 'VA', 5.0, NOW());
INSERT INTO all_sku (particulars, item_type, item_group, sub_group, uom, sale_group, gst, created_at) VALUES ('Dark Chocolate Coated Cashew Stuffed Khalas(No Use)', 'fg', 'TRAIL MIX', 'Trail Mix', 0.011, 'VA', 5.0, NOW());
INSERT INTO all_sku (particulars, item_type, item_group, sub_group, uom, sale_group, gst, created_at) VALUES ('Habit Hazelnut Spread', 'rm', 'MISCELLANEOUS - RM', 'Spread', 1.0, 'Bulk', 5.0, NOW());
INSERT INTO all_sku (particulars, item_type, item_group, sub_group, uom, sale_group, gst, created_at) VALUES ('Noice Date Bites 18gm', 'fg', 'BARS & CEREALS', 'Bites', 0.018, 'VA', 5.0, NOW());

COMMIT;
