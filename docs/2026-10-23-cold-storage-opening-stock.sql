-- Savla / Rishi / Eskimo cold storage OPENING STOCK for 23 Oct 2026 -> Stock Take.
-- Source: Savla_Rishi_Inventory_23rd OCt, 2026- Opening stock - - Copy.xlsx,
--         sheet '23rd Oct  26' (report dated 23-10-2026, stock as on 22-10-2026).
--
-- Run once on RDS warehouse_db, as one script (backend/scripts/apply_cold_storage_report.py).
-- It is a single transaction: if any guard fails, nothing is written.
--
-- WHAT IT WRITES (new_stock_entries only, entry_id 'COLD1023'), shaped exactly as COLD1019:
--   1. One COUNT row per lot line of the report, dated 23 Oct 2026 09:00 IST.
--        item_name = Tally Name as the all_sku article (category / sub category / type
--        from all_sku); warehouse = Unit; floor_name = Storage Location; quantity =
--        cartons, unit_uom = kg per box, total_weight = kg.
--        Off Grade/Rejection: ANJEER 1*2 (Anjeer / OFF-GRADE ANJEER); OFF-GRADE DATES (Premium Dates / Medjoul-Large); OFF-GRADE DATES (Premium Dates / Medjoul-Rej).
--   2. A 0 kg COUNT row for every article still showing stock at those five places
--      that is NOT in the report (latest stock is per article per place, so without
--      this an article gone since the last load would keep its old quantity).
--
-- Postings made on 23 Oct or later at these places are still added on top.
--
-- NOT LOADED:
--   * A-185 Cold (120 lines, 57,621.57 kg) - counted on the floor app.
--   * 42 lines at 0 kg (step 2 zeroes any of those articles still showing stock).
--
-- EXPECTED after step 1 (677,847.72 kg):
--   D-39    Savla        153 lines    533,819.78 kg
--   D-514   Savla         71 lines     51,365.34 kg
--   D-514   Savla Bond    17 lines     17,450.00 kg
--   Eskimo  Eskimo         5 lines      9,120.00 kg
--   Rishi   Rishi         31 lines     66,092.60 kg

BEGIN;
SET LOCAL lock_timeout = '10s';

-- Refuse to load twice, or on top of a count already made that day at these
-- places: counts on the same day are SUMMED, so either would double stock.
DO $$
DECLARE n bigint;
BEGIN
    SELECT COUNT(*) INTO n FROM new_stock_entries
     WHERE entry_id = 'COLD1023'
        OR ((created_at AT TIME ZONE 'Asia/Kolkata')::date = DATE '2026-10-23'
            AND (source_kind IS NULL OR source_kind = 'COUNT')
            AND (REPLACE(UPPER(BTRIM(warehouse)), '-', ''), UPPER(BTRIM(COALESCE(floor_name, ''))))
                IN (VALUES ('D39', 'SAVLA'), ('D514', 'SAVLA'), ('D514', 'SAVLA BOND'), ('RISHI', 'RISHI'), ('ESKIMO', 'ESKIMO')));
    IF n > 0 THEN
        RAISE EXCEPTION '% count row(s) already exist for 23 Oct 2026 at the cold stores (or COLD1023 already loaded) - refusing to double-load', n;
    END IF;
END $$;

CREATE TEMP TABLE cold_load (
    xl_row           int     NOT NULL,
    item_name        text    NOT NULL,
    item_type        text,
    item_category    text,
    item_subcategory text,
    warehouse        text    NOT NULL,
    floor_name       text    NOT NULL,
    total_quantity   numeric NOT NULL,
    unit_uom         numeric NOT NULL,
    total_weight     numeric NOT NULL,
    stock_type       text    NOT NULL,
    remark           text
) ON COMMIT DROP;

-- ALIASES applied (Tally Name -> article):
--   'AL BARAKAH FARD DATES PREMIUM A 10KG' -> 'AL BARAKAH FARD DATES PREMIUM - 10KG'
--   'BLUEBERRY - APPLE INFUSED 9.07 KG' -> 'DRIED BLUEBERRY APPLE INFUSED'
--   'FARD DATES SEEDLESS B GRADE' -> 'AL BARAKAH FARD DATES SEEDLESS'
--   'WET DATES SEEDLESS KHALAS(TAJIR RTV)' -> 'WET DATES SEEDLESS KHALAS'
--   'AL BARAKAH CHOPPED DATES 10KG' -> 'AL BARAKAH CHOPPED DATES'
INSERT INTO cold_load VALUES
    (7, 'OFF-GRADE DATES', 'RM', 'DATES', 'OFF-GRADE DATES', 'D-514', 'Savla', 9.00, 5.000, 45.00, 'Off Grade/Rejection', 'lot 79952 | inward 11/02/2025 | CDPL | Premium Dates / Medjoul-Rej | D-514 @ Savla'),
    (8, 'OFF-GRADE DATES', 'RM', 'DATES', 'OFF-GRADE DATES', 'D-514', 'Savla', 4.00, 5.000, 20.00, 'Off Grade/Rejection', 'lot 81382 | inward 19/03/2025 | CDPL | Premium Dates / Medjoul-Rej | D-514 @ Savla'),
    (9, 'WET DATES AJWA', 'RM', 'DATES', 'AJWA', 'D-514', 'Savla', 40.00, 5.000, 200.00, 'Fresh Stock', 'lot 9621 | inward 19/11/2025 | CDPL | Premium Dates / Ajwa | D-514 @ Savla'),
    (10, 'WET DATES (MABROOM)', 'RM', 'DATES', 'MABROOM', 'D-514', 'Savla', 4.00, 5.000, 20.00, 'Fresh Stock', 'lot 10801 | inward 13/12/2025 | CDPL | Premium Dates / Mabroom | D-514 @ Savla'),
    (11, 'WET DATES AJWA', 'RM', 'DATES', 'AJWA', 'D-514', 'Savla', 15.00, 5.000, 75.00, 'Fresh Stock', 'lot 10798 | inward 13/12/2025 | CDPL | Premium Dates / Ajwa | D-514 @ Savla'),
    (12, 'WET DATES AJWA', 'RM', 'DATES', 'AJWA', 'D-514', 'Savla', 406.00, 5.000, 2030.00, 'Fresh Stock', 'lot 12325 | inward 18/01/2026 | CDPL | Premium Dates / Ajwa-SS | D-514 @ Savla'),
    (13, 'WET DATES (MAZAFATI)', 'RM', 'DATES', 'KIMIA', 'D-39', 'Savla', 730.00, 6.512, 4753.76, 'Fresh Stock', 'lot 186079 | inward 30/03/2026 | CDPL | Economical Dates / Mazafati | D-39 @ Savla'),
    (14, 'WET DATES (MAZAFATI)', 'RM', 'DATES', 'KIMIA', 'D-39', 'Savla', 1661.00, 6.770, 11244.97, 'Fresh Stock', 'lot 2154 | inward 11/04/2026 | CDPL | Economical Dates / Mazafati | D-39 @ Savla'),
    (15, 'AL BARAKAH CRUSHED DATES 10KG', 'RM', 'DATES', 'DICED', 'D-39', 'Savla', 748.00, 10.000, 7480.00, 'Fresh Stock', 'lot 6364 | inward 14/05/2026 | CDPL | Dry Dates / Crushed Date | D-39 @ Savla'),
    (16, 'ORGANIC WANAN PREMIUM LARGE', 'RM', 'DATES', 'WANAN', 'D-514', 'Savla', 1.00, 5.000, 5.00, 'Fresh Stock', 'lot 17177 | inward 30/05/2026 | CDPL | Organic Dates / Org Wanan-Large | D-514 @ Savla'),
    (17, 'OFF-GRADE DATES', 'RM', 'DATES', 'OFF-GRADE DATES', 'D-514', 'Savla', 65.00, 5.000, 325.00, 'Off Grade/Rejection', 'lot 17170 | inward 30/05/2026 | CDPL | Premium Dates / Medjoul-Rej | D-514 @ Savla'),
    (18, 'ORGANIC KHIDRI SPECIAL JUMBO', 'RM', 'DATES', 'KHIDRI', 'D-514', 'Savla', 1.00, 5.000, 5.00, 'Fresh Stock', 'lot 17169 | inward 30/05/2026 | CFPL | Organic Dates / Org Khidri-Jumbo | D-514 @ Savla'),
    (19, 'WET DATES (MABROOM)', 'RM', 'DATES', 'MABROOM', 'D-514', 'Savla', 1.00, 5.000, 5.00, 'Fresh Stock', 'lot 17168 | inward 30/05/2026 | CDPL | Premium Dates / Mabroom | D-514 @ Savla'),
    (20, 'WET DATES SAFAVI', 'RM', 'DATES', 'SAFAVI', 'D-39', 'Savla', 256.00, 5.000, 1280.00, 'Fresh Stock', 'lot 13769 | inward 16/07/2026 | CDPL | Premium Dates / Safavi | D-39 @ Savla'),
    (21, '10KG AL BARAKAH DATE POWDER V2', 'RM', 'DATES', 'POWDER', 'D-39', 'Savla', 960.00, 10.000, 9600.00, 'Fresh Stock', 'lot 14570 | inward 23/07/2026 | CDPL | Date Powder / V2 | D-39 @ Savla'),
    (22, '25KG AL BARAKAH DATE SYRUP V1', 'RM', 'DATES', 'SYRUP', 'D-39', 'Savla', 53.00, 25.000, 1325.00, 'Fresh Stock', 'lot 20328 | inward 06/09/2026 | CDPL | Date Syrup /  DATE SYRUP V1 | D-39 @ Savla'),
    (23, 'AL BARAKAH DATES SYRUP 400G', 'FG', 'DATES', 'SYRUP', 'D-39', 'Savla', 42.00, 4.800, 201.60, 'Fresh Stock', 'lot 21023 | inward 11/09/2026 | CDPL | Date Syrup / 400g x 12 ABD | D-39 @ Savla'),
    (24, 'AL BARAKAH DATE PASTE 12.5KG', 'RM', 'DATES', 'PASTE', 'D-39', 'Savla', 95.00, 12.500, 1187.50, 'Fresh Stock', 'lot 22079 | inward 20/09/2026 | CDPL | Date Paste / Date Paste | D-39 @ Savla'),
    (25, 'AL BARAKAH DATE PASTE 12.5KG', 'RM', 'DATES', 'PASTE', 'D-39', 'Savla', 1060.00, 12.500, 13250.00, 'Fresh Stock', 'lot 25075 | inward 08/10/2026 | CDPL | Date Paste / Date Paste | D-39 @ Savla'),
    (26, 'AL BARAKAH DATE PASTE 12.5KG', 'RM', 'DATES', 'PASTE', 'D-39', 'Savla', 114.00, 12.500, 1425.00, 'Fresh Stock', 'lot 25156 | inward 08/10/2026 | CDPL | Date Paste / Date Paste | D-39 @ Savla'),
    (27, 'AL BARAKAH DATE PASTE 12.5KG', 'RM', 'DATES', 'PASTE', 'D-39', 'Savla', 39.00, 12.500, 487.50, 'Fresh Stock', 'lot 25531 | inward 10/10/2026 | CDPL | Date Paste / Date Paste | D-39 @ Savla'),
    (28, 'AL BARAKAH DATE PASTE 12.5KG', 'RM', 'DATES', 'PASTE', 'D-39', 'Savla', 240.00, 12.500, 3000.00, 'Fresh Stock', 'lot 25431 | inward 10/10/2026 | CDPL | Date Paste / Date Paste | D-39 @ Savla'),
    (29, '10KG AL BARAKAH DATE POWDER V1', 'RM', 'DATES', 'POWDER', 'D-39', 'Savla', 50.00, 10.000, 500.00, 'Fresh Stock', 'lot 25689 | inward 11/10/2026 | CDPL | Date Powder / V1 | D-39 @ Savla'),
    (30, 'AL BARAKAH DATE PASTE 12.5KG', 'RM', 'DATES', 'PASTE', 'D-39', 'Savla', 2080.00, 12.500, 26000.00, 'Fresh Stock', 'lot 25872 | inward 11/10/2026 | CDPL | Date Paste / Date Paste | D-39 @ Savla'),
    (31, 'AL BARAKAH DATE PASTE 12.5KG', 'RM', 'DATES', 'PASTE', 'D-39', 'Savla', 2080.00, 12.500, 26000.00, 'Fresh Stock', 'lot 27214 | inward 17/10/2026 | CDPL | Date Paste / Date Paste | D-39 @ Savla'),
    (32, 'AL BARAKAH DATE PASTE 12.5KG', 'RM', 'DATES', 'PASTE', 'D-39', 'Savla', 2080.00, 12.500, 26000.00, 'Fresh Stock', 'lot 27342 | inward 18/10/2026 | CDPL | Date Paste / Date Paste | D-39 @ Savla'),
    (33, 'AL BARAKAH DATE PASTE 12.5KG', 'RM', 'DATES', 'PASTE', 'D-39', 'Savla', 2080.00, 12.500, 26000.00, 'Fresh Stock', 'lot 27351 | inward 18/10/2026 | CDPL | Date Paste / Date Paste | D-39 @ Savla'),
    (34, 'ALDAKA MEDJOOL PREMIUM LARGE', 'RM', 'DATES', 'MEDJOUL-LARGE', 'D-514', 'Savla', 6.00, 5.000, 30.00, 'Fresh Stock', 'lot 76581 | inward 08/11/2024 | CFPL | Organic Dates / Org Medjoul-Lar | D-514 @ Savla'),
    (35, 'INDIAN GREEN RAISINS', 'RM', 'RAISIN', 'RAISIN - INDIAN', 'D-39', 'Savla', 11.00, 15.000, 165.00, 'Fresh Stock', 'lot 130273 | inward 28/01/2025 | CFPL | Raisins / Indian Raisin | D-39 @ Savla'),
    (36, 'PECAN NUTS', 'RM', 'PREMIUM NUTS', 'PECAN NUTS', 'D-39', 'Savla', 11.00, 13.600, 149.60, 'Fresh Stock', 'lot 133878 | inward 27/02/2025 | CFPL | Pecan Nut / Pecan Tukda | D-39 @ Savla'),
    (37, 'PECAN NUTS', 'RM', 'PREMIUM NUTS', 'PECAN NUTS', 'D-39', 'Savla', 5.00, 13.600, 68.00, 'Fresh Stock', 'lot 134478 | inward 04/03/2025 | CFPL | Pecan Nut / Pecan Tukda | D-39 @ Savla'),
    (38, 'PECAN NUTS', 'RM', 'PREMIUM NUTS', 'PECAN NUTS', 'D-39', 'Savla', 4.00, 13.600, 54.40, 'Fresh Stock', 'lot 134856 | inward 07/03/2025 | CFPL | Pecan Nut / Pecan Tukda | D-39 @ Savla'),
    (39, 'PECAN NUTS', 'RM', 'PREMIUM NUTS', 'PECAN NUTS', 'D-39', 'Savla', 5.00, 13.600, 68.00, 'Fresh Stock', 'lot 149845 | inward 18/07/2025 | CFPL | Pecan Nut / Pecan | D-39 @ Savla'),
    (40, 'OFF-GRADE DATES', 'RM', 'DATES', 'OFF-GRADE DATES', 'D-514', 'Savla', 56.00, 5.000, 280.00, 'Off Grade/Rejection', 'lot 4905 | inward 24/07/2025 | CFPL | Premium Dates / Medjoul-Rej | D-514 @ Savla'),
    (41, 'MACADAMIA ROASTED - SIZE 6', 'RM', 'PREMIUM NUTS', 'MACADAMIA NUTS', 'D-39', 'Savla', 1.00, 11.340, 11.34, 'Fresh Stock', 'lot 151709 | inward 04/08/2025 | CFPL | Macadamia / Roasted Style-6 | D-39 @ Savla'),
    (42, 'DEGLET DATES', 'RM', 'DATES', 'DEGLET', 'D-514', 'Savla', 109.00, 5.000, 545.00, 'Fresh Stock', 'lot 7232 | inward 29/09/2025 | CFPL | Premium Dates / DEGLET NOUR | D-514 @ Savla'),
    (43, 'DRIED CRANBERRY WHOLE', 'RM', 'CRANBERRY', 'CRANBERRY - WHOLE', 'D-39', 'Savla', 16.00, 4.540, 72.64, 'Fresh Stock', 'lot 160135 | inward 09/10/2025 | CFPL | Cranberry / Meduri | D-39 @ Savla'),
    (44, 'OFF-GRADE DATES', 'RM', 'DATES', 'OFF-GRADE DATES', 'D-514', 'Savla', 233.00, 5.000, 1165.00, 'Off Grade/Rejection', 'lot 7760 | inward 10/10/2025 | CFPL | Premium Dates / Medjoul-Rej | D-514 @ Savla'),
    (45, 'ALDAKA MEDJOOL PREMIUM JUMBO', 'RM', 'DATES', 'MEDJOUL-JUMBO', 'D-39', 'Savla', 230.00, 5.000, 1150.00, 'Fresh Stock', 'lot 162094 | inward 28/10/2025 | CFPL | Organic Dates / Org Medjoul-Jumbo | D-39 @ Savla'),
    (46, 'ORGANIC SEQEE PREMIUM LARGE', 'RM', 'DATES', 'SEQEE', 'D-514', 'Savla', 23.00, 5.000, 115.00, 'Fresh Stock', 'lot 9498 | inward 15/11/2025 | CFPL | Organic Dates / Org Seqee-Large | D-514 @ Savla'),
    (47, 'ORGANIC WANAN PREMIUM JUMBO', 'RM', 'DATES', 'WANAN', 'D-514', 'Savla', 78.00, 5.000, 390.00, 'Fresh Stock', 'lot 9497 | inward 15/11/2025 | CFPL | Organic Dates / Org Wanan-Jumbo | D-514 @ Savla'),
    (48, 'ORGANIC WANAN PREMIUM LARGE', 'RM', 'DATES', 'WANAN', 'D-514', 'Savla', 45.00, 5.000, 225.00, 'Fresh Stock', 'lot 9496 | inward 15/11/2025 | CFPL | Organic Dates / Org Wanan-Large | D-514 @ Savla'),
    (49, 'ORGANIC KHIDRI SPECIAL JUMBO', 'RM', 'DATES', 'KHIDRI', 'D-514', 'Savla', 67.00, 5.000, 335.00, 'Fresh Stock', 'lot 9495 | inward 15/11/2025 | CFPL | Organic Dates / Org Khidri-Jumbo | D-514 @ Savla'),
    (50, 'SWEET CHILLI MACADAMIA NUT', 'FG', 'PREMIUM NUTS', 'MACADAMIA NUTS', 'D-39', 'Savla', 30.00, 10.000, 300.00, 'Fresh Stock', 'lot 167918 | inward 18/11/2025 | CFPL | Aus Macadamia / Sweet Chilli | D-39 @ Savla'),
    (51, 'MACADAMIA ROASTED - SIZE 1', 'RM', 'PREMIUM NUTS', 'MACADAMIA NUTS', 'D-39', 'Savla', 12.00, 11.340, 136.08, 'Fresh Stock', 'lot 167919 | inward 18/11/2025 | CFPL | Aus Macadamia / Roasted Style-1 | D-39 @ Savla'),
    (52, 'MACADAMIA ROASTED - SIZE 0', 'RM', 'PREMIUM NUTS', 'MACADAMIA NUTS', 'D-39', 'Savla', 88.00, 11.340, 997.92, 'Fresh Stock', 'lot 167920 | inward 18/11/2025 | CFPL | Aus Macadamia / Roasted Style-0 | D-39 @ Savla'),
    (53, 'MACADAMIA DARK CHOCOLATE CLUSTERS', 'FG', 'PREMIUM NUTS', 'MACADAMIA NUTS', 'D-39', 'Savla', 48.00, 7.500, 360.00, 'Fresh Stock', 'lot 167916 | inward 18/11/2025 | CFPL | Aus Macadamia / Dark Choc | D-39 @ Savla'),
    (54, 'MACADAMIA MILK CHOCOLATE CLUSTERS', 'FG', 'PREMIUM NUTS', 'MACADAMIA NUTS', 'D-39', 'Savla', 47.00, 7.500, 352.50, 'Fresh Stock', 'lot 167915 | inward 18/11/2025 | CFPL | Aus Macadamia / Milk Choc | D-39 @ Savla'),
    (55, 'HONEY ROASTED MACADAMIA NUT', 'FG', 'PREMIUM NUTS', 'MACADAMIA NUTS', 'D-39', 'Savla', 33.00, 10.000, 330.00, 'Fresh Stock', 'lot 167917 | inward 18/11/2025 | CFPL | Aus Macadamia / Honey Roasted | D-39 @ Savla'),
    (56, 'DEGLET DATES', 'RM', 'DATES', 'DEGLET', 'D-514', 'Savla', 10.00, 5.000, 50.00, 'Fresh Stock', 'lot 9976 | inward 25/11/2025 | CFPL | Premium Dates / DEGLET NOUR | D-514 @ Savla'),
    (57, 'MEDJOUL DATES LARGE', 'RM', 'DATES', 'MEDJOUL-LARGE', 'D-514', 'Savla', 47.00, 5.000, 235.00, 'Fresh Stock', 'lot 10470 | inward 04/12/2025 | CFPL | Premium Dates / Medjoul-Large HM | D-514 @ Savla'),
    (58, 'MEDJOUL DATES LARGE', 'RM', 'DATES', 'MEDJOUL-LARGE', 'D-514', 'Savla', 9.00, 5.000, 45.00, 'Fresh Stock', 'lot 10570 | inward 06/12/2025 | CFPL | Premium Dates / Medjoul-Large HM | D-514 @ Savla'),
    (59, 'MACADAMIA ROASTED - SIZE 6', 'RM', 'PREMIUM NUTS', 'MACADAMIA NUTS', 'D-514', 'Savla', 2.00, 10.000, 20.00, 'Fresh Stock', 'lot 10665 | inward 09/12/2025 | CFPL | Macadamia / Roasted Style-6 | D-514 @ Savla'),
    (60, 'DRIED CRANBERRY WHOLE', 'RM', 'CRANBERRY', 'CRANBERRY - WHOLE', 'D-39', 'Savla', 100.00, 4.540, 454.00, 'Fresh Stock', 'lot 175580 | inward 30/12/2025 | CFPL | Cranberry / Meduri | D-39 @ Savla'),
    (61, 'MACADAMIA FINGER LIME & JALAPENO', 'FG', 'PREMIUM NUTS', 'MACADAMIA NUTS', 'D-39', 'Savla', 20.00, 10.000, 200.00, 'Fresh Stock', 'lot 177602 | inward 14/01/2026 | CFPL | Nutwork Macadamia / Fingerlime Jalapeno | D-39 @ Savla'),
    (62, 'MACADAMIA ROSEMARY & SEA SALT', 'FG', 'PREMIUM NUTS', 'MACADAMIA NUTS', 'D-39', 'Savla', 17.00, 10.000, 170.00, 'Fresh Stock', 'lot 177604 | inward 14/01/2026 | CFPL | Nutwork Macadamia / Rosemary & Seasalt | D-39 @ Savla'),
    (63, 'MACADAMIA NIS - ROASTED SEA SALT', 'FG', 'PREMIUM NUTS', 'MACADAMIA NUTS', 'D-39', 'Savla', 21.00, 10.000, 210.00, 'Fresh Stock', 'lot 177605 | inward 14/01/2026 | CFPL | Nutwork Macadamia / R&S Seasalt | D-39 @ Savla'),
    (64, 'WASABI MACADAMIA STYLE 4', 'FG', 'PREMIUM NUTS', 'MACADAMIA NUTS', 'D-39', 'Savla', 19.00, 10.000, 190.00, 'Fresh Stock', 'lot 177603 | inward 14/01/2026 | CFPL | Nutwork Macadamia / Wasabi Style 4 | D-39 @ Savla'),
    (65, 'MEDJOUL DATES LARGE', 'RM', 'DATES', 'MEDJOUL-LARGE', 'D-514', 'Savla', 50.00, 5.000, 250.00, 'Fresh Stock', 'lot 10570 | inward 27/01/2026 | CFPL | Premium Dates / Medjoul-Large HM | D-514 @ Savla'),
    (66, 'DEGLET DATES', 'RM', 'DATES', 'DEGLET', 'D-39', 'Savla', 199.00, 5.000, 995.00, 'Fresh Stock', 'lot 180367 | inward 06/02/2026 | CFPL | Premium Dates / DEGLET NOUR | D-39 @ Savla'),
    (67, 'DEGLET DATES', 'RM', 'DATES', 'DEGLET', 'D-39', 'Savla', 200.00, 5.000, 1000.00, 'Fresh Stock', 'lot 180642 | inward 08/02/2026 | CFPL | Premium Dates / DEGLET NOUR | D-39 @ Savla'),
    (68, 'DEGLET DATES', 'RM', 'DATES', 'DEGLET', 'D-39', 'Savla', 200.00, 5.000, 1000.00, 'Fresh Stock', 'lot 182099 | inward 18/02/2026 | CFPL | Premium Dates / DEGLET NOUR | D-39 @ Savla'),
    (69, 'MEDJOUL DATES MEDIUM', 'RM', 'DATES', 'MEDJOUL-MEDIUM', 'D-514', 'Savla', 16.00, 5.000, 80.00, 'Fresh Stock', 'lot 13659 | inward 21/02/2026 | CFPL | Premium Dates / Medjoul-Medium Local | D-514 @ Savla'),
    (70, 'ORGANIC SEQEE PREMIUM LARGE', 'RM', 'DATES', 'SEQEE', 'D-514', 'Savla', 1.00, 5.000, 5.00, 'Fresh Stock', 'lot 13793 | inward 26/02/2026 | CFPL | Organic Dates / Org Seqee-Large | D-514 @ Savla'),
    (71, 'ORGANIC WANAN PREMIUM JUMBO', 'RM', 'DATES', 'WANAN', 'D-514', 'Savla', 1.00, 5.000, 5.00, 'Fresh Stock', 'lot 13792 | inward 26/02/2026 | CFPL | Organic Dates / Org Wanan-Jumbo | D-514 @ Savla'),
    (72, 'ORGANIC WANAN PREMIUM LARGE', 'RM', 'DATES', 'WANAN', 'D-514', 'Savla', 1.00, 5.000, 5.00, 'Fresh Stock', 'lot 13791 | inward 26/02/2026 | CFPL | Organic Dates / Org Wanan-Large | D-514 @ Savla'),
    (73, 'ORGANIC KHIDRI SPECIAL JUMBO', 'RM', 'DATES', 'KHIDRI', 'D-514', 'Savla', 1.00, 5.000, 5.00, 'Fresh Stock', 'lot 13790 | inward 26/02/2026 | CFPL | Organic Dates / Org Khidri-Jumbo | D-514 @ Savla'),
    (74, 'MACADAMIA NIS - ROASTED SEA SALT', 'FG', 'PREMIUM NUTS', 'MACADAMIA NUTS', 'D-39', 'Savla', 1.00, 10.000, 10.00, 'Fresh Stock', 'lot 183177 | inward 26/02/2026 | CFPL | Nutwork Macadamia / R&S Seasalt | D-39 @ Savla'),
    (75, 'ALDAKA MEDJOOL PREMIUM LARGE', 'RM', 'DATES', 'MEDJOUL-LARGE', 'D-514', 'Savla', 1.00, 5.000, 5.00, 'Fresh Stock', 'lot 13786 | inward 26/02/2026 | CFPL | Organic Dates / Org Medjoul-Lar | D-514 @ Savla'),
    (76, 'ALDAKA MEDJOOL PREMIUM JUMBO', 'RM', 'DATES', 'MEDJOUL-JUMBO', 'D-514', 'Savla', 1.00, 5.000, 5.00, 'Fresh Stock', 'lot 13787 | inward 26/02/2026 | CFPL | Organic Dates / Org Medjoul-Jumbo | D-514 @ Savla'),
    (77, 'MACADAMIA ROSEMARY & SEA SALT', 'FG', 'PREMIUM NUTS', 'MACADAMIA NUTS', 'D-39', 'Savla', 2.00, 10.000, 20.00, 'Fresh Stock', 'lot 183176 | inward 26/02/2026 | CFPL | Nutwork Macadamia / Rosemary & Seasalt | D-39 @ Savla'),
    (78, 'MACADAMIA FINGER LIME & JALAPENO', 'FG', 'PREMIUM NUTS', 'MACADAMIA NUTS', 'D-39', 'Savla', 2.00, 10.000, 20.00, 'Fresh Stock', 'lot 183175 | inward 26/02/2026 | CFPL | Nutwork Macadamia / Fingerlime Jalapeno | D-39 @ Savla'),
    (79, 'WASABI MACADAMIA STYLE 4', 'FG', 'PREMIUM NUTS', 'MACADAMIA NUTS', 'D-39', 'Savla', 2.00, 10.000, 20.00, 'Fresh Stock', 'lot 183174 | inward 26/02/2026 | CFPL | Nutwork Macadamia / Wasabi Style 4 | D-39 @ Savla'),
    (80, 'PECAN NUTS', 'RM', 'PREMIUM NUTS', 'PECAN NUTS', 'D-39', 'Savla', 9.00, 13.600, 122.40, 'Fresh Stock', 'lot 183172 | inward 26/02/2026 | CFPL | Pecan Nut / Pecan Tukda | D-39 @ Savla'),
    (81, 'OFF-GRADE DATES', 'RM', 'DATES', 'OFF-GRADE DATES', 'D-514', 'Savla', 41.00, 5.000, 205.00, 'Off Grade/Rejection', 'lot 14726 | inward 26/03/2026 | CFPL | Premium Dates / Medjoul-Rej | D-514 @ Savla'),
    (82, 'MEDJOUL DATES LARGE', 'RM', 'DATES', 'MEDJOUL-LARGE', 'D-514', 'Savla', 47.00, 5.000, 235.00, 'Fresh Stock', 'lot 14727 | inward 26/03/2026 | CFPL | Premium Dates / Medjoul-Large HM | D-514 @ Savla'),
    (83, 'DERI DATES', 'RM', 'DATES', 'DERI', 'D-39', 'Savla', 1783.00, 5.000, 8915.00, 'Fresh Stock', 'lot 186767 | inward 01/04/2026 | CFPL | Economical Dates / Deri | D-39 @ Savla'),
    (84, 'MACADAMIA ROASTED - SIZE 6', 'RM', 'PREMIUM NUTS', 'MACADAMIA NUTS', 'D-39', 'Savla', 25.00, 11.340, 283.50, 'Fresh Stock', 'lot 1344 | inward 03/04/2026 | CFPL | Macadamia / Roasted Style-6 | D-39 @ Savla'),
    (85, 'CALIFORNIA WALNUT KERNELS 2PC', 'RM', 'WALNUT', 'WALNUT - KERNEL', 'D-514', 'Savla', 9.00, 10.000, 90.00, 'Fresh Stock', 'lot 15192 | inward 04/04/2026 | CFPL | Walnut / Kernels 2pc 250gm | D-514 @ Savla'),
    (86, 'MACADAMIA PASTE', 'RM', 'PREMIUM NUTS', 'MACADAMIA NUTS', 'D-514', 'Savla', 27.00, 6.000, 162.00, 'Fresh Stock', 'lot 15231 | inward 07/04/2026 | CFPL | Macadamia / Paste-2Kg Tub | D-514 @ Savla'),
    (87, 'MACADAMIA NIS - ROASTED SEA SALT', 'FG', 'PREMIUM NUTS', 'MACADAMIA NUTS', 'D-39', 'Savla', 4.00, 10.000, 40.00, 'Fresh Stock', 'lot 2259 | inward 11/04/2026 | CFPL | Nutwork Macadamia / R&S Seasalt | D-39 @ Savla'),
    (88, 'PECAN NUTS', 'RM', 'PREMIUM NUTS', 'PECAN NUTS', 'D-39', 'Savla', 10.00, 10.000, 100.00, 'Fresh Stock', 'lot 2685 | inward 15/04/2026 | CFPL | Pecan Nut / Pecan Tukda | D-39 @ Savla'),
    (89, 'DERI DATES', 'RM', 'DATES', 'DERI', 'D-39', 'Savla', 10.00, 5.000, 50.00, 'Fresh Stock', 'lot 3623 | inward 21/04/2026 | CFPL | Economical Dates / Deri | D-39 @ Savla'),
    (91, 'CALIFORNIA PISTA INSHELL', 'RM', 'PISTA', 'PISTA - INSHELL', 'D-39', 'Savla', 88.00, 11.340, 997.92, 'Fresh Stock', 'lot 5396 | inward 06/05/2026 | CFPL | Pistachios / California 18-21 | D-39 @ Savla'),
    (92, 'CALIFORNIA PISTA INSHELL', 'RM', 'PISTA', 'PISTA - INSHELL', 'D-39', 'Savla', 99.00, 11.340, 1122.66, 'Fresh Stock', 'lot 5396 | inward 06/05/2026 | CFPL | Pistachios / California 18-21 | D-39 @ Savla'),
    (93, 'CALIFORNIA PISTA INSHELL', 'RM', 'PISTA', 'PISTA - INSHELL', 'D-39', 'Savla', 92.00, 11.340, 1043.28, 'Fresh Stock', 'lot 5396 | inward 06/05/2026 | CFPL | Pistachios / California 18-21 | D-39 @ Savla'),
    (94, 'CALIFORNIA PISTA INSHELL', 'RM', 'PISTA', 'PISTA - INSHELL', 'D-39', 'Savla', 87.00, 11.340, 986.58, 'Fresh Stock', 'lot 5396 | inward 06/05/2026 | CFPL | Pistachios / California 18-21 | D-39 @ Savla'),
    (95, 'CALIFORNIA PISTA INSHELL', 'RM', 'PISTA', 'PISTA - INSHELL', 'D-39', 'Savla', 100.00, 11.340, 1134.00, 'Fresh Stock', 'lot 5396 | inward 06/05/2026 | CFPL | Pistachios / California 18-21 | D-39 @ Savla'),
    (96, 'CALIFORNIA PISTA INSHELL', 'RM', 'PISTA', 'PISTA - INSHELL', 'D-39', 'Savla', 92.00, 11.340, 1043.28, 'Fresh Stock', 'lot 5396 | inward 06/05/2026 | CFPL | Pistachios / California 18-21 | D-39 @ Savla'),
    (97, 'CALIFORNIA PISTA INSHELL', 'RM', 'PISTA', 'PISTA - INSHELL', 'D-39', 'Savla', 92.00, 11.340, 1043.28, 'Fresh Stock', 'lot 5396 | inward 06/05/2026 | CFPL | Pistachios / California 18-21 | D-39 @ Savla'),
    (98, 'CALIFORNIA PISTA INSHELL', 'RM', 'PISTA', 'PISTA - INSHELL', 'D-39', 'Savla', 108.00, 11.340, 1224.72, 'Fresh Stock', 'lot 5396 | inward 06/05/2026 | CFPL | Pistachios / California 18-21 | D-39 @ Savla'),
    (99, 'CALIFORNIA PISTA INSHELL', 'RM', 'PISTA', 'PISTA - INSHELL', 'D-39', 'Savla', 33.00, 11.340, 374.22, 'Fresh Stock', 'lot 5396 | inward 06/05/2026 | CFPL | Pistachios / California 18-21 | D-39 @ Savla'),
    (100, 'CALIFORNIA PISTA INSHELL', 'RM', 'PISTA', 'PISTA - INSHELL', 'D-39', 'Savla', 92.00, 11.340, 1043.28, 'Fresh Stock', 'lot 5396 | inward 06/05/2026 | CFPL | Pistachios / California 18-21 | D-39 @ Savla'),
    (101, 'CALIFORNIA PISTA INSHELL', 'RM', 'PISTA', 'PISTA - INSHELL', 'D-39', 'Savla', 88.00, 11.340, 997.92, 'Fresh Stock', 'lot 5396 | inward 06/05/2026 | CFPL | Pistachios / California 18-21 | D-39 @ Savla'),
    (102, 'CALIFORNIA PISTA INSHELL', 'RM', 'PISTA', 'PISTA - INSHELL', 'D-39', 'Savla', 88.00, 11.340, 997.92, 'Fresh Stock', 'lot 5396 | inward 06/05/2026 | CFPL | Pistachios / California 18-21 | D-39 @ Savla'),
    (103, 'CALIFORNIA PISTA INSHELL', 'RM', 'PISTA', 'PISTA - INSHELL', 'D-39', 'Savla', 30.00, 11.340, 340.20, 'Fresh Stock', 'lot 5396 | inward 06/05/2026 | CFPL | Pistachios / California 18-21 | D-39 @ Savla'),
    (104, 'CALIFORNIA PISTA INSHELL', 'RM', 'PISTA', 'PISTA - INSHELL', 'D-39', 'Savla', 120.00, 11.340, 1360.80, 'Fresh Stock', 'lot 5396 | inward 06/05/2026 | CFPL | Pistachios / California 18-21 | D-39 @ Savla'),
    (105, 'PECAN NUTS', 'RM', 'PREMIUM NUTS', 'PECAN NUTS', 'D-39', 'Savla', 4.00, 10.000, 40.00, 'Fresh Stock', 'lot 5547 | inward 08/05/2026 | CFPL | Pecan Nut / Pecan Tukda | D-39 @ Savla'),
    (106, 'MACADAMIA ROASTED - SIZE 6', 'RM', 'PREMIUM NUTS', 'MACADAMIA NUTS', 'D-39', 'Savla', 1.00, 11.340, 11.34, 'Fresh Stock', 'lot 5553 | inward 08/05/2026 | CFPL | Macadamia / Roasted Style-6 | D-39 @ Savla'),
    (107, 'MACADAMIA ROASTED - SIZE 1', 'RM', 'PREMIUM NUTS', 'MACADAMIA NUTS', 'D-39', 'Savla', 1.00, 11.340, 11.34, 'Fresh Stock', 'lot 5551 | inward 08/05/2026 | CFPL | Aus Macadamia / Roasted Style-1 | D-39 @ Savla'),
    (108, 'PECANS NUTS INSHELL', 'RM', 'PREMIUM NUTS', 'PECAN NUTS', 'D-39', 'Savla', 1.00, 13.600, 13.60, 'Fresh Stock', 'lot 5548 | inward 08/05/2026 | CFPL | Pecan Nut / Inshell | D-39 @ Savla'),
    (109, 'ANJEER 1*2', 'RM', 'ANJEER', 'DRIED FIGS', 'D-39', 'Savla', 83.00, 10.000, 830.00, 'Fresh Stock', 'lot 6393 | inward 14/05/2026 | CFPL | Anjeer / MIX-A Grade | D-39 @ Savla'),
    (110, 'WASABI MACADAMIA STYLE 4', 'FG', 'PREMIUM NUTS', 'MACADAMIA NUTS', 'D-514', 'Savla', 1.00, 10.000, 10.00, 'Fresh Stock', 'lot 17175 | inward 30/05/2026 | CFPL | Nutwork Macadamia / Wasabi Style 4 | D-514 @ Savla'),
    (111, 'MACADAMIA ROASTED - SIZE 6', 'RM', 'PREMIUM NUTS', 'MACADAMIA NUTS', 'D-514', 'Savla', 1.00, 11.340, 11.34, 'Fresh Stock', 'lot 17171 | inward 30/05/2026 | CFPL | Macadamia / Roasted Style-6 | D-514 @ Savla'),
    (112, 'MACADAMIA FINGER LIME & JALAPENO', 'FG', 'PREMIUM NUTS', 'MACADAMIA NUTS', 'D-514', 'Savla', 1.00, 10.000, 10.00, 'Fresh Stock', 'lot 17174 | inward 30/05/2026 | CFPL | Nutwork Macadamia / Fingerlime Jalapeno | D-514 @ Savla'),
    (113, 'CALIFORNIA PISTA INSHELL', 'RM', 'PISTA', 'PISTA - INSHELL', 'D-39', 'Savla', 1304.00, 11.340, 14787.36, 'Fresh Stock', 'lot 8605 | inward 02/06/2026 | CFPL | Pistachios / California 21-25 | D-39 @ Savla'),
    (114, 'CALIFORNIA PISTA INSHELL', 'RM', 'PISTA', 'PISTA - INSHELL', 'D-39', 'Savla', 13.00, 11.500, 149.50, 'Fresh Stock', 'lot 8641 | inward 02/06/2026 | CFPL | Pistachios / California 21-25 | D-39 @ Savla'),
    (115, 'DRIED CRANBERRY SLICED', 'RM', 'CRANBERRY', 'CRANBERRY - SLICED', 'D-39', 'Savla', 198.00, 11.340, 2245.32, 'Fresh Stock', 'lot 8762 | inward 03/06/2026 | CFPL | Cranberry / Sliced | D-39 @ Savla'),
    (116, 'ORGANIC WANAN PREMIUM JUMBO', 'RM', 'DATES', 'WANAN', 'D-39', 'Savla', 2.00, 5.000, 10.00, 'Fresh Stock', 'lot 8941 | inward 04/06/2026 | CFPL | Organic Dates / Org Wanan-Jumbo | D-39 @ Savla'),
    (117, 'ORGANIC SEQEE PREMIUM LARGE', 'RM', 'DATES', 'SEQEE', 'D-39', 'Savla', 2.00, 5.000, 10.00, 'Fresh Stock', 'lot 8943 | inward 04/06/2026 | CFPL | Organic Dates / Org Seqee-Large | D-39 @ Savla'),
    (118, 'ORGANIC WANAN PREMIUM LARGE', 'RM', 'DATES', 'WANAN', 'D-39', 'Savla', 2.00, 5.000, 10.00, 'Fresh Stock', 'lot 8942 | inward 04/06/2026 | CFPL | Organic Dates / Org Wanan-Large | D-39 @ Savla'),
    (119, 'ANJEER 1*2', 'RM', 'ANJEER', 'DRIED FIGS', 'D-39', 'Savla', 45.00, 10.000, 450.00, 'Fresh Stock', 'lot 9645 | inward 10/06/2026 | CFPL | Anjeer / Small | D-39 @ Savla'),
    (120, 'KING SOLOMON MEDJOUL SUPER JUMBO 750 G', 'FG', 'DATES', 'MEDJOUL-SUPER JUMBO', 'D-514', 'Savla', 2.00, 8.000, 16.00, 'Fresh Stock', 'lot 17695 | inward 12/06/2026 | CFPL | Premium Dates / Medjoul-Sup.Jumbo-750gm | D-514 @ Savla'),
    (121, 'ANJEER 1*2', 'RM', 'ANJEER', 'DRIED FIGS', 'D-39', 'Savla', 22.00, 10.000, 220.00, 'Fresh Stock', 'lot 10558 | inward 17/06/2026 | CFPL | Anjeer / Small | D-39 @ Savla'),
    (122, 'DERI DATES', 'RM', 'DATES', 'DERI', 'D-514', 'Savla', 68.00, 5.000, 340.00, 'Fresh Stock', 'lot 18018 | inward 20/06/2026 | CFPL | Economical Dates / Deri | D-514 @ Savla'),
    (123, 'DEGLET DATES', 'RM', 'DATES', 'DEGLET', 'D-514', 'Savla', 190.00, 5.000, 950.00, 'Fresh Stock', 'lot 18007 | inward 20/06/2026 | CFPL | Premium Dates / DEGLET NOUR | D-514 @ Savla'),
    (124, 'MEDJOUL DATES JUMBO', 'RM', 'DATES', 'MEDJOUL-JUMBO', 'D-39', 'Savla', 175.00, 5.000, 875.00, 'Fresh Stock', 'lot 11155 | inward 22/06/2026 | CFPL | Premium Dates / Medjoul-Jumbo | D-39 @ Savla'),
    (125, 'MEDJOUL DATES JUMBO', 'RM', 'DATES', 'MEDJOUL-JUMBO', 'D-39', 'Savla', 206.00, 5.000, 1030.00, 'Fresh Stock', 'lot 11151 | inward 22/06/2026 | CFPL | Premium Dates / Medjoul-Jumbo | D-39 @ Savla'),
    (126, 'MEDJOUL DATES JUMBO', 'RM', 'DATES', 'MEDJOUL-JUMBO', 'D-39', 'Savla', 10.00, 5.000, 50.00, 'Fresh Stock', 'lot 11144 | inward 22/06/2026 | CFPL | Premium Dates / Medjoul-Jumbo | D-39 @ Savla'),
    (127, 'DEGLET DATES', 'RM', 'DATES', 'DEGLET', 'D-39', 'Savla', 200.00, 5.000, 1000.00, 'Fresh Stock', 'lot 11147 | inward 22/06/2026 | CFPL | Premium Dates / DEGLET NOUR | D-39 @ Savla'),
    (128, 'MEDJOUL DATES JUMBO', 'RM', 'DATES', 'MEDJOUL-JUMBO', 'D-39', 'Savla', 150.00, 5.000, 750.00, 'Fresh Stock', 'lot 11149 | inward 22/06/2026 | CFPL | Premium Dates / Medjoul-Jumbo | D-39 @ Savla'),
    (129, 'MEDJOUL DATES JUMBO', 'RM', 'DATES', 'MEDJOUL-JUMBO', 'D-39', 'Savla', 206.00, 5.000, 1030.00, 'Fresh Stock', 'lot 11150 | inward 22/06/2026 | CFPL | Premium Dates / Medjoul-Jumbo | D-39 @ Savla'),
    (130, 'ANJEER 1*2', 'RM', 'ANJEER', 'DRIED FIGS', 'D-39', 'Savla', 33.00, 10.000, 330.00, 'Fresh Stock', 'lot 12895 | inward 07/07/2026 | CFPL | Anjeer / Small | D-39 @ Savla'),
    (131, 'AL BARAKAH CHOPPED DATES', 'RM', 'DATES', 'DICED', 'D-39', 'Savla', 95.00, 10.000, 950.00, 'Fresh Stock', 'lot 13249 | inward 10/07/2026 | CFPL | Dry Dates / Chopped Dates | D-39 @ Savla'),
    (132, 'MEDJOUL DATES LARGE', 'RM', 'DATES', 'MEDJOUL-LARGE', 'D-39', 'Savla', 47.00, 5.000, 235.00, 'Fresh Stock', 'lot 13346 | inward 11/07/2026 | CFPL | Premium Dates / Medjoul-Large | D-39 @ Savla'),
    (133, 'CALIFORNIA PISTA INSHELL', 'RM', 'PISTA', 'PISTA - INSHELL', 'D-39', 'Savla', 1432.00, 11.340, 16238.88, 'Fresh Stock', 'lot 13374 | inward 11/07/2026 | CFPL | Pistachios / California 18-21 | D-39 @ Savla'),
    (134, 'MEDJOUL DATES PREMIUM BON BON', 'RM', 'DATES', 'MEDJOUL-BON BON', 'D-514', 'Savla', 78.00, 3.500, 273.00, 'Fresh Stock', 'lot 18904 | inward 14/07/2026 | CFPL | Premium Dates / Medjoul-Bonbon | D-514 @ Savla'),
    (135, 'MEDJOUL DATES JUMBO', 'RM', 'DATES', 'MEDJOUL-JUMBO', 'D-514', 'Savla', 148.00, 5.000, 740.00, 'Fresh Stock', 'lot 18946 | inward 14/07/2026 | CFPL | Premium Dates / Medjoul-Jumbo | D-514 @ Savla'),
    (136, 'MEDJOUL DATES JUMBO', 'RM', 'DATES', 'MEDJOUL-JUMBO', 'D-514', 'Savla', 400.00, 5.000, 2000.00, 'Fresh Stock', 'lot 19588 | inward 28/07/2026 | CFPL | Premium Dates / Medjoul-Jumbo | D-514 @ Savla'),
    (137, 'ANJEER CHUTTA', 'RM', 'ANJEER', 'DRIED FIGS', 'D-514', 'Savla', 25.00, 10.000, 250.00, 'Fresh Stock', 'lot 19657 | inward 29/07/2026 | CFPL | Anjeer / Coin | D-514 @ Savla'),
    (138, 'ORGANIC KHIDRI PREMIUM MEDIUM', 'RM', 'DATES', 'KHIDRI', 'D-514', 'Savla', 96.00, 5.000, 480.00, 'Fresh Stock', 'lot 19804 | inward 04/08/2026 | CFPL | Organic Dates / Org Khidri-Med | D-514 @ Savla'),
    (139, 'ORGANIC KHIDRI LARGE DATES', 'RM', 'DATES', 'KHIDRI', 'D-514', 'Savla', 94.00, 5.000, 470.00, 'Fresh Stock', 'lot 19805 | inward 04/08/2026 | CFPL | Organic Dates / Org Khidri-Large | D-514 @ Savla'),
    (140, 'WET DATES AJWA', 'RM', 'DATES', 'AJWA', 'D-514', 'Savla', 52.00, 5.000, 260.00, 'Fresh Stock', 'lot 19806 | inward 04/08/2026 | CFPL | Premium Dates / Ajwa | D-514 @ Savla'),
    (141, 'ANJEER 1*2', 'RM', 'ANJEER', 'DRIED FIGS', 'D-39', 'Savla', 71.00, 10.000, 710.00, 'Fresh Stock', 'lot 16431 | inward 05/08/2026 | CFPL | Anjeer / Small | D-39 @ Savla'),
    (142, 'ORGANIC WANAN PREMIUM LARGE', 'RM', 'DATES', 'WANAN', 'D-514', 'Savla', 44.00, 5.000, 220.00, 'Fresh Stock', 'lot 19989 | inward 12/08/2026 | CFPL | Organic Dates / Org Wanan-Large | D-514 @ Savla'),
    (143, 'ORGANIC KHIDRI PREMIUM MEDIUM', 'RM', 'DATES', 'KHIDRI', 'D-514', 'Savla', 95.00, 5.000, 475.00, 'Fresh Stock', 'lot 19990 | inward 12/08/2026 | CFPL | Organic Dates / Org Khidri-Med | D-514 @ Savla'),
    (144, 'ORGANIC KHIDRI SPECIAL JUMBO', 'RM', 'DATES', 'KHIDRI', 'D-514', 'Savla', 17.00, 5.000, 85.00, 'Fresh Stock', 'lot 19987 | inward 12/08/2026 | CFPL | Organic Dates / Org Khidri-Jumbo | D-514 @ Savla'),
    (145, 'ORGANIC SEQEE PREMIUM LARGE', 'RM', 'DATES', 'SEQEE', 'D-514', 'Savla', 2.00, 5.000, 10.00, 'Fresh Stock', 'lot 19988 | inward 12/08/2026 | CFPL | Organic Dates / Org Seqee-Large | D-514 @ Savla'),
    (146, 'ANJEER 1*2', 'RM', 'ANJEER', 'DRIED FIGS', 'D-514', 'Savla', 30.00, 10.000, 300.00, 'Fresh Stock', 'lot 19992 | inward 12/08/2026 | CFPL | Anjeer / Small | D-514 @ Savla'),
    (147, 'CASHEW 180', 'RM', 'CASHEW', 'CASHEW - KERNEL', 'D-514', 'Savla', 14.00, 10.000, 140.00, 'Fresh Stock', 'lot 19991 | inward 12/08/2026 | CFPL | Cashew / 180 Count | D-514 @ Savla'),
    (148, 'GREEK DARK SEEDLESS RAISINS (BLACK CURRANT)', 'RM', 'BLACKCURRANT', 'DRIED BLACKCURRANT', 'D-39', 'Savla', 2.00, 12.500, 25.00, 'Fresh Stock', 'lot 17850 | inward 17/08/2026 | CFPL | Black Currant / BlackCurrant | D-39 @ Savla'),
    (149, 'DRIED BLUEBERRY APPLE INFUSED', 'RM', 'BLUEBERRY', 'BLUEBERRY - APPLE INFUSED', 'D-39', 'Savla', 83.00, 9.070, 752.81, 'Fresh Stock', 'lot 17851 | inward 17/08/2026 | CFPL | Blueberry / Blueberry - Apple Infused | D-39 @ Savla'),
    (150, 'ANJEER CHUTTA', 'RM', 'ANJEER', 'DRIED FIGS', 'D-39', 'Savla', 3.00, 10.000, 30.00, 'Fresh Stock', 'lot 18073 | inward 19/08/2026 | CFPL | Anjeer / Small | D-39 @ Savla'),
    (151, 'ANJEER 1*2', 'RM', 'ANJEER', 'DRIED FIGS', 'D-39', 'Savla', 15.00, 10.000, 150.00, 'Fresh Stock', 'lot 18074 | inward 19/08/2026 | CFPL | Anjeer / Small | D-39 @ Savla'),
    (152, 'ANJEER 1*2', 'RM', 'ANJEER', 'DRIED FIGS', 'D-39', 'Savla', 3.00, 10.000, 30.00, 'Fresh Stock', 'lot 18075 | inward 19/08/2026 | CFPL | Anjeer / Small | D-39 @ Savla'),
    (153, 'OFF-GRADE DATES', 'RM', 'DATES', 'OFF-GRADE DATES', 'D-514', 'Savla', 9.00, 5.000, 45.00, 'Off Grade/Rejection', 'lot 20285 | inward 19/08/2026 | CDPL | Premium Dates / Medjoul-Rej | D-514 @ Savla'),
    (154, 'MEDJOUL DATES JUMBO', 'RM', 'DATES', 'MEDJOUL-JUMBO', 'D-39', 'Savla', 148.00, 5.000, 740.00, 'Fresh Stock', 'lot 11141 | inward 20/08/2026 | CFPL | Premium Dates / Medjoul-Jumbo | D-39 @ Savla'),
    (155, 'MEDJOUL DATES LARGE', 'RM', 'DATES', 'MEDJOUL-LARGE', 'D-39', 'Savla', 11.00, 5.000, 55.00, 'Fresh Stock', 'lot 19830 | inward 02/09/2026 | CFPL | Premium Dates / Medjoul-Large | D-39 @ Savla'),
    (156, 'MEDJOUL DATES PREMIUM BON BON', 'RM', 'DATES', 'MEDJOUL-BON BON', 'D-39', 'Savla', 1.00, 13.600, 13.60, 'Fresh Stock', 'lot 19829 | inward 02/09/2026 | CFPL | Pecan Nut / Pecan Tukda | D-39 @ Savla'),
    (158, 'ANJEER 1*2', 'RM', 'ANJEER', 'DRIED FIGS', 'D-39', 'Savla', 3.00, 10.000, 30.00, 'Fresh Stock', 'lot 20564 | inward 08/09/2026 | CFPL | Anjeer / Small | D-39 @ Savla'),
    (159, 'ANJEER 1*2', 'RM', 'ANJEER', 'DRIED FIGS', 'D-39', 'Savla', 3.00, 10.000, 30.00, 'Off Grade/Rejection', 'lot 20563 | inward 08/09/2026 | CFPL | Anjeer / OFF-GRADE ANJEER | D-39 @ Savla'),
    (160, 'ANJEER 1*2', 'RM', 'ANJEER', 'DRIED FIGS', 'D-39', 'Savla', 15.00, 10.000, 150.00, 'Fresh Stock', 'lot 20560 | inward 08/09/2026 | CFPL | Anjeer / Small | D-39 @ Savla'),
    (161, 'ANJEER 1*2', 'RM', 'ANJEER', 'DRIED FIGS', 'D-39', 'Savla', 5.00, 10.000, 50.00, 'Fresh Stock', 'lot 20561 | inward 08/09/2026 | CFPL | Anjeer / Small | D-39 @ Savla'),
    (162, 'ANJEER 1*2', 'RM', 'ANJEER', 'DRIED FIGS', 'D-39', 'Savla', 11.00, 10.000, 110.00, 'Fresh Stock', 'lot 20562 | inward 08/09/2026 | CFPL | Anjeer / Small | D-39 @ Savla'),
    (163, 'MEDJOUL DATES MEDIUM', 'RM', 'DATES', 'MEDJOUL-MEDIUM', 'D-39', 'Savla', 3.00, 5.000, 15.00, 'Fresh Stock', 'lot 20565 | inward 08/09/2026 | CFPL | Premium Dates / Medjoul-Medium Local | D-39 @ Savla'),
    (164, 'SUNFLOWER SEEDS', 'RM', 'SEEDS', 'SUNFLOWER', 'D-39', 'Savla', 981.00, 25.000, 24525.00, 'Fresh Stock', 'lot 20677 | inward 09/09/2026 | CFPL | Seeds / Sunflower | D-39 @ Savla'),
    (165, 'OFF-GRADE DATES', 'RM', 'DATES', 'OFF-GRADE DATES', 'D-514', 'Savla', 5.00, 5.000, 25.00, 'Off Grade/Rejection', 'lot 21259 | inward 10/09/2026 | CDPL | Premium Dates / Medjoul-Large | D-514 @ Savla'),
    (166, 'PECAN NUTS', 'RM', 'PREMIUM NUTS', 'PECAN NUTS', 'D-39', 'Savla', 36.00, 13.600, 489.60, 'Fresh Stock', 'lot 20883 | inward 10/09/2026 | CFPL | Pecan Nut / Pecan | D-39 @ Savla'),
    (167, 'DRIED CRANBERRY SLICED', 'RM', 'CRANBERRY', 'CRANBERRY - SLICED', 'D-39', 'Savla', 1110.00, 11.340, 12587.40, 'Fresh Stock', 'lot 22546 | inward 23/09/2026 | CFPL | Cranberry / Sliced | D-39 @ Savla'),
    (168, 'OFF-GRADE DATES', 'RM', 'DATES', 'OFF-GRADE DATES', 'D-39', 'Savla', 12.00, 5.000, 60.00, 'Off Grade/Rejection', 'lot 22715 | inward 24/09/2026 | CFPL | Premium Dates / Medjoul-Large | D-39 @ Savla'),
    (169, 'INDIAN GREEN RAISINS', 'RM', 'RAISIN', 'RAISIN - INDIAN', 'D-39', 'Savla', 82.00, 10.000, 820.00, 'Fresh Stock', 'lot 23186 | inward 27/09/2026 | CFPL | Raisins / Indian Raisin | D-39 @ Savla'),
    (170, 'INDIAN GREEN RAISINS', 'RM', 'RAISIN', 'RAISIN - INDIAN', 'D-39', 'Savla', 29.00, 10.000, 290.00, 'Fresh Stock', 'lot 23175 | inward 27/09/2026 | CFPL | Raisins / Indian Raisin | D-39 @ Savla'),
    (171, 'INDIAN GREEN RAISINS', 'RM', 'RAISIN', 'RAISIN - INDIAN', 'D-39', 'Savla', 84.00, 10.000, 840.00, 'Fresh Stock', 'lot 23176 | inward 27/09/2026 | CFPL | Raisins / Indian Raisin | D-39 @ Savla'),
    (172, 'INDIAN GREEN RAISINS', 'RM', 'RAISIN', 'RAISIN - INDIAN', 'D-39', 'Savla', 61.00, 10.000, 610.00, 'Fresh Stock', 'lot 23177 | inward 27/09/2026 | CFPL | Raisins / Indian Raisin | D-39 @ Savla'),
    (173, 'INDIAN GREEN RAISINS', 'RM', 'RAISIN', 'RAISIN - INDIAN', 'D-39', 'Savla', 31.00, 10.000, 310.00, 'Fresh Stock', 'lot 23181 | inward 27/09/2026 | CFPL | Raisins / Indian Raisin | D-39 @ Savla'),
    (174, 'INDIAN GREEN RAISINS', 'RM', 'RAISIN', 'RAISIN - INDIAN', 'D-39', 'Savla', 67.00, 10.000, 670.00, 'Fresh Stock', 'lot 23178 | inward 27/09/2026 | CFPL | Raisins / Indian Raisin | D-39 @ Savla'),
    (175, 'INDIAN GREEN RAISINS', 'RM', 'RAISIN', 'RAISIN - INDIAN', 'D-39', 'Savla', 69.00, 10.000, 690.00, 'Fresh Stock', 'lot 23179 | inward 27/09/2026 | CFPL | Raisins / Indian Raisin | D-39 @ Savla'),
    (176, 'MEDJOUL DATES SUPER JUMBO', 'RM', 'DATES', 'MEDJOUL-SUPER JUMBO', 'D-514', 'Savla', 189.00, 5.000, 945.00, 'Fresh Stock', 'lot 22103 | inward 29/09/2026 | CFPL | Premium Dates / Medjoul-Sup.Jumbo | D-514 @ Savla'),
    (177, 'MEDJOUL DATES SUPER JUMBO', 'RM', 'DATES', 'MEDJOUL-SUPER JUMBO', 'D-514', 'Savla', 150.00, 5.000, 750.00, 'Fresh Stock', 'lot 22102 | inward 29/09/2026 | CFPL | Premium Dates / Medjoul-Sup.Jumbo | D-514 @ Savla'),
    (178, 'DRIED PRUNES (PITTED)', 'RM', 'PRUNES', 'PRUNES - PITTED', 'D-39', 'Savla', 300.00, 11.340, 3402.00, 'Fresh Stock', 'lot 23762 | inward 30/09/2026 | CFPL | Prunes / Pitted | D-39 @ Savla'),
    (179, 'DRIED PRUNES (PITTED)', 'RM', 'PRUNES', 'PRUNES - PITTED', 'D-39', 'Savla', 44.00, 11.340, 498.96, 'Fresh Stock', 'lot 23762 | inward 30/09/2026 | CFPL | Prunes / Pitted | D-39 @ Savla'),
    (180, 'ANJEER 1*2', 'RM', 'ANJEER', 'DRIED FIGS', 'D-39', 'Savla', 4.00, 10.000, 40.00, 'Fresh Stock', 'lot 24203 | inward 03/10/2026 | CFPL | Anjeer / Small | D-39 @ Savla'),
    (181, 'ANJEER 1*2', 'RM', 'ANJEER', 'DRIED FIGS', 'D-39', 'Savla', 31.00, 10.000, 310.00, 'Fresh Stock', 'lot 24202 | inward 03/10/2026 | CFPL | Anjeer / Small | D-39 @ Savla'),
    (182, 'ANJEER CHUTTA', 'RM', 'ANJEER', 'DRIED FIGS', 'D-39', 'Savla', 34.00, 10.000, 340.00, 'Fresh Stock', 'lot 24201 | inward 03/10/2026 | CFPL | Anjeer / Small | D-39 @ Savla'),
    (183, 'ANJEER 1*2', 'RM', 'ANJEER', 'DRIED FIGS', 'D-39', 'Savla', 6.00, 10.000, 60.00, 'Fresh Stock', 'lot 24204 | inward 03/10/2026 | CFPL | Anjeer / Small | D-39 @ Savla'),
    (184, 'ANJEER 1*2', 'RM', 'ANJEER', 'DRIED FIGS', 'D-39', 'Savla', 5.00, 10.000, 50.00, 'Off Grade/Rejection', 'lot 24200 | inward 03/10/2026 | CFPL | Anjeer / OFF-GRADE ANJEER | D-39 @ Savla'),
    (185, 'SUNFLOWER SEEDS', 'RM', 'SEEDS', 'SUNFLOWER', 'D-39', 'Savla', 1100.00, 25.000, 27500.00, 'Fresh Stock', 'lot 24493 | inward 05/10/2026 | CFPL | Seeds / Sunflower | D-39 @ Savla'),
    (186, 'OFF-GRADE DATES', 'RM', 'DATES', 'OFF-GRADE DATES', 'D-39', 'Savla', 18.00, 5.000, 90.00, 'Off Grade/Rejection', 'lot 24855 | inward 07/10/2026 | CFPL | Premium Dates / Medjoul-Large | D-39 @ Savla'),
    (187, 'MEDJOUL DATES JUMBO', 'RM', 'DATES', 'MEDJOUL-JUMBO', 'D-514', 'Savla', 70.00, 5.000, 350.00, 'Fresh Stock', 'lot 22570 | inward 07/10/2026 | CFPL | Premium Dates / Medjoul-Jumbo | D-514 @ Savla'),
    (188, '25KG AL BARAKAH DATE SYRUP V1', 'RM', 'DATES', 'SYRUP', 'D-39', 'Savla', 720.00, 25.000, 18000.00, 'Fresh Stock', 'lot 25049 | inward 08/10/2026 | CDPL | Date Syrup /  DATE SYRUP V1 | D-39 @ Savla'),
    (189, 'AL BARAKAH CHOPPED DATES', 'RM', 'DATES', 'DICED', 'D-39', 'Savla', 599.00, 10.000, 5990.00, 'Fresh Stock', 'lot 25048 | inward 08/10/2026 | CFPL | Dry Dates / Chopped Dates | D-39 @ Savla'),
    (190, 'AL BARAKAH DATE PASTE 12.5KG', 'RM', 'DATES', 'PASTE', 'D-39', 'Savla', 11.00, 12.500, 137.50, 'Fresh Stock', 'lot 22079 | inward 09/10/2026 | CDPL | Date Paste / Date Paste | D-39 @ Savla'),
    (191, 'INDIAN GREEN RAISINS', 'RM', 'RAISIN', 'RAISIN - INDIAN', 'D-39', 'Savla', 56.00, 10.000, 560.00, 'Fresh Stock', 'lot 25653 | inward 10/10/2026 | CFPL | Raisins / Indian Raisin | D-39 @ Savla'),
    (192, 'INDIAN GREEN RAISINS', 'RM', 'RAISIN', 'RAISIN - INDIAN', 'D-39', 'Savla', 46.00, 10.000, 460.00, 'Fresh Stock', 'lot 25654 | inward 10/10/2026 | CFPL | Raisins / Indian Raisin | D-39 @ Savla'),
    (193, 'INDIAN GREEN RAISINS', 'RM', 'RAISIN', 'RAISIN - INDIAN', 'D-39', 'Savla', 23.00, 10.000, 230.00, 'Fresh Stock', 'lot 25655 | inward 10/10/2026 | CFPL | Raisins / Indian Raisin | D-39 @ Savla'),
    (194, 'INDIAN GREEN RAISINS', 'RM', 'RAISIN', 'RAISIN - INDIAN', 'D-39', 'Savla', 22.00, 10.000, 220.00, 'Fresh Stock', 'lot 25656 | inward 10/10/2026 | CFPL | Raisins / Indian Raisin | D-39 @ Savla'),
    (195, 'INDIAN GREEN RAISINS', 'RM', 'RAISIN', 'RAISIN - INDIAN', 'D-39', 'Savla', 29.00, 10.000, 290.00, 'Fresh Stock', 'lot 25657 | inward 10/10/2026 | CFPL | Raisins / Indian Raisin | D-39 @ Savla'),
    (196, 'INDIAN GREEN RAISINS', 'RM', 'RAISIN', 'RAISIN - INDIAN', 'D-39', 'Savla', 69.00, 10.000, 690.00, 'Fresh Stock', 'lot 25658 | inward 10/10/2026 | CFPL | Raisins / Indian Raisin | D-39 @ Savla'),
    (197, 'INDIAN GREEN RAISINS', 'RM', 'RAISIN', 'RAISIN - INDIAN', 'D-39', 'Savla', 44.00, 10.000, 440.00, 'Fresh Stock', 'lot 25659 | inward 10/10/2026 | CFPL | Raisins / Indian Raisin | D-39 @ Savla'),
    (198, 'INDIAN GREEN RAISINS', 'RM', 'RAISIN', 'RAISIN - INDIAN', 'D-39', 'Savla', 23.00, 10.000, 230.00, 'Fresh Stock', 'lot 25660 | inward 10/10/2026 | CFPL | Raisins / Indian Raisin | D-39 @ Savla'),
    (199, 'INDIAN GREEN RAISINS', 'RM', 'RAISIN', 'RAISIN - INDIAN', 'D-39', 'Savla', 47.00, 10.000, 470.00, 'Fresh Stock', 'lot 25661 | inward 10/10/2026 | CFPL | Raisins / Indian Raisin | D-39 @ Savla'),
    (200, 'INDIAN GREEN RAISINS', 'RM', 'RAISIN', 'RAISIN - INDIAN', 'D-39', 'Savla', 58.00, 10.000, 580.00, 'Fresh Stock', 'lot 25662 | inward 10/10/2026 | CFPL | Raisins / Indian Raisin | D-39 @ Savla'),
    (201, 'INDIAN GREEN RAISINS', 'RM', 'RAISIN', 'RAISIN - INDIAN', 'D-39', 'Savla', 135.00, 10.000, 1350.00, 'Fresh Stock', 'lot 25663 | inward 10/10/2026 | CFPL | Raisins / Indian Raisin | D-39 @ Savla'),
    (202, 'INDIAN GREEN RAISINS', 'RM', 'RAISIN', 'RAISIN - INDIAN', 'D-39', 'Savla', 11.00, 10.000, 110.00, 'Fresh Stock', 'lot 25664 | inward 10/10/2026 | CFPL | Raisins / Indian Raisin | D-39 @ Savla'),
    (203, 'INDIAN GREEN RAISINS', 'RM', 'RAISIN', 'RAISIN - INDIAN', 'D-39', 'Savla', 1.00, 10.000, 10.00, 'Fresh Stock', 'lot 25665 | inward 10/10/2026 | CFPL | Raisins / Indian Raisin | D-39 @ Savla'),
    (204, 'INDIAN GREEN RAISINS', 'RM', 'RAISIN', 'RAISIN - INDIAN', 'D-39', 'Savla', 18.00, 10.000, 180.00, 'Fresh Stock', 'lot 25666 | inward 10/10/2026 | CFPL | Raisins / Indian Raisin | D-39 @ Savla'),
    (205, 'INDIAN GREEN RAISINS', 'RM', 'RAISIN', 'RAISIN - INDIAN', 'D-39', 'Savla', 39.00, 10.000, 390.00, 'Fresh Stock', 'lot 25667 | inward 10/10/2026 | CFPL | Raisins / Indian Raisin | D-39 @ Savla'),
    (206, 'INDIAN GREEN RAISINS', 'RM', 'RAISIN', 'RAISIN - INDIAN', 'D-39', 'Savla', 113.00, 10.000, 1130.00, 'Fresh Stock', 'lot 25668 | inward 10/10/2026 | CFPL | Raisins / Indian Raisin | D-39 @ Savla'),
    (207, 'INDIAN GREEN RAISINS', 'RM', 'RAISIN', 'RAISIN - INDIAN', 'D-39', 'Savla', 48.00, 10.000, 480.00, 'Fresh Stock', 'lot 25669 | inward 10/10/2026 | CFPL | Raisins / Indian Raisin | D-39 @ Savla'),
    (208, 'INDIAN GREEN RAISINS', 'RM', 'RAISIN', 'RAISIN - INDIAN', 'D-39', 'Savla', 18.00, 10.000, 180.00, 'Fresh Stock', 'lot 25670 | inward 10/10/2026 | CFPL | Raisins / Indian Raisin | D-39 @ Savla'),
    (209, 'SUNFLOWER SEEDS', 'RM', 'SEEDS', 'SUNFLOWER', 'D-514', 'Savla', 1080.00, 25.000, 27000.00, 'Fresh Stock', 'lot 22792 | inward 10/10/2026 | CFPL | Seeds / Sunflower | D-514 @ Savla'),
    (210, 'MEDJOUL DATES LARGE', 'RM', 'DATES', 'MEDJOUL-LARGE', 'D-514', 'Savla', 208.00, 5.000, 1040.00, 'Fresh Stock', 'lot 22903 | inward 13/10/2026 | CFPL | Premium Dates / Medjoul-Large | D-514 @ Savla'),
    (211, 'MEDJOUL DATES MEDIUM', 'RM', 'DATES', 'MEDJOUL-MEDIUM', 'D-514', 'Savla', 198.00, 5.000, 990.00, 'Fresh Stock', 'lot 22896 | inward 13/10/2026 | CFPL | Premium Dates / Medjoul-Medium  | D-514 @ Savla'),
    (212, 'MEDJOUL DATES MEDIUM', 'RM', 'DATES', 'MEDJOUL-MEDIUM', 'D-514', 'Savla', 92.00, 5.000, 460.00, 'Fresh Stock', 'lot 22891 | inward 13/10/2026 | CFPL | Premium Dates / Medjoul-Medium  | D-514 @ Savla'),
    (213, 'MEDJOUL DATES PREMIUM BON BON', 'RM', 'DATES', 'MEDJOUL-BON BON', 'D-514', 'Savla', 28.00, 3.500, 98.00, 'Fresh Stock', 'lot 22910 | inward 13/10/2026 | CFPL | Premium Dates / Medjoul-Bonbon | D-514 @ Savla'),
    (214, 'MEDJOUL DATES LARGE', 'RM', 'DATES', 'MEDJOUL-LARGE', 'D-514', 'Savla', 198.00, 5.000, 990.00, 'Fresh Stock', 'lot 22905 | inward 13/10/2026 | CFPL | Premium Dates / Medjoul-Large | D-514 @ Savla'),
    (215, 'MEDJOUL DATES MEDIUM', 'RM', 'DATES', 'MEDJOUL-MEDIUM', 'D-514', 'Savla', 198.00, 5.000, 990.00, 'Fresh Stock', 'lot 22897 | inward 13/10/2026 | CFPL | Premium Dates / Medjoul-Medium  | D-514 @ Savla'),
    (216, 'MEDJOUL DATES LARGE', 'RM', 'DATES', 'MEDJOUL-LARGE', 'D-514', 'Savla', 150.00, 5.000, 750.00, 'Fresh Stock', 'lot 22906 | inward 13/10/2026 | CFPL | Premium Dates / Medjoul-Large | D-514 @ Savla'),
    (217, 'MEDJOUL DATES MEDIUM', 'RM', 'DATES', 'MEDJOUL-MEDIUM', 'D-514', 'Savla', 198.00, 5.000, 990.00, 'Fresh Stock', 'lot 22893 | inward 13/10/2026 | CFPL | Premium Dates / Medjoul-Medium  | D-514 @ Savla'),
    (218, 'MEDJOUL DATES MEDIUM', 'RM', 'DATES', 'MEDJOUL-MEDIUM', 'D-514', 'Savla', 198.00, 5.000, 990.00, 'Fresh Stock', 'lot 22895 | inward 13/10/2026 | CFPL | Premium Dates / Medjoul-Medium  | D-514 @ Savla'),
    (219, '25KG AL BARAKAH DATE SYRUP V1', 'RM', 'DATES', 'SYRUP', 'D-39', 'Savla', 144.00, 25.000, 3600.00, 'Fresh Stock', 'lot 26482 | inward 14/10/2026 | CDPL | Date Syrup /  DATE SYRUP V1 | D-39 @ Savla'),
    (220, 'AL BARAKAH DATE PASTE 12.5KG', 'RM', 'DATES', 'PASTE', 'D-39', 'Savla', 1666.00, 12.500, 20825.00, 'Fresh Stock', 'lot 26481 | inward 14/10/2026 | CDPL | Date Paste / Date Paste | D-39 @ Savla'),
    (221, '25KG AL BARAKAH DATE SYRUP V1', 'RM', 'DATES', 'SYRUP', 'D-39', 'Savla', 1008.00, 25.000, 25200.00, 'Fresh Stock', 'lot 26585 | inward 15/10/2026 | CDPL | Date Syrup /  DATE SYRUP V1 | D-39 @ Savla'),
    (222, '25KG AL BARAKAH DATE SYRUP V1', 'RM', 'DATES', 'SYRUP', 'D-39', 'Savla', 1008.00, 25.000, 25200.00, 'Fresh Stock', 'lot 26667 | inward 15/10/2026 | CDPL | Date Syrup /  DATE SYRUP V1 | D-39 @ Savla'),
    (223, 'MEDJOUL DATES SUPER JUMBO', 'RM', 'DATES', 'MEDJOUL-SUPER JUMBO', 'D-514', 'Savla', 40.00, 5.000, 200.00, 'Fresh Stock', 'lot 23103 | inward 16/10/2026 | CFPL | Premium Dates / Medjoul-Sup.Jumbo | D-514 @ Savla'),
    (224, 'OFF-GRADE DATES', 'RM', 'DATES', 'OFF-GRADE DATES', 'D-514', 'Savla', 36.00, 5.000, 180.00, 'Off Grade/Rejection', 'lot 23229 | inward 17/10/2026 | CFPL | Premium Dates / Medjoul-Large | D-514 @ Savla'),
    (225, 'ANJEER (FIGS)', 'RM', 'ANJEER', 'DRIED FIGS', 'D-514', 'Savla', 63.00, 5.000, 315.00, 'Fresh Stock', 'lot 23228 | inward 17/10/2026 | CFPL | Anjeer / Anjeer Slice | D-514 @ Savla'),
    (226, '25KG AL BARAKAH DATE SYRUP V1', 'RM', 'DATES', 'SYRUP', 'D-39', 'Savla', 1008.00, 25.000, 25200.00, 'Fresh Stock', 'lot 27341 | inward 18/10/2026 | CDPL | Date Syrup /  DATE SYRUP V1 | D-39 @ Savla'),
    (227, '25KG AL BARAKAH DATE SYRUP V1', 'RM', 'DATES', 'SYRUP', 'D-39', 'Savla', 1008.00, 25.000, 25200.00, 'Fresh Stock', 'lot 27603 | inward 20/10/2026 | CDPL | Date Syrup /  DATE SYRUP V1 | D-39 @ Savla'),
    (228, '25KG AL BARAKAH DATE SYRUP V1', 'RM', 'DATES', 'SYRUP', 'D-39', 'Savla', 1008.00, 25.000, 25200.00, 'Fresh Stock', 'lot 27741 | inward 21/10/2026 | CDPL | Date Syrup /  DATE SYRUP V1 | D-39 @ Savla'),
    (229, 'CALIFORNIA PISTA INSHELL', 'RM', 'PISTA', 'PISTA - INSHELL', 'D-39', 'Savla', 878.00, 25.000, 21950.00, 'Fresh Stock', 'lot 27842 | inward 22/10/2026 | CDPL | Pistachios / California 18-21 | D-39 @ Savla'),
    (230, 'CALIFORNIA PISTA INSHELL', 'RM', 'PISTA', 'PISTA - INSHELL', 'D-39', 'Savla', 194.00, 11.340, 2199.96, 'Fresh Stock', 'lot 28058 | inward 23/10/2026 | CFPL | Pistachios / California 21-25 | D-39 @ Savla'),
    (231, 'CALIFORNIA PISTA INSHELL', 'RM', 'PISTA', 'PISTA - INSHELL', 'D-39', 'Savla', 1521.00, 11.340, 17248.14, 'Fresh Stock', 'lot 28057 | inward 23/10/2026 | CFPL | Pistachios / California 21-25 | D-39 @ Savla'),
    (232, 'CALIFORNIA PISTA INSHELL', 'RM', 'PISTA', 'PISTA - INSHELL', 'D-39', 'Savla', 5.00, 11.340, 56.70, 'Fresh Stock', 'lot 28059 | inward 23/10/2026 | CFPL | Pistachios / California 21-25 | D-39 @ Savla'),
    (233, 'MEDJOUL DATES JUMBO', 'RM', 'DATES', 'MEDJOUL-JUMBO', 'D-514', 'Savla Bond', 210.00, 5.000, 1050.00, 'Fresh Stock', 'lot 17420 | inward 05/06/2026 | CFPL | Premium Dates / Medjoul-Jumbo | D-514 @ Savla Bond'),
    (234, 'MEDJOUL DATES LARGE', 'RM', 'DATES', 'MEDJOUL-LARGE', 'D-514', 'Savla Bond', 210.00, 5.000, 1050.00, 'Fresh Stock', 'lot 17408 | inward 05/06/2026 | CFPL | Premium Dates / Medjoul-Large | D-514 @ Savla Bond'),
    (235, 'MEDJOUL DATES LARGE', 'RM', 'DATES', 'MEDJOUL-LARGE', 'D-514', 'Savla Bond', 170.00, 5.000, 850.00, 'Fresh Stock', 'lot 17407 | inward 05/06/2026 | CFPL | Premium Dates / Medjoul-Large | D-514 @ Savla Bond'),
    (236, 'MEDJOUL DATES JUMBO', 'RM', 'DATES', 'MEDJOUL-JUMBO', 'D-514', 'Savla Bond', 210.00, 5.000, 1050.00, 'Fresh Stock', 'lot 17419 | inward 05/06/2026 | CFPL | Premium Dates / Medjoul-Jumbo | D-514 @ Savla Bond'),
    (237, 'MEDJOUL DATES JUMBO', 'RM', 'DATES', 'MEDJOUL-JUMBO', 'D-514', 'Savla Bond', 210.00, 5.000, 1050.00, 'Fresh Stock', 'lot 17418 | inward 05/06/2026 | CFPL | Premium Dates / Medjoul-Jumbo | D-514 @ Savla Bond'),
    (238, 'MEDJOUL DATES JUMBO', 'RM', 'DATES', 'MEDJOUL-JUMBO', 'D-514', 'Savla Bond', 210.00, 5.000, 1050.00, 'Fresh Stock', 'lot 17417 | inward 05/06/2026 | CFPL | Premium Dates / Medjoul-Jumbo | D-514 @ Savla Bond'),
    (239, 'MEDJOUL DATES JUMBO', 'RM', 'DATES', 'MEDJOUL-JUMBO', 'D-514', 'Savla Bond', 210.00, 5.000, 1050.00, 'Fresh Stock', 'lot 17416 | inward 05/06/2026 | CFPL | Premium Dates / Medjoul-Jumbo | D-514 @ Savla Bond'),
    (240, 'MEDJOUL DATES JUMBO', 'RM', 'DATES', 'MEDJOUL-JUMBO', 'D-514', 'Savla Bond', 210.00, 5.000, 1050.00, 'Fresh Stock', 'lot 17415 | inward 05/06/2026 | CFPL | Premium Dates / Medjoul-Jumbo | D-514 @ Savla Bond'),
    (241, 'MEDJOUL DATES JUMBO', 'RM', 'DATES', 'MEDJOUL-JUMBO', 'D-514', 'Savla Bond', 210.00, 5.000, 1050.00, 'Fresh Stock', 'lot 17414 | inward 05/06/2026 | CFPL | Premium Dates / Medjoul-Jumbo | D-514 @ Savla Bond'),
    (242, 'MEDJOUL DATES JUMBO', 'RM', 'DATES', 'MEDJOUL-JUMBO', 'D-514', 'Savla Bond', 210.00, 5.000, 1050.00, 'Fresh Stock', 'lot 17413 | inward 05/06/2026 | CFPL | Premium Dates / Medjoul-Jumbo | D-514 @ Savla Bond'),
    (243, 'MEDJOUL DATES JUMBO', 'RM', 'DATES', 'MEDJOUL-JUMBO', 'D-514', 'Savla Bond', 210.00, 5.000, 1050.00, 'Fresh Stock', 'lot 17410 | inward 05/06/2026 | CFPL | Premium Dates / Medjoul-Jumbo | D-514 @ Savla Bond'),
    (244, 'MEDJOUL DATES JUMBO', 'RM', 'DATES', 'MEDJOUL-JUMBO', 'D-514', 'Savla Bond', 210.00, 5.000, 1050.00, 'Fresh Stock', 'lot 17411 | inward 05/06/2026 | CFPL | Premium Dates / Medjoul-Jumbo | D-514 @ Savla Bond'),
    (245, 'MEDJOUL DATES JUMBO', 'RM', 'DATES', 'MEDJOUL-JUMBO', 'D-514', 'Savla Bond', 210.00, 5.000, 1050.00, 'Fresh Stock', 'lot 17412 | inward 05/06/2026 | CFPL | Premium Dates / Medjoul-Jumbo | D-514 @ Savla Bond'),
    (246, 'MEDJOUL DATES SUPER JUMBO', 'RM', 'DATES', 'MEDJOUL-SUPER JUMBO', 'D-514', 'Savla Bond', 210.00, 5.000, 1050.00, 'Fresh Stock', 'lot 19095 | inward 17/07/2026 | CFPL | Premium Dates / Medjoul-Sup.Jumbo | D-514 @ Savla Bond'),
    (247, 'MEDJOUL DATES SUPER JUMBO', 'RM', 'DATES', 'MEDJOUL-SUPER JUMBO', 'D-514', 'Savla Bond', 170.00, 5.000, 850.00, 'Fresh Stock', 'lot 19096 | inward 17/07/2026 | CFPL | Premium Dates / Medjoul-Sup.Jumbo | D-514 @ Savla Bond'),
    (248, 'MEDJOUL DATES SUPER JUMBO', 'RM', 'DATES', 'MEDJOUL-SUPER JUMBO', 'D-514', 'Savla Bond', 210.00, 5.000, 1050.00, 'Fresh Stock', 'lot 19092 | inward 17/07/2026 | CFPL | Premium Dates / Medjoul-Sup.Jumbo | D-514 @ Savla Bond'),
    (249, 'MEDJOUL DATES SUPER JUMBO', 'RM', 'DATES', 'MEDJOUL-SUPER JUMBO', 'D-514', 'Savla Bond', 210.00, 5.000, 1050.00, 'Fresh Stock', 'lot 19094 | inward 17/07/2026 | CFPL | Premium Dates / Medjoul-Sup.Jumbo | D-514 @ Savla Bond'),
    (250, 'WET DATES (MAZAFATI)', 'RM', 'DATES', 'KIMIA', 'Rishi', 'Rishi', 25.00, 5.000, 125.00, 'Fresh Stock', 'lot 120372 | inward 2025-08-23 00:00:00 | CDPL | Economical Dates / Mazafati | Rishi @ Rishi'),
    (251, 'WET DATES (MAZAFATI)', 'RM', 'DATES', 'KIMIA', 'Rishi', 'Rishi', 42.00, 6.800, 285.60, 'Fresh Stock', 'lot 123292 | inward 2025-12-09 00:00:00 | CDPL | Economical Dates / Mazafati | Rishi @ Rishi'),
    (252, 'WET DATES AJWA', 'RM', 'DATES', 'AJWA', 'Rishi', 'Rishi', 28.00, 5.000, 140.00, 'Fresh Stock', 'lot 123294 | inward 2025-12-09 00:00:00 | CDPL | Premium Dates / Ajwa | Rishi @ Rishi'),
    (253, 'WET DATES (MABROOM)', 'RM', 'DATES', 'MABROOM', 'Rishi', 'Rishi', 2.00, 15.000, 30.00, 'Fresh Stock', 'lot 123295 | inward 2025-12-09 00:00:00 | CDPL | Premium Dates / Mabroom | Rishi @ Rishi'),
    (254, 'AL BARAKAH FARD DATES SEEDLESS', 'FG', 'DATES', 'FARD', 'Rishi', 'Rishi', 76.00, 10.000, 760.00, 'Fresh Stock', 'lot 123327 | inward 2025-12-10 00:00:00 | CDPL | Economical Dates / Fard-Seedless | Rishi @ Rishi'),
    (257, 'WET DATES (MAZAFATI)', 'RM', 'DATES', 'KIMIA', 'Rishi', 'Rishi', 400.00, 6.800, 2720.00, 'Fresh Stock', 'lot 123936 | inward 2025-12-26 00:00:00 | CDPL | Economical Dates / Mazafati-B (Small) | Rishi @ Rishi'),
    (258, 'WET DATES (MAZAFATI)', 'RM', 'DATES', 'KIMIA', 'Rishi', 'Rishi', 264.00, 6.800, 1795.20, 'Fresh Stock', 'lot 124409 | inward 2026-01-08 00:00:00 | CDPL | Economical Dates / Mazafati-B (Small) | Rishi @ Rishi'),
    (260, 'AL BARAKAH FARD DATES SEEDLESS', 'FG', 'DATES', 'FARD', 'Rishi', 'Rishi', 41.00, 10.000, 410.00, 'Fresh Stock', 'lot 125398 | inward 2026-02-17 00:00:00 | CDPL | Economical Dates / Fard-Seedless | Rishi @ Rishi'),
    (262, 'WET DATES SEEDLESS KHALAS', 'RM', 'DATES', 'SEEDLESS', 'Rishi', 'Rishi', 74.00, 10.000, 740.00, 'Fresh Stock', 'lot 125491 | inward 2026-02-21 00:00:00 | CDPL | Economical Dates / Khalas-Seedless | Rishi @ Rishi'),
    (265, 'PIYAROM DATES', 'RM', 'DATES', 'MARYAM', 'Rishi', 'Rishi', 720.00, 5.000, 3600.00, 'Fresh Stock', 'lot 125609 | inward 2026-02-24 00:00:00 | CDPL | Economical Dates / Piyarom-Maryam | Rishi @ Rishi'),
    (271, 'WET DATES SEEDLESS KHALAS', 'RM', 'DATES', 'SEEDLESS', 'Rishi', 'Rishi', 8.00, 10.000, 80.00, 'Fresh Stock', 'lot 125932 | inward 2026-03-10 00:00:00 | CDPL | Economical Dates / Khalas-Seedless | Rishi @ Rishi'),
    (272, 'WET DATES SEEDLESS KHALAS', 'RM', 'DATES', 'SEEDLESS', 'Rishi', 'Rishi', 34.00, 8.200, 278.80, 'Fresh Stock', 'lot 125937 | inward 2026-03-11 00:00:00 | CDPL | Economical Dates / Khalas-Seedless | Rishi @ Rishi'),
    (273, 'WET DATES KHANEIZI', 'RM', 'DATES', 'KHANEIZI', 'Rishi', 'Rishi', 30.00, 10.000, 300.00, 'Fresh Stock', 'lot 126000 | inward 2026-03-13 00:00:00 | CDPL | Economical Dates / Khaneizi | Rishi @ Rishi'),
    (274, 'AL BARAKAH FARD DATES SEEDLESS', 'FG', 'DATES', 'FARD', 'Rishi', 'Rishi', 70.00, 10.000, 700.00, 'Fresh Stock', 'lot 126130 | inward 2026-03-18 00:00:00 | CDPL | Economical Dates / Fard-Seedless | Rishi @ Rishi'),
    (275, 'WET DATES SEEDLESS KHALAS', 'RM', 'DATES', 'SEEDLESS', 'Rishi', 'Rishi', 53.00, 10.000, 530.00, 'Fresh Stock', 'lot 126202 | inward 2026-03-21 00:00:00 | CDPL | Economical Dates / Khalas-Seedless | Rishi @ Rishi'),
    (278, 'AL BARAKAH FARD DATES SEEDLESS', 'FG', 'DATES', 'FARD', 'Rishi', 'Rishi', 38.00, 10.000, 380.00, 'Fresh Stock', 'lot 126267 | inward 2026-03-23 00:00:00 | CDPL | Economical Dates / Fard-Seedless | Rishi @ Rishi'),
    (280, 'WET DATES SEEDLESS KHALAS', 'RM', 'DATES', 'SEEDLESS', 'Rishi', 'Rishi', 113.00, 10.000, 1130.00, 'Fresh Stock', 'lot 126828 | inward 2026-04-15 00:00:00 | CDPL | Economical Dates / Khalas-Seedless | Rishi @ Rishi'),
    (282, 'WET DATES (MAZAFATI)', 'RM', 'DATES', 'KIMIA', 'Rishi', 'Rishi', 144.00, 6.000, 864.00, 'Fresh Stock', 'lot 127026 | inward 2026-04-21 00:00:00 | CDPL | Economical Dates / Mazafati-Shrink Wrap | Rishi @ Rishi'),
    (283, 'WET DATES SEEDLESS KHALAS', 'RM', 'DATES', 'SEEDLESS', 'Rishi', 'Rishi', 5.00, 10.000, 50.00, 'Fresh Stock', 'lot 127437 | inward 2026-05-07 00:00:00 | CDPL | Economical Dates / Khalas-Seedless | Rishi @ Rishi'),
    (284, 'WET DATES (MAZAFATI)', 'RM', 'DATES', 'KIMIA', 'Rishi', 'Rishi', 375.00, 6.500, 2437.50, 'Fresh Stock', 'lot 127487 | inward 2026-05-09 00:00:00 | CDPL | Economical Dates / Mazafati-Shrink Wrap | Rishi @ Rishi'),
    (285, 'WET DATES (MAZAFATI)', 'RM', 'DATES', 'KIMIA', 'Rishi', 'Rishi', 235.00, 6.500, 1527.50, 'Fresh Stock', 'lot 127488 | inward 2026-05-09 00:00:00 | CDPL | Economical Dates / Mazafati | Rishi @ Rishi'),
    (286, 'WET DATES (SUKKARY)', 'RM', 'DATES', 'SUKHARY', 'Rishi', 'Rishi', 104.00, 12.000, 1248.00, 'Fresh Stock', 'lot 127723 | inward 2026-05-16 00:00:00 | CDPL | Premium Dates / Sukkary - Goel Cleaned | Rishi @ Rishi'),
    (288, 'WET DATES (SUKKARY)', 'RM', 'DATES', 'SUKHARY', 'Rishi', 'Rishi', 3.00, 12.000, 36.00, 'Fresh Stock', 'lot 128147 | inward 2026-05-30 00:00:00 | CDPL | Premium Dates / Sukkary - Goel Cleaned | Rishi @ Rishi'),
    (291, 'AL BARAKAH FARD DATES SEEDLESS', 'FG', 'DATES', 'FARD', 'Rishi', 'Rishi', 53.00, 5.000, 265.00, 'Fresh Stock', 'lot 128534 | inward 2026-06-12 00:00:00 | CDPL | Economical Dates / Fard-Seedless | Rishi @ Rishi'),
    (296, 'AL BARAKAH FARD DATES PREMIUM - 10KG', 'RM', 'DATES', 'FARD', 'Rishi', 'Rishi', 268.00, 10.000, 2680.00, 'Fresh Stock', 'lot 129737 | inward 2026-07-24 00:00:00 | CDPL | Economical Dates / Fard-A | Rishi @ Rishi'),
    (297, 'WET DATES ZAHIDI', 'RM', 'DATES', 'ZAHIDI', 'Rishi', 'Rishi', 232.00, 10.000, 2320.00, 'Fresh Stock', 'lot 130223 | inward 2026-08-10 00:00:00 | CDPL | Economical Dates / Zahidi | Rishi @ Rishi'),
    (298, 'WET DATES ZAHIDI', 'RM', 'DATES', 'ZAHIDI', 'Rishi', 'Rishi', 711.00, 10.000, 7110.00, 'Fresh Stock', 'lot 130261 | inward 2026-08-10 00:00:00 | CDPL | Economical Dates / Zahidi | Rishi @ Rishi'),
    (299, 'WET DATES ZAHIDI', 'RM', 'DATES', 'ZAHIDI', 'Rishi', 'Rishi', 700.00, 10.000, 7000.00, 'Fresh Stock', 'lot 130322 | inward 2026-08-13 00:00:00 | CDPL | Economical Dates / Zahidi | Rishi @ Rishi'),
    (300, 'WET DATES SEEDLESS KHALAS', 'RM', 'DATES', 'SEEDLESS', 'Rishi', 'Rishi', 60.00, 10.000, 600.00, 'Fresh Stock', 'lot 130495 | inward 2026-08-20 00:00:00 | CDPL | Economical Dates / Khalas-Seedless | Rishi @ Rishi'),
    (301, 'SAYER DATES', 'RM', 'DATES', 'SAYER', 'Rishi', 'Rishi', 4960.00, 5.000, 24800.00, 'Fresh Stock', 'lot 131554 | inward 2026-10-19 00:00:00 | CDPL | Economical Dates / Sayer dates -Black | Rishi @ Rishi'),
    (302, 'SAYER DATES', 'RM', 'DATES', 'SAYER', 'Rishi', 'Rishi', 230.00, 5.000, 1150.00, 'Fresh Stock', 'lot 131480 | inward 2026-10-19 00:00:00 | CDPL | Economical Dates / Sayer dates -Black | Rishi @ Rishi'),
    (314, 'WET DATES BUNARINJA', 'RM', 'DATES', 'BUNARINJA', 'Eskimo', 'Eskimo', 65.00, 10.000, 650.00, 'Fresh Stock', 'lot 8186 | inward 2026-07-26 00:00:00 | CFPL | Economical Dates / Bunarinja | Eskimo @ Eskimo'),
    (315, 'WET DATES BUNARINJA', 'RM', 'DATES', 'BUNARINJA', 'Eskimo', 'Eskimo', 29.00, 10.000, 290.00, 'Fresh Stock', 'lot 8187 | inward 2026-07-26 00:00:00 | CFPL | Economical Dates / Bunarinja | Eskimo @ Eskimo'),
    (316, 'WET DATES BUMAAN', 'RM', 'DATES', 'BUMAN', 'Eskimo', 'Eskimo', 66.00, 10.000, 660.00, 'Fresh Stock', 'lot 8188 | inward 2026-07-26 00:00:00 | CFPL | Economical Dates / Buman | Eskimo @ Eskimo'),
    (317, 'WET DATES BUMAAN', 'RM', 'DATES', 'BUMAN', 'Eskimo', 'Eskimo', 22.00, 10.000, 220.00, 'Fresh Stock', 'lot 8189 | inward 2026-07-26 00:00:00 | CFPL | Economical Dates / Buman | Eskimo @ Eskimo'),
    (325, 'AL BARAKAH BARHI DATES 10KG', 'RM', 'DATES', 'BARHI', 'Eskimo', 'Eskimo', 730.00, 10.000, 7300.00, 'Fresh Stock', 'lot 8247 | inward 2026-08-24 00:00:00 | CDPL | Economical Dates / Barhi | Eskimo @ Eskimo');

-- ── 1. The report ─────────────────────────────────────────────────────────
INSERT INTO new_stock_entries (
    item_name, item_type, item_category, item_subcategory,
    warehouse, floor_name, total_quantity, unit_uom, total_weight,
    entered_by, authority, created_at, updated_at,
    stock_type, entry_id, status, is_checked, verified, remark, source_kind
)
SELECT item_name, item_type, item_category, item_subcategory,
       warehouse, floor_name, total_quantity, unit_uom, total_weight,
       'Kaushal', 'Cold storage report (Savla / Rishi / Eskimo) as on 22-10-2026 - opening stock 23-10-2026',
       TIMESTAMPTZ '2026-10-23 09:00:00+05:30', TIMESTAMPTZ '2026-10-23 09:00:00+05:30',
       stock_type, 'COLD1023', 'submitted', FALSE, FALSE, remark, 'COUNT'
  FROM cold_load
 ORDER BY xl_row;

-- ── 2. Zero what the report no longer holds ───────────────────────────────
-- Balance before 23 Oct, computed the way latest_stock_service does.
WITH places(k_wh, k_fl) AS (VALUES ('D39', 'SAVLA'), ('D514', 'SAVLA'), ('D514', 'SAVLA BOND'), ('RISHI', 'RISHI'), ('ESKIMO', 'ESKIMO')),
cnt AS (
    SELECT UPPER(BTRIM(item_name)) AS k_item,
           COALESCE(stock_type, 'Fresh Stock') AS k_stock,
           REPLACE(UPPER(BTRIM(warehouse)), '-', '') AS k_wh,
           UPPER(BTRIM(COALESCE(floor_name, ''))) AS k_fl,
           (created_at AT TIME ZONE 'Asia/Kolkata')::date AS count_day,
           SUM(total_weight) AS w,
           MIN(item_name) AS item_name, MIN(item_type) AS item_type,
           MIN(item_category) AS item_category, MIN(item_subcategory) AS item_subcategory
      FROM new_stock_entries
     WHERE (source_kind IS NULL OR source_kind = 'COUNT')
       AND (status IS NULL OR status <> 'draft')
       AND (created_at AT TIME ZONE 'Asia/Kolkata')::date < DATE '2026-10-23'
       AND (REPLACE(UPPER(BTRIM(warehouse)), '-', ''), UPPER(BTRIM(COALESCE(floor_name, ''))))
           IN (SELECT k_wh, k_fl FROM places)
     GROUP BY 1, 2, 3, 4, 5
),
latest AS (
    SELECT DISTINCT ON (k_item, k_stock, k_wh, k_fl) *
      FROM cnt
     ORDER BY k_item, k_stock, k_wh, k_fl, count_day DESC
),
txn AS (
    SELECT UPPER(BTRIM(t.item_name)) AS k_item,
           COALESCE(t.stock_type, 'Fresh Stock') AS k_stock,
           REPLACE(UPPER(BTRIM(t.warehouse)), '-', '') AS k_wh,
           UPPER(BTRIM(COALESCE(t.location, ''))) AS k_fl,
           SUM(CASE WHEN t.operation = 'ADDITION' THEN t.qty_kg ELSE -t.qty_kg END) AS net_kg,
           MIN(t.item_name) AS item_name, MIN(t.material_type) AS item_type,
           MIN(t.item_category) AS item_category, MIN(t.item_subcategory) AS item_subcategory
      FROM stocktake_transactions t
      LEFT JOIN latest l
             ON l.k_item = UPPER(BTRIM(t.item_name))
            AND l.k_stock = COALESCE(t.stock_type, 'Fresh Stock')
            AND l.k_wh = REPLACE(UPPER(BTRIM(t.warehouse)), '-', '')
            AND l.k_fl = UPPER(BTRIM(COALESCE(t.location, '')))
     WHERE (t.created_at AT TIME ZONE 'Asia/Kolkata')::date < DATE '2026-10-23'
       AND (l.count_day IS NULL OR (t.created_at AT TIME ZONE 'Asia/Kolkata')::date >= l.count_day)
       AND (REPLACE(UPPER(BTRIM(t.warehouse)), '-', ''), UPPER(BTRIM(COALESCE(t.location, ''))))
           IN (SELECT k_wh, k_fl FROM places)
     GROUP BY 1, 2, 3, 4
),
on_hand AS (
    SELECT COALESCE(l.k_item, t.k_item) AS k_item,
           COALESCE(l.k_stock, t.k_stock) AS k_stock,
           COALESCE(l.k_wh, t.k_wh) AS k_wh,
           COALESCE(l.k_fl, t.k_fl) AS k_fl,
           COALESCE(l.item_name, t.item_name) AS item_name,
           COALESCE(l.item_type, t.item_type) AS item_type,
           COALESCE(l.item_category, t.item_category) AS item_category,
           COALESCE(l.item_subcategory, t.item_subcategory) AS item_subcategory,
           COALESCE(l.w, 0) + COALESCE(t.net_kg, 0) AS kg,
           l.count_day
      FROM latest l
      FULL JOIN txn t USING (k_item, k_stock, k_wh, k_fl)
)
INSERT INTO new_stock_entries (
    item_name, item_type, item_category, item_subcategory,
    warehouse, floor_name, total_quantity, unit_uom, total_weight,
    entered_by, authority, created_at, updated_at,
    stock_type, entry_id, status, is_checked, verified, remark, source_kind
)
SELECT o.item_name, o.item_type, o.item_category, o.item_subcategory,
       p.wh, p.fl, 0, 0, 0,
       'Kaushal', 'Cold storage report (Savla / Rishi / Eskimo) as on 22-10-2026 - opening stock 23-10-2026',
       TIMESTAMPTZ '2026-10-23 09:00:00+05:30', TIMESTAMPTZ '2026-10-23 09:00:00+05:30',
       o.k_stock, 'COLD1023', 'submitted', FALSE, FALSE,
       LEFT('Not in cold storage report as on 22-10-2026 - set to 0 (was '
            || ROUND(o.kg, 2) || ' kg, last counted ' || COALESCE(o.count_day::text, 'never') || ')', 255),
       'COUNT'
  FROM on_hand o
  JOIN (VALUES ('D39', 'SAVLA', 'D-39', 'Savla'), ('D514', 'SAVLA', 'D-514', 'Savla'), ('D514', 'SAVLA BOND', 'D-514', 'Savla Bond'), ('RISHI', 'RISHI', 'Rishi', 'Rishi'), ('ESKIMO', 'ESKIMO', 'Eskimo', 'Eskimo')) AS p(k_wh, k_fl, wh, fl) ON p.k_wh = o.k_wh AND p.k_fl = o.k_fl
 WHERE ROUND(o.kg, 2) <> 0
   AND NOT EXISTS (
        SELECT 1 FROM cold_load c
         WHERE UPPER(BTRIM(c.item_name)) = o.k_item
           AND c.stock_type = o.k_stock
           AND REPLACE(UPPER(c.warehouse), '-', '') = o.k_wh
           AND UPPER(c.floor_name) = o.k_fl)
 ORDER BY p.wh, p.fl, o.item_name;

-- ── Check: should match EXPECTED in the header (zeroed lines show 0 kg) ──
SELECT warehouse, floor_name,
       COUNT(*) FILTER (WHERE total_weight <> 0) AS report_lines,
       COUNT(*) FILTER (WHERE total_weight = 0)  AS zeroed_articles,
       SUM(total_weight)                          AS kg
  FROM new_stock_entries
 WHERE entry_id = 'COLD1023'
 GROUP BY 1, 2
 ORDER BY 1, 2;

-- Articles zeroed by step 2, with what they showed before.
SELECT warehouse, floor_name, item_name, stock_type, remark
  FROM new_stock_entries
 WHERE entry_id = 'COLD1023' AND total_weight = 0
 ORDER BY 1, 2, 3;

-- Loaded article names the live catalogue does not know (expected: none).
SELECT DISTINCT n.item_name
  FROM new_stock_entries n
 WHERE n.entry_id = 'COLD1023'
   AND NOT EXISTS (SELECT 1 FROM all_sku s
                    WHERE UPPER(BTRIM(s.particulars)) = UPPER(BTRIM(n.item_name)));

COMMIT;
