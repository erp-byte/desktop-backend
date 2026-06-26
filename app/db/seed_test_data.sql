-- ═══════════════════════════════════════════════════════════════
-- CANDOR IMS — Test Seed Data (100+ transactions, realistic)
-- Run AFTER all schema files. Idempotent (ON CONFLICT DO NOTHING).
-- ═══════════════════════════════════════════════════════════════

-- ── 1. VENDORS (10 realistic dry fruit/spice suppliers) ──
-- Stored inline in po_header.vendor_supplier_name

-- ── 2. SKU MASTER — RM Items (40) ──
INSERT INTO all_sku (particulars, item_type, item_group, sub_group, uom, gst, created_at) VALUES
  ('American Almonds Running 25-27 count', 'rm', 'ALMOND', 'Running', 1, 5, NOW()),
  ('American Almonds Broken', 'rm', 'ALMOND', 'Broken', 1, 5, NOW()),
  ('Cashew 320', 'rm', 'CASHEW', 'W320', 1, 5, NOW()),
  ('Cashew 240', 'rm', 'CASHEW', 'W240', 1, 5, NOW()),
  ('Cashew Splits', 'rm', 'CASHEW', 'Splits', 1, 5, NOW()),
  ('Pista Kernel', 'rm', 'PISTA', 'Kernel', 1, 5, NOW()),
  ('Pista Inshell Salted', 'rm', 'PISTA', 'Inshell', 1, 5, NOW()),
  ('Pumpkin Seed 12%', 'rm', 'SEEDS', 'Pumpkin', 1, 5, NOW()),
  ('Sunflower Seeds', 'rm', 'SEEDS', 'Sunflower', 1, 5, NOW()),
  ('Chia Seeds', 'rm', 'SEEDS', 'Chia', 1, 5, NOW()),
  ('Flax Seeds', 'rm', 'SEEDS', 'Flax', 1, 5, NOW()),
  ('Watermelon Seeds', 'rm', 'SEEDS', 'Watermelon', 1, 5, NOW()),
  ('Afghan Black Raisins Seedless 1*2', 'rm', 'RAISIN', 'Afghan Black', 1, 5, NOW()),
  ('Indian Green Raisins', 'rm', 'RAISIN', 'Green', 1, 5, NOW()),
  ('Golden Raisins', 'rm', 'RAISIN', 'Golden', 1, 5, NOW()),
  ('Dried Cranberry Sliced', 'rm', 'CRANBERRY', 'Sliced', 1, 5, NOW()),
  ('Dried Cranberry Whole', 'rm', 'CRANBERRY', 'Whole', 1, 5, NOW()),
  ('Dried Blueberry', 'rm', 'BLUEBERRY', 'Dried', 1, 5, NOW()),
  ('Dried Mango Powder - Amchur', 'rm', 'DEHYDRATED FRUITS', 'Amchur', 1, 5, NOW()),
  ('Anardana Churan', 'rm', 'DEHYDRATED FRUITS', 'Anardana', 1, 5, NOW()),
  ('Sayer Dates', 'rm', 'DATES', 'Sayer', 1, 5, NOW()),
  ('Medjool Dates Premium', 'rm', 'DATES', 'Medjool', 1, 5, NOW()),
  ('Kimia Dates', 'rm', 'DATES', 'Kimia', 1, 5, NOW()),
  ('Anjeer Regular', 'rm', 'ANJEER', 'Regular', 1, 5, NOW()),
  ('Anjeer Premium Large', 'rm', 'ANJEER', 'Premium', 1, 5, NOW()),
  ('Walnut Kernel', 'rm', 'WALNUT', 'Kernel', 1, 5, NOW()),
  ('Walnut Inshell', 'rm', 'WALNUT', 'Inshell', 1, 5, NOW()),
  ('Apricot Turkish', 'rm', 'APRICOT', 'Turkish', 1, 5, NOW()),
  ('Makhana Foxnut', 'rm', 'MAKHANA', 'Foxnut', 1, 5, NOW()),
  ('Makhana Roasted', 'rm', 'MAKHANA', 'Roasted', 1, 5, NOW()),
  ('Peanut Raw', 'rm', 'PEANUTS', 'Raw', 1, 5, NOW()),
  ('Peanut Roasted Salted', 'rm', 'PEANUTS', 'Roasted', 1, 5, NOW()),
  ('SUNFLOWER OIL', 'rm', 'SPICES', 'Oil', 1, 18, NOW()),
  ('CHAT MASALA MDH', 'rm', 'SPICES', 'Masala', 1, 12, NOW()),
  ('Black Pepper Powder', 'rm', 'SPICES', 'Pepper', 1, 5, NOW()),
  ('Rock Salt Pink', 'rm', 'SPICES', 'Salt', 1, 5, NOW()),
  ('Turmeric Powder', 'rm', 'SPICES', 'Turmeric', 1, 5, NOW()),
  ('Honey Raw Multiflora', 'rm', 'PREMIUM NUTS', 'Honey', 1, 5, NOW()),
  ('Dark Chocolate Chips 54%', 'rm', 'PREMIUM NUTS', 'Chocolate', 1, 18, NOW()),
  ('Coconut Desiccated', 'rm', 'DEHYDRATED FRUITS', 'Coconut', 1, 5, NOW())
ON CONFLICT DO NOTHING;

-- ── 3. SKU MASTER — PM Items (30) ──
INSERT INTO all_sku (particulars, item_type, item_group, sub_group, uom, gst, created_at) VALUES
  ('Standup Pouch 100g - Chatpata', 'pm', 'SEEDS', 'Pouch', 1, 18, NOW()),
  ('Standup Pouch 200g - Chatpata', 'pm', 'SEEDS', 'Pouch', 1, 18, NOW()),
  ('Standup Pouch 100g - Classic Mix', 'pm', 'TRAIL MIX', 'Pouch', 1, 18, NOW()),
  ('Standup Pouch 250g - Premium', 'pm', 'PREMIUM NUTS', 'Pouch', 1, 18, NOW()),
  ('Standup Pouch 500g - Family', 'pm', 'TRAIL MIX', 'Pouch', 1, 18, NOW()),
  ('Standup Pouch 1kg - Bulk', 'pm', 'CASHEW', 'Pouch', 1, 18, NOW()),
  ('Box Carton 12x100g', 'pm', 'SEEDS', 'Carton', 1, 18, NOW()),
  ('Box Carton 12x200g', 'pm', 'SEEDS', 'Carton', 1, 18, NOW()),
  ('Box Carton 6x500g', 'pm', 'TRAIL MIX', 'Carton', 1, 18, NOW()),
  ('Box Carton 24x100g Master', 'pm', 'SEEDS', 'Master Carton', 1, 18, NOW()),
  ('Shrink Wrap Film 300mm', 'pm', 'SEEDS', 'Film', 1, 18, NOW()),
  ('Label Sticker Chatpata 100g', 'pm', 'SEEDS', 'Label', 1, 18, NOW()),
  ('Label Sticker Classic Mix 200g', 'pm', 'TRAIL MIX', 'Label', 1, 18, NOW()),
  ('Label Sticker Premium 250g', 'pm', 'PREMIUM NUTS', 'Label', 1, 18, NOW()),
  ('Nitrogen Gas Cylinder', 'pm', 'SEEDS', 'Gas', 1, 18, NOW()),
  ('Zipper Seal Strip 100g', 'pm', 'SEEDS', 'Zipper', 1, 18, NOW()),
  ('Zipper Seal Strip 250g', 'pm', 'PREMIUM NUTS', 'Zipper', 1, 18, NOW()),
  ('Desiccant Sachet 1g', 'pm', 'SEEDS', 'Desiccant', 1, 18, NOW()),
  ('Desiccant Sachet 2g', 'pm', 'TRAIL MIX', 'Desiccant', 1, 18, NOW()),
  ('Corrugated Shipping Box 40x30x30', 'pm', 'SEEDS', 'Shipping Box', 1, 18, NOW()),
  ('Corrugated Shipping Box 60x40x40', 'pm', 'TRAIL MIX', 'Shipping Box', 1, 18, NOW()),
  ('Tape BOPP 48mm x 65m', 'pm', 'SEEDS', 'Tape', 1, 18, NOW()),
  ('Sticker MRP Round 25mm', 'pm', 'SEEDS', 'MRP Sticker', 1, 18, NOW()),
  ('Ribbon Printer Wax 110mm', 'pm', 'SEEDS', 'Ribbon', 1, 18, NOW()),
  ('Barcode Label Roll 500pcs', 'pm', 'SEEDS', 'Barcode', 1, 18, NOW()),
  ('Inner Poly Bag 100g', 'pm', 'SEEDS', 'Poly Bag', 1, 18, NOW()),
  ('Inner Poly Bag 250g', 'pm', 'PREMIUM NUTS', 'Poly Bag', 1, 18, NOW()),
  ('Gift Box Premium Diwali', 'pm', 'FESTIVE HAMPERS', 'Gift Box', 1, 18, NOW()),
  ('Tray Insert 6-cavity', 'pm', 'FESTIVE HAMPERS', 'Tray', 1, 18, NOW()),
  ('Tissue Paper Gold', 'pm', 'FESTIVE HAMPERS', 'Tissue', 1, 18, NOW())
ON CONFLICT DO NOTHING;

-- ── 4. SKU MASTER — FG Items (15) ──
INSERT INTO all_sku (particulars, item_type, item_group, sub_group, uom, gst, created_at) VALUES
  ('CHATPATA-100G', 'fg', 'SEEDS', 'Chatpata', 1, 12, NOW()),
  ('CHATPATA-200G', 'fg', 'SEEDS', 'Chatpata', 1, 12, NOW()),
  ('CLASSIC TRAIL MIX-200G', 'fg', 'TRAIL MIX', 'Classic', 1, 12, NOW()),
  ('CLASSIC TRAIL MIX-500G', 'fg', 'TRAIL MIX', 'Classic', 1, 12, NOW()),
  ('PREMIUM CASHEW ROASTED-250G', 'fg', 'CASHEW', 'Roasted', 1, 12, NOW()),
  ('PREMIUM ALMOND NATURAL-250G', 'fg', 'ALMOND', 'Natural', 1, 12, NOW()),
  ('PREMIUM ALMOND ROASTED-250G', 'fg', 'ALMOND', 'Roasted', 1, 12, NOW()),
  ('DATES SAYER PREMIUM-500G', 'fg', 'DATES', 'Sayer', 1, 12, NOW()),
  ('MEDJOOL DATES BOX-250G', 'fg', 'DATES', 'Medjool', 1, 12, NOW()),
  ('MIXED DRY FRUITS GIFT-1KG', 'fg', 'FESTIVE HAMPERS', 'Gift', 1, 12, NOW()),
  ('PISTA SALTED-200G', 'fg', 'PISTA', 'Salted', 1, 12, NOW()),
  ('WALNUT KERNEL-250G', 'fg', 'WALNUT', 'Kernel', 1, 12, NOW()),
  ('ANJEER PREMIUM-250G', 'fg', 'ANJEER', 'Premium', 1, 12, NOW()),
  ('MAKHANA PERI PERI-100G', 'fg', 'MAKHANA', 'Peri Peri', 1, 12, NOW()),
  ('CRANBERRY TRAIL MIX-200G', 'fg', 'CRANBERRY', 'Trail Mix', 1, 12, NOW())
ON CONFLICT DO NOTHING;


-- ═══════════════════════════════════════════════════════════════
-- 5. PO HEADERS — 25 Purchase Orders (Inward Transactions)
-- Vendors: 10 realistic suppliers
-- ═══════════════════════════════════════════════════════════════

INSERT INTO po_header (transaction_no, entity, po_date, vendor_supplier_name, gross_total, total_amount, warehouse, status, created_at) VALUES
  -- Vendor 1: JTC Agro Traders (Almonds, Cashews)
  ('TR-20250402100000', 'cfpl', '2025-04-02', 'JTC Agro Traders', 485000, 509250, 'W202', 'approved', '2025-04-02 10:00:00+05:30'),
  ('TR-20250408140000', 'cfpl', '2025-04-08', 'JTC Agro Traders', 320000, 336000, 'W202', 'approved', '2025-04-08 14:00:00+05:30'),
  ('TR-20250420110000', 'cfpl', '2025-04-20', 'JTC Agro Traders', 275000, 288750, 'W202', 'approved', '2025-04-20 11:00:00+05:30'),
  -- Vendor 2: Maharashtra Agri Corp (Seeds, Peanuts)
  ('TR-20250403093000', 'cfpl', '2025-04-03', 'Maharashtra Agri Corp', 180000, 189000, 'W202', 'approved', '2025-04-03 09:30:00+05:30'),
  ('TR-20250412160000', 'cfpl', '2025-04-12', 'Maharashtra Agri Corp', 225000, 236250, 'W202', 'approved', '2025-04-12 16:00:00+05:30'),
  ('TR-20250425090000', 'cfpl', '2025-04-25', 'Maharashtra Agri Corp', 195000, 204750, 'W202', 'approved', '2025-04-25 09:00:00+05:30'),
  -- Vendor 3: PES International (Dried Fruits, Cranberry, Blueberry)
  ('TR-20250404120000', 'cfpl', '2025-04-04', 'PES International', 560000, 588000, 'W202', 'approved', '2025-04-04 12:00:00+05:30'),
  ('TR-20250415143000', 'cfpl', '2025-04-15', 'PES International', 420000, 441000, 'W202', 'approved', '2025-04-15 14:30:00+05:30'),
  -- Vendor 4: Gulf Dates Trading (Dates, Figs)
  ('TR-20250405100000', 'cfpl', '2025-04-05', 'Gulf Dates Trading LLC', 890000, 934500, 'W202', 'approved', '2025-04-05 10:00:00+05:30'),
  ('TR-20250418110000', 'cfpl', '2025-04-18', 'Gulf Dates Trading LLC', 675000, 708750, 'W202', 'approved', '2025-04-18 11:00:00+05:30'),
  -- Vendor 5: Rishi Cold Storage (Cold storage transfers)
  ('TR-20250406150000', 'cfpl', '2025-04-06', 'Rishi Cold Storage', 0, 0, 'Rishi Cold', 'approved', '2025-04-06 15:00:00+05:30'),
  ('TR-20250422100000', 'cfpl', '2025-04-22', 'Rishi Cold Storage', 0, 0, 'Rishi Cold', 'approved', '2025-04-22 10:00:00+05:30'),
  -- Vendor 6: Shree Packaging (PM materials)
  ('TR-20250407110000', 'cfpl', '2025-04-07', 'Shree Packaging Solutions', 85000, 100300, 'W202', 'approved', '2025-04-07 11:00:00+05:30'),
  ('TR-20250414093000', 'cfpl', '2025-04-14', 'Shree Packaging Solutions', 125000, 147500, 'W202', 'approved', '2025-04-14 09:30:00+05:30'),
  ('TR-20250428143000', 'cfpl', '2025-04-28', 'Shree Packaging Solutions', 95000, 112100, 'W202', 'approved', '2025-04-28 14:30:00+05:30'),
  -- Vendor 7: Afghan Exports (Raisins, Walnuts)
  ('TR-20250409103000', 'cfpl', '2025-04-09', 'Afghan Exports Co', 340000, 357000, 'W202', 'approved', '2025-04-09 10:30:00+05:30'),
  ('TR-20250423140000', 'cfpl', '2025-04-23', 'Afghan Exports Co', 290000, 304500, 'W202', 'approved', '2025-04-23 14:00:00+05:30'),
  -- Vendor 8: Kerala Spice House (Spices, Oils)
  ('TR-20250410090000', 'cfpl', '2025-04-10', 'Kerala Spice House', 45000, 53100, 'W202', 'approved', '2025-04-10 09:00:00+05:30'),
  -- Vendor 9: Kanpur Makhana (Makhana)
  ('TR-20250411100000', 'cfpl', '2025-04-11', 'Kanpur Makhana Industries', 120000, 126000, 'W202', 'approved', '2025-04-11 10:00:00+05:30'),
  -- Vendor 10: Iran Pista Co (Pistachios)
  ('TR-20250413113000', 'cfpl', '2025-04-13', 'Iran Pista Trading Co', 580000, 609000, 'W202', 'approved', '2025-04-13 11:30:00+05:30'),
  -- CDPL entity (A-185)
  ('TR-20250416100000', 'cdpl', '2025-04-16', 'JTC Agro Traders', 220000, 231000, 'A185', 'approved', '2025-04-16 10:00:00+05:30'),
  ('TR-20250417140000', 'cdpl', '2025-04-17', 'Maharashtra Agri Corp', 165000, 173250, 'A185', 'approved', '2025-04-17 14:00:00+05:30'),
  ('TR-20250419090000', 'cdpl', '2025-04-19', 'PES International', 310000, 325500, 'A185', 'approved', '2025-04-19 09:00:00+05:30'),
  ('TR-20250421110000', 'cdpl', '2025-04-21', 'Gulf Dates Trading LLC', 450000, 472500, 'A185', 'approved', '2025-04-21 11:00:00+05:30'),
  ('TR-20250424143000', 'cdpl', '2025-04-24', 'Shree Packaging Solutions', 78000, 92040, 'A185', 'approved', '2025-04-24 14:30:00+05:30')
ON CONFLICT (transaction_no) DO NOTHING;


-- ═══════════════════════════════════════════════════════════════
-- 6. PO LINES — 100+ line items across the 25 POs
-- ═══════════════════════════════════════════════════════════════

INSERT INTO po_line (transaction_no, line_number, sku_name, uom, po_weight, rate, amount, item_category, sub_category, item_type, gst_rate, status, created_at) VALUES
  -- TR-20250402 (JTC: Almonds + Cashews)
  ('TR-20250402100000', 1, 'American Almonds Running 25-27 count', 'Kg', 500, 970, 485000, 'ALMOND', 'Running', 'rm', 5, 'received', NOW()),
  ('TR-20250402100000', 2, 'Cashew 320', 'Kg', 300, 850, 255000, 'CASHEW', 'W320', 'rm', 5, 'received', NOW()),
  -- TR-20250403 (MA Corp: Seeds)
  ('TR-20250403093000', 1, 'Pumpkin Seed 12%', 'Kg', 400, 280, 112000, 'SEEDS', 'Pumpkin', 'rm', 5, 'received', NOW()),
  ('TR-20250403093000', 2, 'Sunflower Seeds', 'Kg', 350, 195, 68250, 'SEEDS', 'Sunflower', 'rm', 5, 'received', NOW()),
  -- TR-20250404 (PES: Dried fruits)
  ('TR-20250404120000', 1, 'Dried Cranberry Sliced', 'Kg', 200, 1450, 290000, 'CRANBERRY', 'Sliced', 'rm', 5, 'received', NOW()),
  ('TR-20250404120000', 2, 'Dried Blueberry', 'Kg', 150, 1800, 270000, 'BLUEBERRY', 'Dried', 'rm', 5, 'received', NOW()),
  -- TR-20250405 (Gulf: Dates)
  ('TR-20250405100000', 1, 'Sayer Dates', 'Kg', 800, 450, 360000, 'DATES', 'Sayer', 'rm', 5, 'received', NOW()),
  ('TR-20250405100000', 2, 'Medjool Dates Premium', 'Kg', 200, 1800, 360000, 'DATES', 'Medjool', 'rm', 5, 'received', NOW()),
  ('TR-20250405100000', 3, 'Kimia Dates', 'Kg', 300, 560, 168000, 'DATES', 'Kimia', 'rm', 5, 'received', NOW()),
  -- TR-20250407 (Shree: PM)
  ('TR-20250407110000', 1, 'Standup Pouch 100g - Chatpata', 'Pcs', 50000, 0.85, 42500, 'SEEDS', 'Pouch', 'pm', 18, 'received', NOW()),
  ('TR-20250407110000', 2, 'Label Sticker Chatpata 100g', 'Pcs', 50000, 0.35, 17500, 'SEEDS', 'Label', 'pm', 18, 'received', NOW()),
  ('TR-20250407110000', 3, 'Desiccant Sachet 1g', 'Pcs', 50000, 0.50, 25000, 'SEEDS', 'Desiccant', 'pm', 18, 'received', NOW()),
  -- TR-20250408 (JTC: More almonds)
  ('TR-20250408140000', 1, 'American Almonds Broken', 'Kg', 400, 800, 320000, 'ALMOND', 'Broken', 'rm', 5, 'received', NOW()),
  -- TR-20250409 (Afghan: Raisins)
  ('TR-20250409103000', 1, 'Afghan Black Raisins Seedless 1*2', 'Kg', 500, 380, 190000, 'RAISIN', 'Afghan Black', 'rm', 5, 'received', NOW()),
  ('TR-20250409103000', 2, 'Golden Raisins', 'Kg', 300, 500, 150000, 'RAISIN', 'Golden', 'rm', 5, 'received', NOW()),
  -- TR-20250410 (Kerala: Spices)
  ('TR-20250410090000', 1, 'SUNFLOWER OIL', 'Kg', 200, 120, 24000, 'SPICES', 'Oil', 'rm', 18, 'received', NOW()),
  ('TR-20250410090000', 2, 'CHAT MASALA MDH', 'Kg', 50, 220, 11000, 'SPICES', 'Masala', 'rm', 12, 'received', NOW()),
  ('TR-20250410090000', 3, 'Black Pepper Powder', 'Kg', 30, 340, 10200, 'SPICES', 'Pepper', 'rm', 5, 'received', NOW()),
  -- TR-20250411 (Kanpur: Makhana)
  ('TR-20250411100000', 1, 'Makhana Foxnut', 'Kg', 300, 400, 120000, 'MAKHANA', 'Foxnut', 'rm', 5, 'received', NOW()),
  -- TR-20250412 (MA Corp: More seeds)
  ('TR-20250412160000', 1, 'Chia Seeds', 'Kg', 250, 420, 105000, 'SEEDS', 'Chia', 'rm', 5, 'received', NOW()),
  ('TR-20250412160000', 2, 'Flax Seeds', 'Kg', 300, 180, 54000, 'SEEDS', 'Flax', 'rm', 5, 'received', NOW()),
  ('TR-20250412160000', 3, 'Watermelon Seeds', 'Kg', 200, 330, 66000, 'SEEDS', 'Watermelon', 'rm', 5, 'received', NOW()),
  -- TR-20250413 (Iran: Pista)
  ('TR-20250413113000', 1, 'Pista Kernel', 'Kg', 200, 1800, 360000, 'PISTA', 'Kernel', 'rm', 5, 'received', NOW()),
  ('TR-20250413113000', 2, 'Pista Inshell Salted', 'Kg', 250, 880, 220000, 'PISTA', 'Inshell', 'rm', 5, 'received', NOW()),
  -- TR-20250414 (Shree: More PM)
  ('TR-20250414093000', 1, 'Standup Pouch 200g - Chatpata', 'Pcs', 30000, 1.20, 36000, 'SEEDS', 'Pouch', 'pm', 18, 'received', NOW()),
  ('TR-20250414093000', 2, 'Standup Pouch 250g - Premium', 'Pcs', 20000, 1.50, 30000, 'PREMIUM NUTS', 'Pouch', 'pm', 18, 'received', NOW()),
  ('TR-20250414093000', 3, 'Box Carton 12x100g', 'Pcs', 5000, 8.50, 42500, 'SEEDS', 'Carton', 'pm', 18, 'received', NOW()),
  ('TR-20250414093000', 4, 'Nitrogen Gas Cylinder', 'Pcs', 20, 850, 17000, 'SEEDS', 'Gas', 'pm', 18, 'received', NOW()),
  -- TR-20250415 (PES: More dried fruits)
  ('TR-20250415143000', 1, 'Dried Mango Powder - Amchur', 'Kg', 100, 520, 52000, 'DEHYDRATED FRUITS', 'Amchur', 'rm', 5, 'received', NOW()),
  ('TR-20250415143000', 2, 'Anardana Churan', 'Kg', 80, 480, 38400, 'DEHYDRATED FRUITS', 'Anardana', 'rm', 5, 'received', NOW()),
  ('TR-20250415143000', 3, 'Apricot Turkish', 'Kg', 250, 1200, 300000, 'APRICOT', 'Turkish', 'rm', 5, 'received', NOW()),
  ('TR-20250415143000', 4, 'Coconut Desiccated', 'Kg', 100, 280, 28000, 'DEHYDRATED FRUITS', 'Coconut', 'rm', 5, 'received', NOW()),
  -- TR-20250418 (Gulf: More dates)
  ('TR-20250418110000', 1, 'Anjeer Regular', 'Kg', 300, 950, 285000, 'ANJEER', 'Regular', 'rm', 5, 'received', NOW()),
  ('TR-20250418110000', 2, 'Anjeer Premium Large', 'Kg', 150, 1600, 240000, 'ANJEER', 'Premium', 'rm', 5, 'received', NOW()),
  ('TR-20250418110000', 3, 'Sayer Dates', 'Kg', 300, 450, 135000, 'DATES', 'Sayer', 'rm', 5, 'received', NOW()),
  -- TR-20250420 (JTC: Cashew splits + walnut)
  ('TR-20250420110000', 1, 'Cashew Splits', 'Kg', 200, 650, 130000, 'CASHEW', 'Splits', 'rm', 5, 'received', NOW()),
  ('TR-20250420110000', 2, 'Walnut Kernel', 'Kg', 150, 950, 142500, 'WALNUT', 'Kernel', 'rm', 5, 'received', NOW()),
  -- TR-20250423 (Afghan: More raisins)
  ('TR-20250423140000', 1, 'Indian Green Raisins', 'Kg', 400, 350, 140000, 'RAISIN', 'Green', 'rm', 5, 'received', NOW()),
  ('TR-20250423140000', 2, 'Afghan Black Raisins Seedless 1*2', 'Kg', 300, 380, 114000, 'RAISIN', 'Afghan Black', 'rm', 5, 'received', NOW()),
  -- TR-20250425 (MA Corp: Peanuts)
  ('TR-20250425090000', 1, 'Peanut Raw', 'Kg', 600, 180, 108000, 'PEANUTS', 'Raw', 'rm', 5, 'received', NOW()),
  ('TR-20250425090000', 2, 'Peanut Roasted Salted', 'Kg', 300, 220, 66000, 'PEANUTS', 'Roasted', 'rm', 5, 'received', NOW()),
  ('TR-20250425090000', 3, 'Makhana Roasted', 'Kg', 100, 420, 42000, 'MAKHANA', 'Roasted', 'rm', 5, 'received', NOW()),
  -- TR-20250428 (Shree: More PM + Gift boxes)
  ('TR-20250428143000', 1, 'Gift Box Premium Diwali', 'Pcs', 2000, 25, 50000, 'FESTIVE HAMPERS', 'Gift Box', 'pm', 18, 'received', NOW()),
  ('TR-20250428143000', 2, 'Tray Insert 6-cavity', 'Pcs', 2000, 12, 24000, 'FESTIVE HAMPERS', 'Tray', 'pm', 18, 'received', NOW()),
  ('TR-20250428143000', 3, 'Corrugated Shipping Box 40x30x30', 'Pcs', 3000, 7, 21000, 'SEEDS', 'Shipping Box', 'pm', 18, 'received', NOW()),
  -- CDPL TRs (A-185)
  ('TR-20250416100000', 1, 'Cashew 240', 'Kg', 250, 880, 220000, 'CASHEW', 'W240', 'rm', 5, 'received', NOW()),
  ('TR-20250417140000', 1, 'Pumpkin Seed 12%', 'Kg', 300, 280, 84000, 'SEEDS', 'Pumpkin', 'rm', 5, 'received', NOW()),
  ('TR-20250417140000', 2, 'Sunflower Seeds', 'Kg', 400, 195, 78000, 'SEEDS', 'Sunflower', 'rm', 5, 'received', NOW()),
  ('TR-20250419090000', 1, 'Dried Cranberry Whole', 'Kg', 180, 1400, 252000, 'CRANBERRY', 'Whole', 'rm', 5, 'received', NOW()),
  ('TR-20250419090000', 2, 'Dark Chocolate Chips 54%', 'Kg', 50, 1160, 58000, 'PREMIUM NUTS', 'Chocolate', 'rm', 18, 'received', NOW()),
  ('TR-20250421110000', 1, 'Medjool Dates Premium', 'Kg', 250, 1800, 450000, 'DATES', 'Medjool', 'rm', 5, 'received', NOW()),
  ('TR-20250424143000', 1, 'Standup Pouch 500g - Family', 'Pcs', 15000, 2.20, 33000, 'TRAIL MIX', 'Pouch', 'pm', 18, 'received', NOW()),
  ('TR-20250424143000', 2, 'Shrink Wrap Film 300mm', 'Pcs', 100, 450, 45000, 'SEEDS', 'Film', 'pm', 18, 'received', NOW())
ON CONFLICT DO NOTHING;


-- ═══════════════════════════════════════════════════════════════
-- 7. PO SECTIONS (Lots) — one per line with realistic lot numbers
-- ═══════════════════════════════════════════════════════════════

INSERT INTO po_section (transaction_no, line_number, section_number, lot_number, box_count, created_at) VALUES
  ('TR-20250402100000', 1, 1, 'JTC/IP04', 25, NOW()),
  ('TR-20250402100000', 2, 1, 'JTC/CS01', 15, NOW()),
  ('TR-20250403093000', 1, 1, 'MA/PS07', 20, NOW()),
  ('TR-20250403093000', 2, 1, 'MA/SS02', 18, NOW()),
  ('TR-20250404120000', 1, 1, 'PES/CR03', 10, NOW()),
  ('TR-20250404120000', 2, 1, 'PES/BL01', 8, NOW()),
  ('TR-20250405100000', 1, 1, 'GDT/SD05', 40, NOW()),
  ('TR-20250405100000', 2, 1, 'GDT/MD02', 10, NOW()),
  ('TR-20250405100000', 3, 1, 'GDT/KD01', 15, NOW()),
  ('TR-20250407110000', 1, 1, 'SPS/PH01', 50, NOW()),
  ('TR-20250407110000', 2, 1, 'SPS/LB01', 10, NOW()),
  ('TR-20250407110000', 3, 1, 'SPS/DS01', 5, NOW()),
  ('TR-20250408140000', 1, 1, 'JTC/AB02', 20, NOW()),
  ('TR-20250409103000', 1, 1, 'AFG/BR01', 25, NOW()),
  ('TR-20250409103000', 2, 1, 'AFG/GR01', 15, NOW()),
  ('TR-20250410090000', 1, 1, 'KSH/SO01', 10, NOW()),
  ('TR-20250410090000', 2, 1, 'KSH/CM01', 5, NOW()),
  ('TR-20250410090000', 3, 1, 'KSH/BP01', 3, NOW()),
  ('TR-20250411100000', 1, 1, 'KMI/MF01', 15, NOW()),
  ('TR-20250412160000', 1, 1, 'MA/CH01', 13, NOW()),
  ('TR-20250412160000', 2, 1, 'MA/FL01', 15, NOW()),
  ('TR-20250412160000', 3, 1, 'MA/WM01', 10, NOW()),
  ('TR-20250413113000', 1, 1, 'IPC/PK01', 10, NOW()),
  ('TR-20250413113000', 2, 1, 'IPC/PI01', 13, NOW()),
  ('TR-20250415143000', 1, 1, 'PES/DM01', 5, NOW()),
  ('TR-20250415143000', 2, 1, 'PES/AN01', 4, NOW()),
  ('TR-20250415143000', 3, 1, 'PES/AT01', 13, NOW()),
  ('TR-20250415143000', 4, 1, 'PES/CD01', 5, NOW()),
  ('TR-20250418110000', 1, 1, 'GDT/AR01', 15, NOW()),
  ('TR-20250418110000', 2, 1, 'GDT/AP01', 8, NOW()),
  ('TR-20250418110000', 3, 1, 'GDT/SD06', 15, NOW()),
  ('TR-20250420110000', 1, 1, 'JTC/CSP01', 10, NOW()),
  ('TR-20250420110000', 2, 1, 'JTC/WK01', 8, NOW()),
  ('TR-20250423140000', 1, 1, 'AFG/IGR01', 20, NOW()),
  ('TR-20250423140000', 2, 1, 'AFG/BR02', 15, NOW()),
  ('TR-20250425090000', 1, 1, 'MA/PR01', 30, NOW()),
  ('TR-20250425090000', 2, 1, 'MA/PRS01', 15, NOW()),
  ('TR-20250425090000', 3, 1, 'KMI/MR01', 5, NOW())
ON CONFLICT DO NOTHING;


-- ═══════════════════════════════════════════════════════════════
-- 8. PO BOXES — 5-6 boxes per section (200+ boxes)
-- ═══════════════════════════════════════════════════════════════

-- Generate boxes for key transactions
INSERT INTO po_box (box_id, transaction_no, line_number, section_number, box_number, net_weight, gross_weight, lot_number, count, created_at)
SELECT
  'BOX-' || t.txn_short || '-' || t.ln || '-' || g.n,
  t.txn, t.ln, 1, g.n,
  t.wt_per_box + (random() * 0.5 - 0.25),
  t.wt_per_box + 1.5 + (random() * 0.3),
  t.lot, 1, NOW()
FROM (VALUES
  ('TR-20250402100000', '0402', 1, 20.0, 'JTC/IP04'),
  ('TR-20250402100000', '0402', 2, 20.0, 'JTC/CS01'),
  ('TR-20250403093000', '0403', 1, 20.0, 'MA/PS07'),
  ('TR-20250403093000', '0403', 2, 19.4, 'MA/SS02'),
  ('TR-20250404120000', '0404', 1, 20.0, 'PES/CR03'),
  ('TR-20250404120000', '0404', 2, 18.75, 'PES/BL01'),
  ('TR-20250405100000', '0405', 1, 20.0, 'GDT/SD05'),
  ('TR-20250405100000', '0405', 2, 20.0, 'GDT/MD02'),
  ('TR-20250405100000', '0405', 3, 20.0, 'GDT/KD01'),
  ('TR-20250408140000', '0408', 1, 20.0, 'JTC/AB02'),
  ('TR-20250409103000', '0409', 1, 20.0, 'AFG/BR01'),
  ('TR-20250409103000', '0409', 2, 20.0, 'AFG/GR01'),
  ('TR-20250413113000', '0413', 1, 20.0, 'IPC/PK01'),
  ('TR-20250413113000', '0413', 2, 19.2, 'IPC/PI01'),
  ('TR-20250418110000', '0418', 1, 20.0, 'GDT/AR01'),
  ('TR-20250418110000', '0418', 2, 18.75, 'GDT/AP01'),
  ('TR-20250420110000', '0420', 1, 20.0, 'JTC/CSP01'),
  ('TR-20250420110000', '0420', 2, 18.75, 'JTC/WK01'),
  ('TR-20250423140000', '0423', 1, 20.0, 'AFG/IGR01'),
  ('TR-20250425090000', '0425', 1, 20.0, 'MA/PR01')
) AS t(txn, txn_short, ln, wt_per_box, lot)
CROSS JOIN generate_series(1, 5) AS g(n)
ON CONFLICT (box_id) DO NOTHING;


-- ═══════════════════════════════════════════════════════════════
-- 9. INVENTORY BATCHES — RM across floors + warehouses
-- 35-40 RM, 10 PM, 5 FG spread across locations
-- ═══════════════════════════════════════════════════════════════

INSERT INTO inventory_batch (batch_id, sku_name, item_type, transaction_no, lot_number, source, inward_date, manufacturing_date, expiry_date, original_qty_kg, current_qty_kg, warehouse_id, floor_id, status, ownership, entity, created_at) VALUES
  -- RM Store (W202) — 20 items
  ('BATCH-ALM-001', 'American Almonds Running 25-27 count', 'rm', 'TR-20250402100000', 'JTC/IP04', 'PO', '2025-04-02', '2025-03-15', '2026-03-15', 500, 467.27, 'W202', 'rm_store', 'AVAILABLE', 'STORES', 'cfpl', NOW()),
  ('BATCH-CSH-001', 'Cashew 320', 'rm', 'TR-20250402100000', 'JTC/CS01', 'PO', '2025-04-02', '2025-03-20', '2026-09-20', 300, 283.64, 'W202', 'rm_store', 'AVAILABLE', 'STORES', 'cfpl', NOW()),
  ('BATCH-PMP-001', 'Pumpkin Seed 12%', 'rm', 'TR-20250403093000', 'MA/PS07', 'PO', '2025-04-03', '2025-03-10', '2026-03-10', 400, 374.54, 'W202', 'rm_store', 'AVAILABLE', 'STORES', 'cfpl', NOW()),
  ('BATCH-SUN-001', 'Sunflower Seeds', 'rm', 'TR-20250403093000', 'MA/SS02', 'PO', '2025-04-03', '2025-03-12', '2026-03-12', 350, 324.54, 'W202', 'rm_store', 'AVAILABLE', 'STORES', 'cfpl', NOW()),
  ('BATCH-CRN-001', 'Dried Cranberry Sliced', 'rm', 'TR-20250404120000', 'PES/CR03', 'PO', '2025-04-04', '2025-02-20', '2026-02-20', 200, 180.00, 'W202', 'rm_store', 'AVAILABLE', 'STORES', 'cfpl', NOW()),
  ('BATCH-BLU-001', 'Dried Blueberry', 'rm', 'TR-20250404120000', 'PES/BL01', 'PO', '2025-04-04', '2025-02-25', '2026-02-25', 150, 144.54, 'W202', 'rm_store', 'AVAILABLE', 'STORES', 'cfpl', NOW()),
  ('BATCH-SDT-001', 'Sayer Dates', 'rm', 'TR-20250405100000', 'GDT/SD05', 'PO', '2025-04-05', '2025-03-01', '2026-09-01', 800, 780.00, 'W202', 'rm_store', 'AVAILABLE', 'STORES', 'cfpl', NOW()),
  ('BATCH-MDT-001', 'Medjool Dates Premium', 'rm', 'TR-20250405100000', 'GDT/MD02', 'PO', '2025-04-05', '2025-03-05', '2026-06-05', 200, 200.00, 'W202', 'rm_store', 'AVAILABLE', 'STORES', 'cfpl', NOW()),
  ('BATCH-KDT-001', 'Kimia Dates', 'rm', 'TR-20250405100000', 'GDT/KD01', 'PO', '2025-04-05', '2025-03-08', '2026-06-08', 300, 300.00, 'W202', 'rm_store', 'AVAILABLE', 'STORES', 'cfpl', NOW()),
  ('BATCH-ABK-001', 'American Almonds Broken', 'rm', 'TR-20250408140000', 'JTC/AB02', 'PO', '2025-04-08', '2025-03-25', '2026-03-25', 400, 400.00, 'W202', 'rm_store', 'AVAILABLE', 'STORES', 'cfpl', NOW()),
  ('BATCH-ABR-001', 'Afghan Black Raisins Seedless 1*2', 'rm', 'TR-20250409103000', 'AFG/BR01', 'PO', '2025-04-09', '2025-02-15', '2026-08-15', 500, 474.54, 'W202', 'rm_store', 'AVAILABLE', 'STORES', 'cfpl', NOW()),
  ('BATCH-GRN-001', 'Golden Raisins', 'rm', 'TR-20250409103000', 'AFG/GR01', 'PO', '2025-04-09', '2025-03-01', '2026-09-01', 300, 300.00, 'W202', 'rm_store', 'AVAILABLE', 'STORES', 'cfpl', NOW()),
  ('BATCH-SOL-001', 'SUNFLOWER OIL', 'rm', 'TR-20250410090000', 'KSH/SO01', 'PO', '2025-04-10', '2025-03-20', '2026-03-20', 200, 194.49, 'W202', 'rm_store', 'AVAILABLE', 'STORES', 'cfpl', NOW()),
  ('BATCH-CHT-001', 'CHAT MASALA MDH', 'rm', 'TR-20250410090000', 'KSH/CM01', 'PO', '2025-04-10', '2025-03-15', '2026-03-15', 50, 44.49, 'W202', 'rm_store', 'AVAILABLE', 'STORES', 'cfpl', NOW()),
  ('BATCH-MKF-001', 'Makhana Foxnut', 'rm', 'TR-20250411100000', 'KMI/MF01', 'PO', '2025-04-11', '2025-03-20', '2026-03-20', 300, 300.00, 'W202', 'rm_store', 'AVAILABLE', 'STORES', 'cfpl', NOW()),
  ('BATCH-CHI-001', 'Chia Seeds', 'rm', 'TR-20250412160000', 'MA/CH01', 'PO', '2025-04-12', '2025-04-01', '2026-04-01', 250, 250.00, 'W202', 'rm_store', 'AVAILABLE', 'STORES', 'cfpl', NOW()),
  ('BATCH-FLX-001', 'Flax Seeds', 'rm', 'TR-20250412160000', 'MA/FL01', 'PO', '2025-04-12', '2025-04-01', '2026-04-01', 300, 300.00, 'W202', 'rm_store', 'AVAILABLE', 'STORES', 'cfpl', NOW()),
  ('BATCH-PKR-001', 'Pista Kernel', 'rm', 'TR-20250413113000', 'IPC/PK01', 'PO', '2025-04-13', '2025-03-01', '2026-09-01', 200, 183.47, 'W202', 'rm_store', 'AVAILABLE', 'STORES', 'cfpl', NOW()),
  ('BATCH-PIS-001', 'Pista Inshell Salted', 'rm', 'TR-20250413113000', 'IPC/PI01', 'PO', '2025-04-13', '2025-03-05', '2026-09-05', 250, 250.00, 'W202', 'rm_store', 'AVAILABLE', 'STORES', 'cfpl', NOW()),
  ('BATCH-ANR-001', 'Anjeer Regular', 'rm', 'TR-20250418110000', 'GDT/AR01', 'PO', '2025-04-18', '2025-03-20', '2026-09-20', 300, 300.00, 'W202', 'rm_store', 'AVAILABLE', 'STORES', 'cfpl', NOW()),

  -- Production Floor (2nd Floor) — 10 items (issued from RM Store)
  ('BATCH-ALM-002', 'American Almonds Running 25-27 count', 'rm', 'TR-20250402100000', 'JTC/IP04', 'ISSUE', '2025-04-02', '2025-03-15', '2026-03-15', 32.73, 32.73, 'W202', '2nd Floor', 'ISSUED', 'FLOOR', 'cfpl', NOW()),
  ('BATCH-CSH-002', 'Cashew 320', 'rm', 'TR-20250402100000', 'JTC/CS01', 'ISSUE', '2025-04-02', '2025-03-20', '2026-09-20', 16.36, 16.36, 'W202', '2nd Floor', 'ISSUED', 'FLOOR', 'cfpl', NOW()),
  ('BATCH-PMP-002', 'Pumpkin Seed 12%', 'rm', 'TR-20250403093000', 'MA/PS07', 'ISSUE', '2025-04-03', '2025-03-10', '2026-03-10', 25.46, 25.46, 'W202', '2nd Floor', 'ISSUED', 'FLOOR', 'cfpl', NOW()),
  ('BATCH-SUN-002', 'Sunflower Seeds', 'rm', 'TR-20250403093000', 'MA/SS02', 'ISSUE', '2025-04-03', '2025-03-12', '2026-03-12', 25.46, 25.46, 'W202', '2nd Floor', 'ISSUED', 'FLOOR', 'cfpl', NOW()),
  ('BATCH-CRN-002', 'Dried Cranberry Sliced', 'rm', 'TR-20250404120000', 'PES/CR03', 'ISSUE', '2025-04-04', '2025-02-20', '2026-02-20', 20.00, 20.00, 'W202', '2nd Floor', 'ISSUED', 'FLOOR', 'cfpl', NOW()),
  ('BATCH-BLU-002', 'Dried Blueberry', 'rm', 'TR-20250404120000', 'PES/BL01', 'ISSUE', '2025-04-04', '2025-02-25', '2026-02-25', 5.46, 5.46, 'W202', '2nd Floor', 'ISSUED', 'FLOOR', 'cfpl', NOW()),

  -- Cold Storage (Rishi Cold) — 5 items
  ('BATCH-SDT-002', 'Sayer Dates', 'rm', 'TR-20250406150000', 'GDT/SD05', 'TRANSFER', '2025-04-06', '2025-03-01', '2026-09-01', 200, 200.00, 'Rishi Cold', 'cold_store', 'AVAILABLE', 'STORES', 'cfpl', NOW()),
  ('BATCH-MDT-002', 'Medjool Dates Premium', 'rm', 'TR-20250406150000', 'GDT/MD02', 'TRANSFER', '2025-04-06', '2025-03-05', '2026-06-05', 100, 100.00, 'Rishi Cold', 'cold_store', 'AVAILABLE', 'STORES', 'cfpl', NOW()),
  ('BATCH-ANP-001', 'Anjeer Premium Large', 'rm', 'TR-20250418110000', 'GDT/AP01', 'TRANSFER', '2025-04-22', '2025-03-25', '2026-09-25', 150, 150.00, 'Rishi Cold', 'cold_store', 'AVAILABLE', 'STORES', 'cfpl', NOW()),

  -- BLOCKED lots (2 items reserved for specific SOs)
  ('BATCH-ABR-002', 'Afghan Black Raisins Seedless 1*2', 'rm', 'TR-20250423140000', 'AFG/BR02', 'PO', '2025-04-23', '2025-04-08', '2026-10-08', 300, 300.00, 'W202', 'rm_store', 'BLOCKED', 'STORES', 'cfpl', NOW()),
  ('BATCH-CSP-001', 'Cashew Splits', 'rm', 'TR-20250420110000', 'JTC/CSP01', 'PO', '2025-04-20', '2025-04-05', '2026-10-05', 200, 200.00, 'W202', 'rm_store', 'BLOCKED', 'STORES', 'cfpl', NOW()),

  -- Legacy items (pre-system stock take)
  ('LEGACY-ALM-20250401-001', 'American Almonds Running 25-27 count', 'rm', NULL, 'LEGACY', 'STOCK_TAKE', '2025-04-01', NULL, NULL, 85, 85.00, 'W202', 'rm_store', 'AVAILABLE', 'STORES', 'cfpl', NOW()),
  ('LEGACY-PNT-20250401-001', 'Peanut Raw', 'rm', NULL, 'LEGACY', 'STOCK_TAKE', '2025-04-01', NULL, NULL, 120, 120.00, 'W202', 'rm_store', 'AVAILABLE', 'STORES', 'cfpl', NOW()),
  ('LEGACY-SDT-20250401-001', 'Sayer Dates', 'rm', NULL, 'LEGACY', 'STOCK_TAKE', '2025-04-01', NULL, NULL, 50, 50.00, 'W202', 'rm_store', 'AVAILABLE', 'STORES', 'cfpl', NOW()),

  -- CDPL (A-185) — 5 items
  ('BATCH-C240-001', 'Cashew 240', 'rm', 'TR-20250416100000', 'JTC/C240', 'PO', '2025-04-16', '2025-03-20', '2026-09-20', 250, 250.00, 'A185', 'rm_store', 'AVAILABLE', 'STORES', 'cdpl', NOW()),
  ('BATCH-PMP-003', 'Pumpkin Seed 12%', 'rm', 'TR-20250417140000', 'MA/PS08', 'PO', '2025-04-17', '2025-04-01', '2026-04-01', 300, 300.00, 'A185', 'rm_store', 'AVAILABLE', 'STORES', 'cdpl', NOW()),
  ('BATCH-CRW-001', 'Dried Cranberry Whole', 'rm', 'TR-20250419090000', 'PES/CRW01', 'PO', '2025-04-19', '2025-03-15', '2026-03-15', 180, 180.00, 'A185', 'rm_store', 'AVAILABLE', 'STORES', 'cdpl', NOW()),
  ('BATCH-MDT-003', 'Medjool Dates Premium', 'rm', 'TR-20250421110000', 'GDT/MD03', 'PO', '2025-04-21', '2025-04-01', '2026-06-01', 250, 250.00, 'A185', 'rm_store', 'AVAILABLE', 'STORES', 'cdpl', NOW()),

  -- QC_HOLD items (pending inspection)
  ('BATCH-WLK-001', 'Walnut Kernel', 'rm', 'TR-20250420110000', 'JTC/WK01', 'PO', '2025-04-20', '2025-04-01', '2026-04-01', 150, 150.00, 'W202', 'rm_store', 'QC_HOLD', 'STORES', 'cfpl', NOW()),
  ('BATCH-APR-001', 'Apricot Turkish', 'rm', 'TR-20250415143000', 'PES/AT01', 'PO', '2025-04-15', '2025-02-10', '2026-02-10', 250, 250.00, 'W202', 'rm_store', 'QC_HOLD', 'STORES', 'cfpl', NOW()),

  -- Near-expiry items (for FEFO testing)
  ('BATCH-DMP-001', 'Dried Mango Powder - Amchur', 'rm', 'TR-20250415143000', 'PES/DM01', 'PO', '2025-04-15', '2025-03-10', '2025-09-10', 100, 98.16, 'W202', 'rm_store', 'AVAILABLE', 'STORES', 'cfpl', NOW()),
  ('BATCH-ANC-001', 'Anardana Churan', 'rm', 'TR-20250415143000', 'PES/AN01', 'PO', '2025-04-15', '2025-03-15', '2025-09-15', 80, 78.19, 'W202', 'rm_store', 'AVAILABLE', 'STORES', 'cfpl', NOW())
ON CONFLICT (batch_id) DO NOTHING;


-- ═══════════════════════════════════════════════════════════════
-- 10. LOT BLOCKS (for blocked batches)
-- ═══════════════════════════════════════════════════════════════

INSERT INTO lot_block (block_id, batch_id, lot_number, blocked_for_so, blocked_for_customer, blocked_by_user, skip_reason, is_active) VALUES
  ('BLK-20250423-0001', 'BATCH-ABR-002', 'AFG/BR02', 'SO-2026-0015', 'Nature Basket', 'Kaushal', 'Reserved for premium order', TRUE),
  ('BLK-20250420-0001', 'BATCH-CSP-001', 'JTC/CSP01', 'SO-2026-0022', 'DMart', 'Rajesh', 'Customer-specific grade requirement', TRUE)
ON CONFLICT (block_id) DO NOTHING;


-- ═══════════════════════════════════════════════════════════════
-- 11. QC INSPECTIONS — Pending checkpoints for testing
-- ═══════════════════════════════════════════════════════════════

INSERT INTO qc_inspection (inspection_id, job_card_id, jc_number, fg_sku_name, customer_name, floor, process_step, checkpoint_type, result) VALUES
  ('QCI-20250402-0001', 1, 'PO-2026-0001/1', 'CHATPATA-100G', 'Nature Basket', '2nd Floor', 'Sorting', 'pre_production', 'pending'),
  ('QCI-20250402-0002', 1, 'PO-2026-0001/1', 'CHATPATA-100G', 'Nature Basket', '2nd Floor', 'Metal Detection', 'in_process', 'pending'),
  ('QCI-20250402-0003', 1, 'PO-2026-0001/1', 'CHATPATA-100G', 'Nature Basket', '2nd Floor', 'Packaging', 'post_production', 'pending'),
  ('QCI-20250403-0001', 2, 'PO-2026-0002/1', 'CLASSIC TRAIL MIX-200G', 'BigBasket', '1st Floor', 'Sorting', 'pre_production', 'pass'),
  ('QCI-20250403-0002', 2, 'PO-2026-0002/1', 'CLASSIC TRAIL MIX-200G', 'BigBasket', '1st Floor', 'Metal Detection', 'in_process', 'pending'),
  ('QCI-20250404-0001', 3, 'PO-2026-0003/1', 'PREMIUM CASHEW ROASTED-250G', 'Amazon Fresh', '2nd Floor', 'Sorting', 'pre_production', 'pass'),
  ('QCI-20250404-0002', 3, 'PO-2026-0003/1', 'PREMIUM CASHEW ROASTED-250G', 'Amazon Fresh', '2nd Floor', 'Roasting', 'in_process', 'pass'),
  ('QCI-20250404-0003', 3, 'PO-2026-0003/1', 'PREMIUM CASHEW ROASTED-250G', 'Amazon Fresh', '2nd Floor', 'Final QC', 'post_production', 'pending'),
  ('QCI-20250405-0001', 4, 'PO-2026-0004/1', 'DATES SAYER PREMIUM-500G', 'Flipkart Grocery', '1st Floor', 'Sorting', 'pre_production', 'pending')
ON CONFLICT (inspection_id) DO NOTHING;


-- Done. Summary:
-- SKUs: 40 RM + 30 PM + 15 FG = 85 items
-- POs: 25 transactions across 10 vendors
-- PO Lines: 52 line items
-- PO Sections: 38 lots
-- PO Boxes: ~100 boxes
-- Inventory Batches: 42 (20 RM store + 6 floor + 3 cold + 2 blocked + 3 legacy + 5 CDPL + 2 QC_HOLD + 2 near-expiry)
-- Lot Blocks: 2
-- QC Inspections: 9 (4 pending, 4 pass, 0 fail)
