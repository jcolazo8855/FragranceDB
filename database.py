"""
database.py — SQLite storage for the fragrance scraper.

Two main tables:
  fragrances  — one row per unique brand+name (characteristics from Parfumo/Fragrantica)
  offers      — one row per retail size/variant (price data from Jomashop/FNet/LuckyScent)

A fragrance has many offers (different sizes, retailers).
Filtering in the GUI runs SQL against a JOIN of these two tables.
"""

import sqlite3
import json
import os
import re
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fragrances.db")


# ─────────────────────────────────────────────────────────────────────────────
#  Schema
# ─────────────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS fragrances (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    brand               TEXT NOT NULL,
    name                TEXT NOT NULL,
    slug                TEXT UNIQUE NOT NULL,          -- normalized 'brand|name' dedup key
    perfumer            TEXT,
    year                INTEGER,
    gender              TEXT,                          -- men / women / unisex
    concentration       TEXT,                          -- EDP / EDT / Extrait / Parfum
    fragrance_family    TEXT,
    top_notes           TEXT,                          -- JSON array
    middle_notes        TEXT,                          -- JSON array
    base_notes          TEXT,                          -- JSON array
    main_accords        TEXT,                          -- JSON array
    rating_parfumo      REAL,
    votes_parfumo       INTEGER,
    rating_fragrantica  REAL,
    votes_fragrantica   INTEGER,
    longevity           TEXT,
    sillage             TEXT,
    description         TEXT,
    url_parfumo         TEXT,
    url_fragrantica     TEXT,
    image_url           TEXT,
    enriched_at         TEXT
);

CREATE TABLE IF NOT EXISTS offers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fragrance_id    INTEGER REFERENCES fragrances(id) ON DELETE CASCADE,
    input_brand     TEXT,
    input_name      TEXT,
    retailer        TEXT NOT NULL,                     -- Jomashop / FragranceNet / LuckyScent
    variant_title   TEXT,
    size_ml         REAL,
    size_oz         REAL,
    concentration   TEXT,
    original_price  REAL,
    sale_price      REAL,
    discount_pct    REAL,
    price_per_ml    REAL,
    in_stock        INTEGER DEFAULT 1,
    product_url     TEXT,
    image_url       TEXT,
    scraped_at      TEXT
);

-- Expression-based unique index: COALESCE makes NULL size_ml deduplicate too
-- (a plain UNIQUE constraint treats NULLs as distinct in SQLite).
CREATE UNIQUE INDEX IF NOT EXISTS uq_offers_dedup
    ON offers(retailer, COALESCE(product_url,''), COALESCE(size_ml,-1));

CREATE INDEX IF NOT EXISTS idx_offers_fragrance ON offers(fragrance_id);
CREATE INDEX IF NOT EXISTS idx_offers_retailer  ON offers(retailer);
CREATE INDEX IF NOT EXISTS idx_offers_ppm        ON offers(price_per_ml);
CREATE INDEX IF NOT EXISTS idx_frag_brand        ON fragrances(brand);
"""


# ─────────────────────────────────────────────────────────────────────────────
#  Connection / init
# ─────────────────────────────────────────────────────────────────────────────

def connect(db_path: str = DB_PATH) -> sqlite3.Connection:
    # check_same_thread=False: Streamlit reruns scripts on different threads
    # while caching the connection; SQLite's own locking keeps this safe for
    # our single-writer (scraper) + readers (GUI) pattern.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets the GUI read while the scraper subprocess writes.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db(db_path: str = DB_PATH) -> None:
    conn = connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

def make_slug(brand: str, name: str) -> str:
    """Normalized dedup key, e.g. 'profumum roma|olibanum'."""
    def norm(s):
        s = (s or "").lower().strip()
        s = re.sub(r"\b(eau de (parfum|toilette|cologne)|edp|edt|edc|extrait|parfum|spray)\b", "", s)
        s = re.sub(r"[^a-z0-9 ]", "", s)
        return re.sub(r"\s+", " ", s).strip()

    b, n = norm(brand), norm(name)
    # If noise-stripping removed everything, fall back to the raw text so two
    # different all-noise inputs (e.g. 'EDP'/'Spray') don't collide on '|'.
    if not b and brand:
        b = re.sub(r"[^a-z0-9 ]", "", brand.lower()).strip()
    if not n and name:
        n = re.sub(r"[^a-z0-9 ]", "", name.lower()).strip()
    return f"{b}|{n}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value) -> str:
    """Serialize a list/dict to JSON text; pass through strings."""
    if value is None:
        return None
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


# ─────────────────────────────────────────────────────────────────────────────
#  Upserts
# ─────────────────────────────────────────────────────────────────────────────

def upsert_fragrance(conn: sqlite3.Connection, frag: dict) -> int:
    """
    Insert or update a fragrance by slug. Returns fragrance_id.
    Only overwrites existing fields when the new value is non-empty.
    """
    slug = make_slug(frag.get("brand", ""), frag.get("name", ""))
    cur = conn.execute("SELECT * FROM fragrances WHERE slug = ?", (slug,))
    existing = cur.fetchone()

    fields = {
        "brand":              frag.get("brand"),
        "name":               frag.get("name"),
        "slug":               slug,
        "perfumer":           frag.get("perfumer"),
        "year":               frag.get("year"),
        "gender":             frag.get("gender"),
        "concentration":      frag.get("concentration"),
        "fragrance_family":   frag.get("fragrance_family"),
        "top_notes":          _json(frag.get("top_notes")),
        "middle_notes":       _json(frag.get("middle_notes")),
        "base_notes":         _json(frag.get("base_notes")),
        "main_accords":       _json(frag.get("main_accords")),
        "rating_parfumo":     frag.get("rating_parfumo"),
        "votes_parfumo":      frag.get("votes_parfumo"),
        "rating_fragrantica": frag.get("rating_fragrantica"),
        "votes_fragrantica":  frag.get("votes_fragrantica"),
        "longevity":          frag.get("longevity"),
        "sillage":            frag.get("sillage"),
        "description":        frag.get("description"),
        "url_parfumo":        frag.get("url_parfumo"),
        "url_fragrantica":    frag.get("url_fragrantica"),
        "image_url":          frag.get("image_url"),
        "enriched_at":        _now(),
    }

    if existing is None:
        cols = ", ".join(fields.keys())
        ph   = ", ".join("?" for _ in fields)
        cur  = conn.execute(f"INSERT INTO fragrances ({cols}) VALUES ({ph})",
                            list(fields.values()))
        conn.commit()
        return cur.lastrowid
    else:
        # Update only with non-empty new values (preserve existing enrichment)
        updates, params = [], []
        for k, v in fields.items():
            if k == "slug":
                continue
            if v not in (None, "", "[]"):
                updates.append(f"{k} = ?")
                params.append(v)
        if updates:
            params.append(existing["id"])
            conn.execute(f"UPDATE fragrances SET {', '.join(updates)} WHERE id = ?", params)
            conn.commit()
        return existing["id"]


def insert_offer(conn: sqlite3.Connection, offer: dict, fragrance_id: int | None = None) -> int:
    """Insert a retail offer (size/variant). De-dupes on (retailer, url, size_ml)."""
    fields = {
        "fragrance_id":   fragrance_id,
        "input_brand":    offer.get("input_brand"),
        "input_name":     offer.get("input_name"),
        "retailer":       offer.get("retailer") or offer.get("source"),
        "variant_title":  offer.get("variant_title"),
        "size_ml":        _to_float(offer.get("size_ml") or offer.get("retrieved_size_ml")),
        "size_oz":        _to_float(offer.get("size_oz")),
        "concentration":  offer.get("concentration"),
        "original_price": _to_float(offer.get("original_price")),
        "sale_price":     _to_float(offer.get("sale_price")),
        "discount_pct":   _pct_to_float(offer.get("discount_pct")),
        "price_per_ml":   _to_float(offer.get("price_per_ml")),
        "in_stock":       1 if offer.get("in_stock", True) else 0,
        "product_url":    offer.get("product_url"),
        "image_url":      offer.get("image_url"),
        "scraped_at":     _now(),
    }
    cols = ", ".join(fields.keys())
    ph   = ", ".join("?" for _ in fields)
    try:
        cur = conn.execute(
            f"INSERT OR REPLACE INTO offers ({cols}) VALUES ({ph})",
            list(fields.values()))
        conn.commit()
        return cur.lastrowid
    except sqlite3.Error as e:
        print(f"  DB error inserting offer: {e}")
        return -1


def _to_float(v):
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace("$", "").replace(",", "").replace("ml", "").strip())
    except (ValueError, TypeError):
        return None


def _pct_to_float(v):
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace("%", "").strip())
    except (ValueError, TypeError):
        return None


# ─────────────────────────────────────────────────────────────────────────────
#  Queries (used by the GUI)
# ─────────────────────────────────────────────────────────────────────────────

def query_offers(conn: sqlite3.Connection, filters: dict) -> list:
    """
    Return joined fragrance+offer rows matching the filter dict.
    Supported filters: brands[], retailers[], genders[], min_ppm, max_ppm,
                       min_size, max_size, min_rating, notes[], search_text,
                       in_stock_only
    """
    where, params = [], []

    if filters.get("brands"):
        where.append("f.brand IN (%s)" % ",".join("?" * len(filters["brands"])))
        params += filters["brands"]
    if filters.get("retailers"):
        where.append("o.retailer IN (%s)" % ",".join("?" * len(filters["retailers"])))
        params += filters["retailers"]
    if filters.get("genders"):
        where.append("f.gender IN (%s)" % ",".join("?" * len(filters["genders"])))
        params += filters["genders"]
    if filters.get("min_ppm") is not None:
        where.append("o.price_per_ml >= ?"); params.append(filters["min_ppm"])
    if filters.get("max_ppm") is not None:
        where.append("o.price_per_ml <= ?"); params.append(filters["max_ppm"])
    if filters.get("min_size") is not None:
        where.append("o.size_ml >= ?"); params.append(filters["min_size"])
    if filters.get("max_size") is not None:
        where.append("o.size_ml <= ?"); params.append(filters["max_size"])
    if filters.get("min_rating") is not None:
        where.append("(COALESCE(f.rating_parfumo,0) >= ? OR COALESCE(f.rating_fragrantica,0) >= ?)")
        params += [filters["min_rating"], filters["min_rating"]]
    if filters.get("in_stock_only"):
        where.append("o.in_stock = 1")
    if filters.get("search_text"):
        s = f"%{filters['search_text']}%"
        where.append("(f.brand LIKE ? OR f.name LIKE ? OR o.variant_title LIKE ?)")
        params += [s, s, s]
    if filters.get("notes"):
        # match any of the requested notes within any note field
        note_clauses = []
        for n in filters["notes"]:
            like = f"%{n}%"
            note_clauses.append(
                "(f.top_notes LIKE ? OR f.middle_notes LIKE ? OR f.base_notes LIKE ? "
                "OR f.main_accords LIKE ?)")
            params += [like, like, like, like]
        where.append("(" + " OR ".join(note_clauses) + ")")

    sql = """
        SELECT
            f.brand, f.name, f.perfumer, f.year, f.gender, f.fragrance_family,
            f.top_notes, f.middle_notes, f.base_notes, f.main_accords,
            f.rating_parfumo, f.rating_fragrantica, f.longevity, f.sillage,
            o.retailer, o.variant_title, o.size_ml, o.concentration,
            o.original_price, o.sale_price, o.discount_pct, o.price_per_ml,
            o.in_stock, o.product_url, o.image_url, o.scraped_at
        FROM offers o
        LEFT JOIN fragrances f ON o.fragrance_id = f.id
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY o.price_per_ml IS NULL, o.price_per_ml ASC"

    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def distinct_values(conn: sqlite3.Connection, column: str, table: str = "fragrances") -> list:
    """Return sorted distinct non-null values for a column (for filter dropdowns)."""
    safe = {"brand", "gender", "fragrance_family", "perfumer"}
    safe_offer = {"retailer", "concentration"}
    if table == "fragrances" and column not in safe:
        return []
    if table == "offers" and column not in safe_offer:
        return []
    rows = conn.execute(
        f"SELECT DISTINCT {column} FROM {table} "
        f"WHERE {column} IS NOT NULL AND {column} != '' ORDER BY {column}"
    ).fetchall()
    return [r[0] for r in rows]


def all_notes(conn: sqlite3.Connection) -> list:
    """Collect every distinct note/accord across all fragrances (for the notes filter)."""
    notes = set()
    rows = conn.execute(
        "SELECT top_notes, middle_notes, base_notes, main_accords FROM fragrances"
    ).fetchall()
    for r in rows:
        for field in r:
            if not field:
                continue
            try:
                for n in json.loads(field):
                    notes.add(n.strip().title())
            except (json.JSONDecodeError, AttributeError):
                pass
    return sorted(notes)


def infer_missing_sizes(conn: sqlite3.Connection, fragrance_id: int) -> int:
    """
    For offers under `fragrance_id` that have a price but no size_ml,
    infer the size from the most common known size for that same fragrance.
    Returns the number of rows updated.
    """
    known = conn.execute(
        "SELECT size_ml FROM offers WHERE fragrance_id = ? AND size_ml IS NOT NULL "
        "GROUP BY size_ml ORDER BY COUNT(*) DESC LIMIT 1",
        (fragrance_id,)
    ).fetchone()
    if not known:
        return 0
    inferred_ml = known[0]
    cur = conn.execute(
        "UPDATE offers SET size_ml = ?, size_oz = ?, price_per_ml = "
        "  CASE WHEN sale_price IS NOT NULL THEN ROUND(sale_price / ?, 4) ELSE NULL END "
        "WHERE fragrance_id = ? AND size_ml IS NULL AND sale_price IS NOT NULL",
        (inferred_ml, round(inferred_ml / 29.5735, 2), inferred_ml, fragrance_id)
    )
    conn.commit()
    return cur.rowcount


def stats(conn: sqlite3.Connection) -> dict:
    return {
        "fragrances": conn.execute("SELECT COUNT(*) FROM fragrances").fetchone()[0],
        "offers":     conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0],
        "retailers":  conn.execute("SELECT COUNT(DISTINCT retailer) FROM offers").fetchone()[0],
        "brands":     conn.execute("SELECT COUNT(DISTINCT brand) FROM fragrances").fetchone()[0],
        "enriched":   conn.execute(
            "SELECT COUNT(*) FROM fragrances WHERE rating_parfumo IS NOT NULL "
            "OR rating_fragrantica IS NOT NULL").fetchone()[0],
    }


if __name__ == "__main__":
    init_db()
    print(f"Database initialized at {DB_PATH}")
    conn = connect()
    tbls = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    print(f"   Tables: {tbls}")
    print(f"   Stats: {stats(conn)}")
    conn.close()
