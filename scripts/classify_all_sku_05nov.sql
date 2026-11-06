-- Classifies the all_sku articles Stock Take refuses because type, category and
-- sub-category are empty. Values come from the "To classify" rows of
-- backend/scratch/all_sku_classification_review_05Nov2026.xlsx.
--
-- Run once on RDS. It fills only rows where all three fields are still empty, so
-- an article already classified is left alone. Expect: UPDATE 109

UPDATE all_sku AS a
   SET item_type = v.item_type, item_group = v.item_group, sub_group = v.sub_group
  FROM (VALUES
    -- Low confidence: best guesses. Delete any line you'd rather check first.
    (4091, 'fg', 'cranberry', 'cranberry - whole'),  -- Carnival Masala Masti Cranberry 100gm
    (4104, 'fg', 'trail mix', 'chocolate'),  -- Del monte Assorted Choco Dates (120gx12)
    (4105, 'fg', 'trail mix', 'chocolate'),  -- Del Monte Dark Choco Dates (160gx12)
    (4106, 'fg', 'trail mix', 'chocolate'),  -- Del Monte Milk Choco Dates (160gx12)
    (4122, 'rm', 'miscellaneous - rm', 'other misc'),  -- Green Tea / Other Tea Sachet Etc
    (4170, 'rm', 'miscellaneous - rm', 'other misc'),  -- Sugar Sachets
    -- Medium confidence: same product family, a judgement call.
    (4033, 'pm', 'packaging', 'jar'),  -- PM24- Bombay Sweet 200ml Jar With Alumn Golden Cap & Wad
    (4038, 'pm', 'packaging', 'pm'),  -- PM24-Del Monte LEAFLETS (USER MANNUAL)
    (4062, 'pm', 'packaging', 'roll'),  -- PM24_Zydus_Max Protein Date Bytes Choco Fudge Laminate Roll
    (4064, 'pm', 'packaging', 'roll'),  -- PM24_Zydus_Max Protein Date Bytes Nuts & Seeds Laminate Roll
    (4098, 'rm', 'dates', 'deglet'),  -- Dates Deglet Loose
    (4099, 'rm', 'dates', 'khalas'),  -- Dates Khalas Loose
    (4100, 'rm', 'dates', 'khaneizi'),  -- Dates Khaneizi Loose
    (4101, 'rm', 'dates', 'mabroom'),  -- Dates Mabroom Loose
    (4102, 'rm', 'dates', 'maryam'),  -- Dates Maryam Loose
    (4112, 'fg', 'premium nuts', 'macadamia nuts'),  -- Draggees Macademia Dark (50 GM)
    (4113, 'fg', 'premium nuts', 'macadamia nuts'),  -- Drangees Dark Macademia
    (4121, 'fg', 'seeds', 'watermelon'),  -- Good Life Magaj Seeds 500 G PP
    (4156, 'fg', 'peanuts', 'peanut - whole'),  -- Protein Chef Roasted Peanut Pudina 90g
    (4159, 'fg', 'bars & cereals', 'bites'),  -- RB Date Bites Choco fudge Monocarton (Date Bite 20 g x 12 pcs)
    (4160, 'fg', 'bars & cereals', 'bites'),  -- RB Date Bites Choco fudge Tin (Date Bite 20 g x 10 pcs)
    (4162, 'fg', 'bars & cereals', 'bites'),  -- RB Date Bites Classic Monocarton (Date Bite 20 g x 12 pcs)
    (4163, 'fg', 'bars & cereals', 'bites'),  -- RB Date Bites Classic Tin (Date Bite 20 g x 10 pcs)
    (4164, 'rm', 'dates', 'zahidi'),  -- Roasted Seedless Zahidi
    -- High confidence: the name or a near-identical article settles it.
    (4032, 'pm', 'packaging', 'pouch'),  -- PM24-Al Barakah Arabian Dates Standy Zipper Pouch 500gm (150x225x70mm)
    (4034, 'pm', 'packaging', 'label'),  -- PM24- Bombay Sweet Himalayan Pink Salt Cashews Label
    (4035, 'pm', 'packaging', 'tape'),  -- PM24-Bopp Adhesive Unprinted Tape Green
    (4036, 'pm', 'packaging', 'tape'),  -- PM24-Bopp Adhesive Unprinted Tape Red
    (4037, 'pm', 'packaging', 'pouch'),  -- PM24-Carnival-Standy Zipper Pouch -150×225mm-Sliced Cranberry-250g
    (4039, 'pm', 'packaging', 'pouch'),  -- PM24-D Mart Healthy Choice Unroasted Pumpkin Seeds 100g (115x160mm) Standy Pouch
    (4040, 'pm', 'packaging', 'pouch'),  -- PM24-D Mart Healthy Choice Unroasted Sunflower Seeds 100g (115x160mm) Standy Pouch
    (4041, 'pm', 'packaging', 'pouch'),  -- PM24-D Mart Healthy Choice Unroasted Watermelon Seeds 100g (115x160mm) Standy Pouch
    (4042, 'pm', 'packaging', 'pouch'),  -- PM24-Dmart Standy Zipper Pouch -150×225mm-Sliced American Cranberry-200g
    (4043, 'pm', 'packaging', 'pouch'),  -- PM24-Good Life Tarbooz Magaz Seeds 100g pouch (GL DFH-DF1)
    (4044, 'pm', 'packaging', 'label'),  -- PM24-Label Roll Al Barakah Date Syrup 400g -2 Sides
    (4045, 'pm', 'packaging', 'pouch'),  -- PM24-Medplus MultiColour Digi Prntd Pouch 85x50mm_Pepper Cashew 30 gms
    (4046, 'pm', 'packaging', 'pouch'),  -- PM24-Medplus MultiColour Digi Prntd Pouch 85x50mm_Peri Peri Cashew 30 gms
    (4047, 'pm', 'packaging', 'pouch'),  -- PM24-Medplus MultiColour Digi Prntd Pouch 85x50mm_Salted Almond
    (4048, 'pm', 'packaging', 'pouch'),  -- PM24-Medplus MultiColour Digi Prntd Pouch 85x50mm_Salted Cashew 30 gms
    (4049, 'pm', 'packaging', 'pet'),  -- PM24-Pet Bottles 400g (Date Syrup) with gold FTC with Valve
    (4050, 'pm', 'packaging', 'pouch'),  -- PM24-PM DF GL SP-A0 (100X145) POUCH
    (4051, 'pm', 'packaging', 'pouch'),  -- PM24-PM DF GL SP-A1 (135X200) POUCH
    (4052, 'pm', 'packaging', 'pouch'),  -- PM24-PM DF Value Pack DFH A1 (135X200) POUCH
    (4053, 'pm', 'packaging', 'pouch'),  -- PM24-PM QR PCH GL DF4 NBL NI HND (170X260) POUCH
    (4054, 'pm', 'packaging', 'top'),  -- PM24-Premium Emirates Dates 500gm Top Film Thermo pack
    (4055, 'pm', 'packaging', 'top'),  -- PM24-Premium Emirates Dates Seedless 500gm Top Film Thermo pack
    (4056, 'pm', 'packaging', 'label'),  -- PM24-Self-Adhesive PP Matte Synthetic Label Roll, 100 mm × 30 m, 1"
    (4057, 'pm', 'packaging', 'bottom'),  -- PM24-Thermo. Bottom Web Unprinted 323 mm X 200 Mic
    (4058, 'pm', 'packaging', 'top'),  -- PM24-Thermo. Top Web Unprinted 312mm X 80 mic
    (4059, 'pm', 'packaging', 'tape'),  -- PM24 - Unprinted BOPP Tape 100gm_new
    (4060, 'pm', 'packaging', 'pouch roll'),  -- PM24-Vedaka Purple Pouch Roll 500GM
    (4061, 'pm', 'packaging', 'tray'),  -- PM24-Zydus_Tray_RPET_46x46x20mm with 435 mic
    (4063, 'pm', 'packaging', 'tin'),  -- PM24_Zydus_Max Protein Date Bytes Choco Fudge Tin Cans
    (4065, 'pm', 'packaging', 'tin'),  -- PM24_Zydus_Max Protein Date Bytes Nuts & Seeds Tin Cans
    (4085, 'rm', 'dates', 'diced'),  -- AL BARAKAH CHOPPED DATES 10KG
    (4086, 'rm', 'dates', 'syrup'),  -- AL BARAKAH DATE SYRUP 25KG
    (4087, 'fg', 'dates', 'khalas'),  -- Al Barakah Khalas Dates 400g (B0DGLN47YS)
    (4088, 'fg', 'dates', 'safavi'),  -- Al Barakah Safawi Dates/Kalmi Dates 400g (B0C3D8DB7M)
    (4089, 'fg', 'festive hampers', 'gifting'),  -- Carnival Blissful Celebration 400gm
    (4090, 'fg', 'festive hampers', 'gifting'),  -- Carnival Madhur Mahotsav 375gm
    (4092, 'fg', 'peanuts', 'peanut - coated'),  -- Coated Peanuts Masala Bulk
    (4094, 'fg', 'peanuts', 'peanut - coated'),  -- Coated Peanuts Pudina Bulk
    (4095, 'fg', 'peanuts', 'peanut - coated'),  -- Coated Peanuts Wasabi Bulk
    (4096, 'rm', 'miscellaneous - rm', 'other misc'),  -- Cocoa Mass Alkalised
    (4107, 'fg', 'dates', 'zahidi'),  -- Delmonte Royal Arabian Zahidi Dates 450gm pack of 2 [1Case x 12 Pack]
    (4111, 'fg', 'almond', 'almond - kernels'),  -- Draggees Almond Milk (50 GM)
    (4114, 'fg', 'anjeer', 'dried figs'),  -- Dried Anjeer 250gm
    (4115, 'fg', 'anjeer', 'dried figs'),  -- Dried Anjeer 500gm
    (4118, 'fg', 'trail mix', 'trail mix'),  -- Garlic Bread Trail Mix 25gm
    (4119, 'fg', 'seeds', 'flax'),  -- GOOD LIFE FLAX SEEDS 200G PP
    (4120, 'fg', 'seeds', 'flax'),  -- GOOD LIFE FLAX SEEDS ALSI 100 g PP
    (4123, 'fg', 'trail mix', 'trail mix'),  -- Italian Herb Trail Mix 25gm
    (4124, 'fg', 'trail mix', 'trail mix'),  -- Jalapeno Trail Mix 25gm
    (4126, 'fg', 'trail mix', 'trail mix'),  -- Mexican Salsa Trail Mix 25gm
    (4127, 'rm', 'seasoning', 'seasoning'),  -- MINT SEASONING
    (4128, 'fg', 'trail mix', 'trail mix'),  -- Natch Glazed Nut Mix Chilli 120g
    (4129, 'fg', 'trail mix', 'trail mix'),  -- Natch Glazed Nut Mix Cinnamon 120g
    (4130, 'fg', 'trail mix', 'trail mix'),  -- Natch Glazed Nut Mix Cocoa 120g
    (4131, 'fg', 'almond', 'almond - mamra'),  -- NB MAMRA ALMOND ROYAL LOOSE
    (4135, 'fg', 'prunes', 'prunes - pitted'),  -- PL California Prunes 200gm
    (4137, 'pm', 'packaging', 'carton'),  -- PM24-3 Ply Carton Panch Mewa 350x350x170MM (16 PACK JAR)-plain
    (4138, 'pm', 'packaging', 'carton'),  -- PM24 - 5 Ply Carton _Delmonte Choco Dates Standee Pouch (310x290x260)
    (4139, 'pm', 'packaging', 'carton'),  -- PM24 - 5 Ply Carton_ Delmonte Choco Dates Tin (335x335x140mm with partition)
    (4140, 'pm', 'packaging', 'hamper-box'),  -- PM24-Blissful Celebration 4P Festive Box
    (4141, 'pm', 'packaging', 'monocarton'),  -- PM24-Del Monte Milk Choco Dates Outer Sleeve 414x158 Mono Carton, FBB
    (4142, 'pm', 'packaging', 'carton'),  -- PM24-EATRITE Flavoured Nuts 3 Ply Master Carton (365x315x303mm)
    (4143, 'pm', 'packaging', 'hamper-box'),  -- PM24-Madhur Mahotsav 4P Festive Box
    (4144, 'pm', 'packaging', 'hamper-box'),  -- PM24-Mangalmay Utsav 6P Festive Box
    (4145, 'pm', 'packaging', 'carton'),  -- PM24-PM24-5 Ply Carton Carnival Anjeer Carton 10 Kg (425X320X190)OD
    (4146, 'pm', 'packaging', 'hamper-box'),  -- PM24-Shahi Utsav 4P Festive Box
    (4147, 'pm', 'packaging', 'hamper-box'),  -- PM24-Shubhkamanaye 3P Festive Box
    (4148, 'pm', 'packaging', 'monocarton'),  -- PM24-Zydus RB date Bites Choco fudge 100x80x186 mm Mono Carton
    (4149, 'pm', 'packaging', 'monocarton'),  -- PM24-Zydus RB Date Bites Classic 100x80x186 mm Mono Carton
    (4150, 'pm', 'packaging', 'carton'),  -- PM24-Zydus_5 Ply Carton 440x440x150mm RB Dates bites Tin pack 16x(10x20g)
    (4151, 'pm', 'packaging', 'carton'),  -- PM24-Zydus_5 Ply Carton 500x290x140mm RB Dates bites 12x(12x20g)
    (4152, 'fg', 'bars & cereals', 'bites'),  -- PRAWA DATE BITES CLASSIC (21gm)
    (4153, 'fg', 'bars & cereals', 'bites'),  -- PRAWA DATE BITES ROSE (21 GM)
    (4155, 'fg', 'peanuts', 'peanut - coated'),  -- Protein Chef Coated Peanut Pudina 50g
    (4157, 'fg', 'trail mix', 'trail mix'),  -- Rajsi Panchmewa 100gm
    (4158, 'fg', 'bars & cereals', 'bites'),  -- RB Date Bites Choco fudge 20g
    (4161, 'fg', 'bars & cereals', 'bites'),  -- RB Date Bites Classic 20g
    (4165, 'fg', 'cashew', 'cashew - kernel'),  -- RTS HIMALAYAN PINK SALT CASHEWS 90g
    (4166, 'fg', 'cranberry', 'cranberry - sliced'),  -- Sliced American Cranberry 200gm
    (4169, 'fg', 'bars & cereals', 'energy bars'),  -- Strawberry Date Fruit Bar Loose
    (4171, 'fg', 'trail mix', 'trail mix'),  -- The Good Bug Seed Mix 250gm
    (4172, 'fg', 'almond', 'almond - kernels'),  -- Vedaka California Almonds 200gm (B07GQNLYBN)
    (4173, 'fg', 'almond', 'almond - kernels'),  -- Vedaka Premium Whole Almonds 500g ( B01LPY9R08 )
    (4174, 'fg', 'cashew', 'cashew - kernel'),  -- Vedaka Roasted & Salted Cashews 200g ( B07GR346FP )
    (4176, 'fg', 'bars & cereals', 'energy bars')  -- Wholesome Millet Laddu (Semi)
  ) AS v(sku_id, item_type, item_group, sub_group)
 WHERE a.sku_id = v.sku_id
   AND a.item_type IS NULL AND a.item_group IS NULL AND a.sub_group IS NULL;

-- To undo, run this on its own:
-- UPDATE all_sku SET item_type = NULL, item_group = NULL, sub_group = NULL WHERE sku_id IN (4032, 4033, 4034, 4035, 4036, 4037, 4038, 4039, 4040, 4041, 4042, 4043, 4044, 4045, 4046, 4047, 4048, 4049, 4050, 4051, 4052, 4053, 4054, 4055, 4056, 4057, 4058, 4059, 4060, 4061, 4062, 4063, 4064, 4065, 4085, 4086, 4087, 4088, 4089, 4090, 4091, 4092, 4094, 4095, 4096, 4098, 4099, 4100, 4101, 4102, 4104, 4105, 4106, 4107, 4111, 4112, 4113, 4114, 4115, 4118, 4119, 4120, 4121, 4122, 4123, 4124, 4126, 4127, 4128, 4129, 4130, 4131, 4135, 4137, 4138, 4139, 4140, 4141, 4142, 4143, 4144, 4145, 4146, 4147, 4148, 4149, 4150, 4151, 4152, 4153, 4155, 4156, 4157, 4158, 4159, 4160, 4161, 4162, 4163, 4164, 4165, 4166, 4169, 4170, 4171, 4172, 4173, 4174, 4176);
