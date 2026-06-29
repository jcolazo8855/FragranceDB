"""
app.py — Streamlit GUI for browsing & downloading scraped fragrance data.

Run:
    streamlit run app.py

Features
  • Filter by brand, retailer, gender, notes/accords, price-per-ml, size, rating
  • Live result table sorted by best value ($/ml)
  • Download the filtered set as CSV or Excel
  • Run new scrapes directly from the sidebar
"""

import json
import io
import os
import subprocess
import sys
import pandas as pd
import streamlit as st

import database as db

APP_DIR = os.path.dirname(os.path.abspath(__file__))

st.set_page_config(page_title="Fragrance Vault", page_icon="🧴", layout="wide")

# ── Light custom styling ─────────────────────────────────────────────────────
st.markdown("""
<style>
    .stApp { background: #faf8f5; }
    h1, h2, h3 { color: #2c2420; font-family: 'Georgia', serif; }
    .metric-card {
        background: linear-gradient(135deg, #f4ede4 0%, #e8dcc8 100%);
        border-radius: 12px; padding: 16px; text-align: center;
        border: 1px solid #d9c9b0;
    }
    .stDataFrame { border-radius: 8px; }
    [data-testid="stSidebar"] { background: #f1e9dd; }
</style>
""", unsafe_allow_html=True)


@st.cache_resource
def get_conn():
    db.init_db()
    return db.connect()


conn = get_conn()


# ─────────────────────────────────────────────────────────────────────────────
#  Header + stats
# ─────────────────────────────────────────────────────────────────────────────

st.title("🧴 Fragrance Vault")
st.caption("Scraped retail offers from Jomashop · FragranceNet · LuckyScent, "
           "enriched with characteristics from Parfumo & Fragrantica.")

s = db.stats(conn)
c1, c2, c3, c4, c5 = st.columns(5)
for col, label, val in [
    (c1, "Fragrances", s["fragrances"]), (c2, "Offers", s["offers"]),
    (c3, "Brands", s["brands"]), (c4, "Retailers", s["retailers"]),
    (c5, "Enriched", s["enriched"]),
]:
    col.markdown(f"<div class='metric-card'><h2 style='margin:0'>{val}</h2>"
                 f"<span style='color:#7a6a55'>{label}</span></div>",
                 unsafe_allow_html=True)

st.divider()


# ─────────────────────────────────────────────────────────────────────────────
#  Sidebar — scrape controls + filters
# ─────────────────────────────────────────────────────────────────────────────

with st.sidebar:
    st.header("➕ Scrape new data")
    src   = st.selectbox("Retailer source", ["all", "luckyscent", "jomashop", "sephora", "ulta"])
    sb    = st.text_input("House / Brand", placeholder="Profumum Roma")
    sn    = st.text_input("Fragrance (optional)", placeholder="Olibanum")
    enr   = st.checkbox("Enrich (Parfumo + Fragrantica)", value=True)
    if st.button("Run scrape", type="primary", use_container_width=True):
        if not sb.strip():
            st.error("Enter at least a brand.")
        else:
            cmd = [sys.executable, os.path.join(APP_DIR, "scraper.py"),
                   "--source", src, "--brand", sb.strip()]
            if sn.strip():
                cmd += ["--name", sn.strip()]
            if not enr:
                cmd += ["--no-enrich"]
            st.code(" ".join(cmd))
            try:
                with st.spinner("Scraping... a Chrome window will open. Don't click inside it."):
                    result = subprocess.run(
                        cmd, cwd=APP_DIR, capture_output=True, text=True,
                        timeout=600, encoding="utf-8", errors="replace",
                    )
                if result.returncode == 0:
                    st.success("Scrape finished. Results are shown below.")
                    if result.stdout:
                        st.text(result.stdout[-3000:])
                    st.cache_resource.clear()
                    st.rerun()
                else:
                    st.error("Scraper exited with an error.")
                    if result.stderr:
                        st.code(result.stderr[-3000:])
                    if result.stdout:
                        st.text(result.stdout[-3000:])
            except subprocess.TimeoutExpired:
                st.error("Scrape timed out after 10 minutes.")
            except Exception as e:
                st.error(f"Could not launch scraper: {e}")

    st.divider()
    st.header("🔎 Filters")

    brands    = st.multiselect("Brand", db.distinct_values(conn, "brand"))
    retailers = st.multiselect("Retailer", db.distinct_values(conn, "retailer", "offers"))
    genders   = st.multiselect("Gender", db.distinct_values(conn, "gender"))
    notes_all = db.all_notes(conn)
    notes     = st.multiselect("Notes / accords", notes_all,
                               help="Matches any selected note across top/heart/base/accords")

    st.subheader("Price per ml ($)")
    ppm_lo, ppm_hi = st.slider("ppm range", 0.0, 50.0, (0.0, 50.0), 0.5,
                               label_visibility="collapsed")
    st.subheader("Size (ml)")
    sz_lo, sz_hi = st.slider("size range", 0, 500, (0, 500), 5,
                             label_visibility="collapsed")
    min_rating = st.slider("Min rating (Parfumo or Fragrantica)", 0.0, 10.0, 0.0, 0.5)
    stock_only = st.checkbox("In stock only", value=False)
    search     = st.text_input("Search text", placeholder="name / brand / variant")


# ─────────────────────────────────────────────────────────────────────────────
#  Query + display
# ─────────────────────────────────────────────────────────────────────────────

filters = {
    "brands": brands or None,
    "retailers": retailers or None,
    "genders": genders or None,
    "notes": notes or None,
    "min_ppm": ppm_lo if ppm_lo > 0 else None,
    "max_ppm": ppm_hi if ppm_hi < 50 else None,
    "min_size": sz_lo if sz_lo > 0 else None,
    "max_size": sz_hi if sz_hi < 500 else None,
    "min_rating": min_rating if min_rating > 0 else None,
    "in_stock_only": stock_only,
    "search_text": search.strip() or None,
}

rows = db.query_offers(conn, filters)

if not rows:
    st.info("No results yet. Use the sidebar to scrape a brand, then refresh.")
    st.stop()

df = pd.DataFrame(rows)

# Prettify JSON note columns into comma strings for display
def _fmt_notes(val):
    if not val:
        return ""
    try:
        return ", ".join(json.loads(val))
    except (json.JSONDecodeError, TypeError):
        return str(val)

for col in ("top_notes", "middle_notes", "base_notes", "main_accords"):
    if col in df.columns:
        df[col] = df[col].apply(_fmt_notes)

# Column order for display
display_cols = [c for c in [
    "brand", "name", "variant_title", "size_ml", "concentration",
    "sale_price", "original_price", "discount_pct", "price_per_ml",
    "retailer", "in_stock", "rating_parfumo", "rating_fragrantica",
    "main_accords", "top_notes", "middle_notes", "base_notes",
    "perfumer", "year", "gender", "longevity", "sillage", "product_url",
] if c in df.columns]

st.subheader(f"📋 {len(df)} offers")

# Mark the best $/ml row for each unique fragrance (brand + name)
if "price_per_ml" in df.columns:
    best_idx = (
        df[df["price_per_ml"].notna()]
        .groupby(["brand", "name"])["price_per_ml"]
        .idxmin()
    )
    best_set = set(best_idx.values)
else:
    best_set = set()

def _highlight_best(row):
    if row.name in best_set and pd.notna(row.get("price_per_ml")):
        return ["background-color: #c6f0c2; font-weight: bold"
                if col == "price_per_ml" else "" for col in row.index]
    return [""] * len(row.index)

styled = df[display_cols].style.apply(_highlight_best, axis=1)

st.dataframe(
    styled,
    use_container_width=True, hide_index=True, height=560,
    column_config={
        "price_per_ml": st.column_config.NumberColumn("$/ml", format="$%.2f"),
        "sale_price":   st.column_config.NumberColumn("Sale", format="$%.2f"),
        "original_price": st.column_config.NumberColumn("MSRP", format="$%.2f"),
        "size_ml":      st.column_config.NumberColumn("Size", format="%g ml"),
        "rating_parfumo":     st.column_config.NumberColumn("Parfumo", format="%.1f"),
        "rating_fragrantica": st.column_config.NumberColumn("Fragrantica", format="%.1f"),
        "product_url":  st.column_config.LinkColumn("Link", display_text="open"),
        "in_stock":     st.column_config.CheckboxColumn("Stock"),
    },
)

# ── Downloads ────────────────────────────────────────────────────────────────
d1, d2, _ = st.columns([1, 1, 4])
csv_bytes = df[display_cols].to_csv(index=False).encode("utf-8")
d1.download_button("⬇️ CSV", csv_bytes, "fragrances_filtered.csv",
                   "text/csv", use_container_width=True)

xbuf = io.BytesIO()
with pd.ExcelWriter(xbuf, engine="openpyxl") as writer:
    df[display_cols].to_excel(writer, index=False, sheet_name="Fragrances")
d2.download_button("⬇️ Excel", xbuf.getvalue(), "fragrances_filtered.xlsx",
                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                   use_container_width=True)

# ── Quick insight ────────────────────────────────────────────────────────────
if "price_per_ml" in df and df["price_per_ml"].notna().any():
    best = df.loc[df["price_per_ml"].idxmin()]
    st.success(f"🏆 Best value: **{best['brand']} {best['name']}** — "
               f"${best['price_per_ml']:.2f}/ml "
               f"({best.get('size_ml','?')}ml @ ${best.get('sale_price','?')}, {best['retailer']})")
