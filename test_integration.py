"""
test_integration.py -- offline integration test for database + scraper logic.

Exercises: slug generation, upsert_fragrance, insert_offer, query_offers,
distinct_values, all_notes, stats, and scraper helper functions
(clean_price, size_to_ml, ppm).  No network access required.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import database as db
import scraper

passed = 0
failed = 0


def check(label, condition):
    global passed, failed
    if condition:
        passed += 1
        print(f"  OK  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}")


# ---- Scraper helpers ----

check("clean_price('$49.99')", scraper.clean_price("$49.99") == 49.99)
check("clean_price('USD 1,299.00')", scraper.clean_price("USD 1,299.00") == 1299.0)
check("clean_price(None)", scraper.clean_price(None) is None)
check("clean_price('')", scraper.clean_price("") is None)

check("size_to_ml('100ml')", scraper.size_to_ml("100ml") == 100.0)
check("size_to_ml('3.4 oz')", scraper.size_to_ml("3.4 oz") == round(3.4 * 29.5735, 1))
check("size_to_ml('no size')", scraper.size_to_ml("no size") is None)

check("ppm(100, 50)", scraper.ppm(100, 50) == 2.0)
check("ppm(None, 50)", scraper.ppm(None, 50) is None)
check("ppm(100, 0)", scraper.ppm(100, 0) is None)

# ---- Database layer ----

# Use a temp DB so we don't touch the real one
tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
tmp.close()
try:
    db.init_db(tmp.name)
    conn = db.connect(tmp.name)

    # Slug generation
    check("make_slug basic", db.make_slug("Profumum Roma", "Olibanum") == "profumum roma|olibanum")
    check("make_slug strips concentration", "edp" not in db.make_slug("House", "Rose EDP"))

    # Insert a fragrance
    fid = db.upsert_fragrance(conn, {
        "brand": "TestBrand", "name": "TestScent",
        "perfumer": "Nose One", "year": 2020, "gender": "unisex",
        "rating_parfumo": 8.1, "rating_fragrantica": 7.5,
        "top_notes": ["bergamot", "lemon"],
        "middle_notes": ["rose", "jasmine"],
        "base_notes": ["musk"],
        "main_accords": ["citrus", "floral"],
    })
    check("upsert_fragrance returns id", fid is not None and fid > 0)

    # Re-upsert same slug updates rather than duplicates
    fid2 = db.upsert_fragrance(conn, {
        "brand": "TestBrand", "name": "TestScent",
        "rating_parfumo": 8.5,
    })
    check("upsert idempotent", fid2 == fid)

    # Check that the rating was updated
    row = conn.execute("SELECT rating_parfumo FROM fragrances WHERE id = ?", (fid,)).fetchone()
    check("upsert updates fields", row[0] == 8.5)

    # Insert offers
    oid1 = db.insert_offer(conn, {
        "retailer": "Jomashop", "input_brand": "TestBrand", "input_name": "TestScent",
        "variant_title": "TestScent 100ml EDP", "size_ml": 100, "size_oz": 3.38,
        "original_price": 200.0, "sale_price": 150.0, "discount_pct": 25.0,
        "price_per_ml": 1.5, "in_stock": True,
        "product_url": "https://example.com/test-100ml",
    }, fid)
    check("insert_offer returns id", oid1 > 0)

    oid2 = db.insert_offer(conn, {
        "retailer": "LuckyScent", "input_brand": "TestBrand", "input_name": "TestScent",
        "variant_title": "TestScent 50ml EDP", "size_ml": 50,
        "original_price": 120.0, "sale_price": 120.0,
        "price_per_ml": 2.4, "in_stock": True,
        "product_url": "https://example.com/test-50ml",
    }, fid)
    check("second offer inserted", oid2 > 0)

    # Stats
    s = db.stats(conn)
    check("stats fragrances=1", s["fragrances"] == 1)
    check("stats offers=2", s["offers"] == 2)
    check("stats retailers=2", s["retailers"] == 2)
    check("stats enriched=1", s["enriched"] == 1)

    # Query with no filters
    rows = db.query_offers(conn, {})
    check("query_offers returns 2 rows", len(rows) == 2)
    check("query sorted by ppm asc", rows[0]["price_per_ml"] <= rows[1]["price_per_ml"])

    # Filter by retailer
    rows = db.query_offers(conn, {"retailers": ["Jomashop"]})
    check("filter by retailer", len(rows) == 1 and rows[0]["retailer"] == "Jomashop")

    # Filter by min_ppm
    rows = db.query_offers(conn, {"min_ppm": 2.0})
    check("filter by min_ppm", len(rows) == 1 and rows[0]["price_per_ml"] == 2.4)

    # Filter by search text
    rows = db.query_offers(conn, {"search_text": "TestBrand"})
    check("filter by search_text", len(rows) == 2)

    # Distinct values
    brands = db.distinct_values(conn, "brand")
    check("distinct_values brand", brands == ["TestBrand"])

    retailers = db.distinct_values(conn, "retailer", "offers")
    check("distinct_values retailer", set(retailers) == {"Jomashop", "LuckyScent"})

    # All notes
    notes = db.all_notes(conn)
    check("all_notes returns notes", "Bergamot" in notes and "Rose" in notes and "Musk" in notes)
    check("all_notes includes accords", "Citrus" in notes and "Floral" in notes)

    conn.close()
finally:
    os.unlink(tmp.name)
    # Clean up WAL/SHM files if they exist
    for ext in (".db-wal", ".db-shm"):
        p = tmp.name.replace(".db", "") + ext
        if os.path.exists(p):
            os.unlink(p)

# ---- Summary ----
print(f"\n{'='*50}")
print(f"  {passed} passed, {failed} failed, {passed+failed} total")
if failed:
    print("  SOME TESTS FAILED")
    sys.exit(1)
else:
    print("  ALL TESTS PASSED")
