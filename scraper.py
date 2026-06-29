"""
scraper.py — "polite but implacable" fragrance scraper.

Pipeline per query:
  1. Retailer scrape  (Jomashop | FragranceNet | LuckyScent — chosen by --source)
     → every size / variant with price, $/ml, stock, URLs
  2. Enrichment       (Parfumo + Fragrantica)
     → notes, accords, perfumer, year, ratings, longevity, sillage
  3. Merge + store    → SQLite via database.py

"Polite":     real UA, randomized human-like delays, exponential backoff,
              honours a configurable per-domain minimum interval, one worker.
"Implacable": retries transient failures, multiple selector fallbacks,
              resumes from DB (skips already-scraped), never silently drops a row.

Bot detection: Jomashop (Forter) and Fragrantica (Cloudflare) require a visible
browser. HEADLESS defaults to False. Do not interact with the window while running.

Setup:
    pip install playwright requests
    playwright install chromium
"""

import asyncio
import os
import random
import re
import sys
import time
from datetime import datetime
from urllib.parse import quote_plus, urljoin, unquote

import database as db

try:
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout
except ImportError:
    sys.exit("Run:  pip install playwright requests && playwright install chromium")


# ─────────────────────────────────────────────────────────────────────────────
#  Politeness configuration
# ─────────────────────────────────────────────────────────────────────────────

HEADLESS          = False     # Forter / Cloudflare require visible browser
DEBUG             = False     # when True, dump raw HTML + a screenshot for each enrichment page
MIN_DELAY         = 2.5       # seconds between requests (base)
MAX_DELAY         = 5.0       # upper bound for random jitter
PER_DOMAIN_GAP    = 4.0       # minimum seconds between hits on the SAME domain
MAX_RETRIES       = 3         # transient failure retries
BACKOFF_BASE      = 4.0       # exponential backoff base (4, 16, 64s)
PAGE_TIMEOUT      = 45_000
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36")

_last_hit: dict[str, float] = {}     # domain → last request timestamp

STEALTH_JS = """
Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
window.chrome={runtime:{},loadTimes:()=>{},csi:()=>{},app:{}};
Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});
Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});
"""


# ─────────────────────────────────────────────────────────────────────────────
#  Shared helpers
# ─────────────────────────────────────────────────────────────────────────────

def _domain(url: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", url).split("/")[0]


async def _polite_wait(url: str):
    """Enforce per-domain gap + global jitter before a request."""
    dom = _domain(url)
    now = time.time()
    if dom in _last_hit:
        elapsed = now - _last_hit[dom]
        if elapsed < PER_DOMAIN_GAP:
            await asyncio.sleep(PER_DOMAIN_GAP - elapsed)
    await asyncio.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
    _last_hit[dom] = time.time()


async def goto(page, url: str, wait_ms: int = 3500, retries: int = MAX_RETRIES) -> bool:
    """Navigate politely with exponential-backoff retries. Returns True on success."""
    for attempt in range(retries):
        await _polite_wait(url)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
            await page.wait_for_timeout(wait_ms)
            # Detect hard blocks
            body = (await page.evaluate("() => document.body ? document.body.innerText : ''"))[:500].lower()
            if "access denied" in body or "are you a robot" in body or "captcha" in body:
                raise RuntimeError("bot-block page")
            return True
        except (PWTimeout, RuntimeError) as e:
            backoff = BACKOFF_BASE * (4 ** attempt)
            print(f"      retry {attempt+1}/{retries} after {backoff:.0f}s ({e}) -- {url[:60]}")
            await asyncio.sleep(backoff)
    print(f"      FAILED: gave up on {url[:70]}")
    return False


def clean_price(raw):
    if not raw:
        return None
    s = str(raw)
    for pat in (r"USD\s*([\d,]+\.?\d*)", r"\$\s*([\d,]+\.?\d*)", r"([\d,]+\.?\d*)\s*\$"):
        m = re.search(pat, s, re.I)
        if m:
            return float(m.group(1).replace(",", ""))
    return None


def size_to_ml(text: str):
    if not text:
        return None
    # Multi-piece sets: "2 x 75 ml", "75ml x 2", "3 x 1.7 oz" → total volume
    m = re.search(r"(\d+)\s*[xX]\s*(\d+(?:\.\d+)?)\s*ml", text, re.I)
    if m:
        return round(int(m.group(1)) * float(m.group(2)), 1)
    m = re.search(r"(\d+(?:\.\d+)?)\s*ml\s*[xX]\s*(\d+)", text, re.I)
    if m:
        return round(float(m.group(1)) * int(m.group(2)), 1)
    m = re.search(r"(\d+)\s*[xX]\s*(\d+(?:\.\d+)?)\s*(?:fl\.?\s*)?oz", text, re.I)
    if m:
        return round(int(m.group(1)) * float(m.group(2)) * 29.5735, 1)
    # Single sizes
    m = re.search(r"(\d+(?:\.\d+)?)\s*ml", text, re.I)
    if m:
        return float(m.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:fl\.?\s*)?oz", text, re.I)
    if m:
        return round(float(m.group(1)) * 29.5735, 1)
    return None


def ppm(price, ml):
    try:
        if price and ml and float(ml) > 0:
            return round(float(price) / float(ml), 4)
    except (ValueError, TypeError):
        pass
    return None


async def dismiss_popup(page):
    for sel in ["button[aria-label='Close']", ".modal-close",
                "[class*='close-modal']", "text=I don't want a discount"]:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.click()
                await page.wait_for_timeout(300)
                return
        except Exception:
            pass


DEBUG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug")


async def debug_dump(page, tag: str):
    """
    When DEBUG is on, save the current page's HTML, a screenshot, and a JSON
    summary of candidate selectors to ./debug/. This makes it trivial to update
    the Parfumo/Fragrantica selectors if those sites change their markup.
    """
    if not DEBUG:
        return
    os.makedirs(DEBUG_DIR, exist_ok=True)
    safe = re.sub(r"[^a-z0-9]+", "_", tag.lower()).strip("_")[:60]
    ts   = datetime.now().strftime("%H%M%S")
    base = os.path.join(DEBUG_DIR, f"{safe}_{ts}")

    # 1. Raw HTML
    try:
        html = await page.content()
        with open(base + ".html", "w", encoding="utf-8") as f:
            f.write(html)
    except Exception as e:
        print(f"      [debug] html dump failed: {e}")

    # 2. Screenshot (full page)
    try:
        await page.screenshot(path=base + ".png", full_page=True)
    except Exception as e:
        print(f"      [debug] screenshot failed: {e}")

    # 3. Selector probe — counts of useful candidate selectors, plus sample text.
    #    This tells you at a glance which selectors still match on the live page.
    try:
        probe = await page.evaluate(r"""
            () => {
                const probe = (sel) => {
                    const els = [...document.querySelectorAll(sel)];
                    return {
                        count: els.length,
                        sample: els.slice(0, 5).map(e =>
                            (e.innerText || e.getAttribute('alt') ||
                             e.getAttribute('content') || '').trim().slice(0, 60)
                        ).filter(Boolean),
                    };
                };
                const selectors = {
                    // Generic
                    'h1':                         'h1',
                    '[itemprop=ratingValue]':     '[itemprop="ratingValue"]',
                    '[itemprop=ratingCount]':     '[itemprop="ratingCount"]',
                    '[itemprop=image]':           '[itemprop="image"]',
                    // Parfumo
                    'a[href*=/Notes/]':           'a[href*="/Notes/"]',
                    'a[href*=/Perfumers/]':       'a[href*="/Perfumers/"]',
                    '.barfiller_text':            '.barfiller_text',
                    '.notes_list':                '.notes_list',
                    '.classification':            '[class*="classification"]',
                    // Fragrantica
                    '.accord-bar':                '.accord-bar',
                    'a[href*=/notes/]':           'a[href*="/notes/"]',
                    '#pyramid':                   '#pyramid',
                    '#mainpicbox img':            '#mainpicbox img',
                    '.notes-box':                 '.notes-box',
                };
                const out = {};
                for (const [label, sel] of Object.entries(selectors)) {
                    try { out[label] = probe(sel); } catch(e) { out[label] = {error: String(e)}; }
                }
                // JSON-LD blocks (often contain structured rating/brand)
                out['_jsonld'] = [...document.querySelectorAll('script[type="application/ld+json"]')]
                    .map(s => s.innerText.slice(0, 200));
                out['_title'] = document.title;
                out['_url']   = window.location.href;
                return out;
            }
        """)
        import json as _json
        with open(base + "_selectors.json", "w", encoding="utf-8") as f:
            _json.dump(probe, f, indent=2, ensure_ascii=False)
        # Console summary: which key selectors matched
        hits = {k: v.get("count") for k, v in probe.items()
                if isinstance(v, dict) and v.get("count")}
        print(f"      [debug] dumped {base}.html / .png / _selectors.json")
        print(f"      [debug] live selector hits: {hits}")
    except Exception as e:
        print(f"      [debug] selector probe failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
#  RETAILER SCRAPERS — return list of offer dicts (one per size/variant)
# ─────────────────────────────────────────────────────────────────────────────

# Jomashop search results → all matching product cards (each is a size variant)
JOMA_CARDS_JS = """
() => [...document.querySelectorAll('li.productItem')].map(c => ({
    brand: c.querySelector('span.brand-name')?.innerText?.trim()||'',
    name:  c.querySelector('span.name-out-brand')?.innerText?.trim()||'',
    href:  c.querySelector('a.productName-link')?.getAttribute('href')||'',
    img:   c.querySelector('a.productImg-link img')?.src||'',
    orig:  [...c.querySelectorAll('.was-price-wrapper span')]
             .find(s=>s.innerText.includes('$'))?.innerText.trim()||'',
    sale:  [...c.querySelectorAll('.now-price span')]
             .find(s=>s.innerText.includes('$'))?.innerText.trim()||
           [...c.querySelectorAll('.productPrice span')]
             .find(s=>s.innerText.includes('$'))?.innerText.trim()||''
})).filter(c => c.name)
"""


NON_FRAGRANCE = re.compile(
    r"\b(watch|watches|chronograph|timepiece|quartz|automatic"
    r"|sunglass|sunglasses|eyeglass|eyeglasses|spectacles|frames"
    r"|wallet|belt|handbag|purse|scarf|jewel|bracelet|necklace|ring)\b", re.I
)

def _is_non_fragrance(text: str) -> bool:
    """Return True if the product is clearly NOT a fragrance (watch, glasses, etc.)."""
    return bool(NON_FRAGRANCE.search(text or ""))


JOMA_PRODUCT_SIZE_JS = r"""
    () => {
        // 1. JSON-LD description (most reliable)
        const lds = [...document.querySelectorAll('script[type="application/ld+json"]')]
            .map(s => { try { return JSON.parse(s.innerText); } catch(e) { return null; } })
            .filter(Boolean);
        const desc = (lds.find(o => o.description || o.name) || {});
        const descText = (desc.description || '') + ' ' + (desc.name || '');

        // 2. Spec / detail tables (often have "Volume: 100 ml")
        const specText = [...document.querySelectorAll(
            'table td, [class*="spec"] td, [class*="detail"] td, [class*="attribute"]'
        )].map(e => e.innerText).join(' ');

        // 3. Full body text as last resort
        const body = document.body.innerText;

        const candidate = descText + ' ' + specText + ' ' + body;
        const patterns = [
            /\d+\s*[xX]\s*\d+(?:\.\d+)?\s*ml/i,
            /\d+(?:\.\d+)?\s*ml\s*[xX]\s*\d+/i,
            /\d+\s*[xX]\s*\d+(?:\.\d+)?\s*(?:fl\.?\s*)?oz/i,
            /\d+(?:\.\d+)?\s*ml/i,
            /\d+(?:\.\d+)?\s*(?:fl\.?\s*)?oz/i,
        ];
        for (const pat of patterns) {
            const m = candidate.match(pat);
            if (m) return m[0];
        }
        return '';
    }
"""

async def _jomashop_size_from_page(page, prod_url: str) -> float | None:
    """Visit the product page to extract size when the search card didn't have one."""
    if not await goto(page, prod_url, 3000):
        return None
    raw = await page.evaluate(JOMA_PRODUCT_SIZE_JS)
    return size_to_ml(raw) if raw else None


async def scrape_jomashop(page, brand, name) -> list:
    q = quote_plus(f"{brand} {name}".strip())
    # Use the fragrance category filter to avoid watches/sunglasses
    url = f"https://www.jomashop.com/search?q={q}&category=fragrance"
    if not await goto(page, url, 3500):
        return []
    await dismiss_popup(page)
    cards = await page.evaluate(JOMA_CARDS_JS)
    offers = []
    qwords = set(re.sub(r"[^a-z0-9 ]", " ", f"{brand} {name}".lower()).split())
    for c in cards:
        hay = f"{c['brand']} {c['name']}".lower()
        if qwords and sum(1 for w in qwords if w in hay) < max(1, len(qwords) // 2):
            continue
        if _is_non_fragrance(c["name"]):
            continue
        sale = clean_price(c.get("sale"))
        orig = clean_price(c.get("orig")) or sale
        ml   = size_to_ml(c["name"])
        href = c.get("href", "")
        prod_url = href if href.startswith("http") else urljoin("https://www.jomashop.com", href)
        # If size missing from card, visit the product page to find it
        if ml is None and prod_url:
            ml = await _jomashop_size_from_page(page, prod_url)
            # Navigate back to results isn't needed — we store and continue
        offers.append({
            "retailer": "Jomashop", "input_brand": brand, "input_name": name,
            "variant_title": c["name"], "size_ml": ml,
            "size_oz": round(ml / 29.5735, 2) if ml else None,
            "original_price": orig, "sale_price": sale,
            "discount_pct": round((orig - sale) / orig * 100) if (orig and sale and orig > sale) else None,
            "price_per_ml": ppm(sale, ml), "in_stock": bool(sale),
            "product_url": prod_url, "image_url": (c.get("img") or "").split("?")[0],
        })
    return offers


async def scrape_fragrancenet(page, brand, name) -> list:
    slug = re.sub(r"[^a-z0-9]+", "-", brand.lower()).strip("-")
    offers = []
    for prefix in ("/fragrances/", "/cologne/", "/perfume/"):
        if not await goto(page, f"https://www.fragrancenet.com{prefix}{slug}", 3500):
            continue
        ok = await page.evaluate(
            "() => !/(could not be found|EAU MY!|PAGE NOT FOUND)/i.test(document.body.innerText)")
        if not ok:
            continue
        links = await page.evaluate("""
            () => [...new Map([...document.querySelectorAll('a[href]')]
                .filter(a => /\\/(cologne|perfume)\\//.test(a.getAttribute('href')||'')
                          && (a.getAttribute('href')||'').split('/').length >= 5)
                .map(a => [a.getAttribute('href'),
                           {href:a.getAttribute('href'), text:(a.innerText||'').trim()}])).values()]
        """)
        nwords = set(re.sub(r"[^a-z0-9 ]", " ", name.lower()).split())
        for l in links:
            lt = l["text"].lower()
            if nwords and not any(w in lt for w in nwords if len(w) > 2):
                continue
            url = l["href"] if l["href"].startswith("http") else f"https://www.fragrancenet.com{l['href']}"
            url = url.split("#")[0]
            if not await goto(page, url, 3500):
                continue
            await dismiss_popup(page)
            d = await page.evaluate("""
                () => {
                    const h1 = document.querySelector('h1')?.innerText?.trim()||'';
                    const sizes = [...document.querySelectorAll('[class*="size"] option, select option, [class*="variant"]')]
                        .map(e => e.innerText?.trim()).filter(t => /\\d+\\s*(ml|oz)/i.test(t||''));
                    let sale = '';
                    for (const el of document.querySelectorAll('.price')) {
                        const t = el.innerText?.trim(), p = el.parentElement;
                        if (t && t.startsWith('$') && t !== '--'
                            && !p?.className?.includes('pwc') && !p?.className?.includes('coupon')) {
                            sale = t; break;
                        }
                    }
                    const img = document.querySelector('.mainImage img,#product-image img')?.src||'';
                    return {h1, sizes, sale, img, url: window.location.href};
                }
            """)
            sale = clean_price(d.get("sale"))
            if not sale:
                continue
            # One offer per detected size, else a single offer
            sizes = d.get("sizes") or [d.get("h1", "")]
            seen_ml = set()
            for s in sizes:
                ml = size_to_ml(s) or size_to_ml(d.get("h1", ""))
                if ml in seen_ml:
                    continue
                seen_ml.add(ml)
                offers.append({
                    "retailer": "FragranceNet", "input_brand": brand, "input_name": name,
                    "variant_title": d.get("h1") or l["text"], "size_ml": ml,
                    "size_oz": round(ml / 29.5735, 2) if ml else None,
                    "original_price": sale, "sale_price": sale, "discount_pct": None,
                    "price_per_ml": ppm(sale, ml), "in_stock": True,
                    "product_url": d.get("url", url), "image_url": (d.get("img") or "").split("?")[0],
                })
            break  # found the product; stop scanning prefixes
        if offers:
            break
    return offers


async def scrape_luckyscent(page, brand, name) -> list:
    words = brand.lower().split()
    slugs = [re.sub(r"[^a-z0-9]+", "-", brand.lower()).strip("-")]
    if len(words) > 1:
        slugs.append(words[0])
    offers = []
    for slug in slugs:
        if not await goto(page, f"https://www.luckyscent.com/brands/{slug}", 3500):
            continue
        if not await page.evaluate("() => !document.body.innerText.includes('can not be found')"):
            continue
        for _ in range(3):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(800)
        links = await page.evaluate("""
            () => [...new Map([...document.querySelectorAll('a[href*="/products/"]')]
                .map(a => [a.getAttribute('href'),
                     {href:a.getAttribute('href'), text:(a.innerText||'').replace(/\\n+/g,' ').trim()}])).values()]
                .filter(l => l.text && l.text.length > 2 && !/fitting|sample pack|gift card/i.test(l.text))
        """)
        nwords = set(re.sub(r"[^a-z0-9 ]", " ", name.lower()).split())
        for l in links:
            lt = l["text"].lower()
            if nwords and not any(w in lt for w in nwords if len(w) > 2):
                continue
            url = l["href"] if l["href"].startswith("http") else f"https://www.luckyscent.com{l['href']}"
            if not await goto(page, url, 3000):
                continue
            d = await page.evaluate("""
                () => {
                    const body = document.body.innerText;
                    const h1 = document.querySelector('h1')?.innerText?.trim()||'';
                    // collect every "$NNN ... Size: NNml" pairing
                    const variants = [];
                    const re = /\\$(\\d+(?:\\.\\d+)?)[\\s\\S]{0,40}?(\\d+(?:\\.\\d+)?)\\s*ml/gi;
                    let m; while ((m = re.exec(body)) !== null) {
                        variants.push({price: parseFloat(m[1]), ml: parseFloat(m[2])});
                    }
                    const img = document.querySelector('[class*="product__media"] img,[class*="product-image"] img')?.src||'';
                    return {h1, variants, img, url: window.location.href};
                }
            """)
            seen = set()
            for v in d.get("variants", []):
                key = (v["ml"], v["price"])
                if key in seen or v["price"] < 15:
                    continue
                seen.add(key)
                offers.append({
                    "retailer": "LuckyScent", "input_brand": brand, "input_name": name,
                    "variant_title": d.get("h1") or l["text"], "size_ml": v["ml"],
                    "size_oz": round(v["ml"] / 29.5735, 2) if v["ml"] else None,
                    "original_price": v["price"], "sale_price": v["price"], "discount_pct": None,
                    "price_per_ml": ppm(v["price"], v["ml"]), "in_stock": True,
                    "product_url": d.get("url", url), "image_url": (d.get("img") or "").split("?")[0],
                })
        if offers:
            break
    return offers


async def scrape_sephora(page, brand, name) -> list:
    """Search Sephora and scrape matching product pages."""
    q = quote_plus(f"{brand} {name}".strip())
    if not await goto(page, f"https://www.sephora.com/search?keyword={q}", 7000):
        return []
    await dismiss_popup(page)
    # Sephora is a heavy SPA — scroll to trigger lazy render
    for _ in range(2):
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(1500)
    links = await page.evaluate("""
        () => [...new Set([...document.querySelectorAll('a[href*="/product/"]')]
            .map(a => a.href).filter(h => h.includes('sephora.com/product/')))]
            .slice(0, 20)
    """)
    nwords = set(re.sub(r"[^a-z0-9 ]", " ", (brand + " " + name).lower()).split())
    offers = []
    for url in links:
        url = url.split("?")[0]
        if not await goto(page, url, 5000):
            continue
        await dismiss_popup(page)
        d = await page.evaluate(r"""
            () => {
                const out = {url: window.location.href};
                // JSON-LD is the most reliable source
                const lds = [...document.querySelectorAll('script[type="application/ld+json"]')]
                    .map(s => { try { return JSON.parse(s.innerText); } catch(e) { return null; } })
                    .filter(Boolean);
                const prod = lds.find(o => o['@type'] === 'Product') || {};
                out.name  = prod.name  || document.querySelector('h1')?.innerText?.trim() || '';
                out.brand = prod.brand?.name || '';
                out.image = (prod.image || [null])[0] || '';
                // Offers from JSON-LD
                const rawOffers = Array.isArray(prod.offers) ? prod.offers
                    : prod.offers ? [prod.offers] : [];
                out.jsonOffers = rawOffers.map(o => ({
                    price: parseFloat(o.price) || null,
                    size:  o.name || '',
                    avail: (o.availability || '').includes('InStock'),
                }));
                // Fallback: grab displayed price
                const priceEl = document.querySelector(
                    '[data-comp*="Price"] [class*="price"], [class*="css-0"] b, [class*="Price__value"]'
                );
                out.fallbackPrice = priceEl ? priceEl.innerText.trim() : '';
                // Size options from select/buttons
                out.sizeOptions = [...document.querySelectorAll(
                    '[data-comp*="Size"] button, [class*="swatch"] button, select option'
                )].map(e => (e.innerText || e.value || '').trim())
                 .filter(t => /\d+(\.\d+)?\s*(ml|oz)/i.test(t));
                return out;
            }
        """)
        title = d.get("name", "")
        hay = title.lower()
        if nwords and sum(1 for w in nwords if w in hay) < max(1, len(nwords) // 2):
            continue
        json_offers = d.get("jsonOffers") or []
        if json_offers:
            for jo in json_offers:
                price = jo.get("price")
                if not price:
                    continue
                ml = size_to_ml(jo.get("size", "") + " " + title)
                offers.append({
                    "retailer": "Sephora", "input_brand": brand, "input_name": name,
                    "variant_title": title, "size_ml": ml,
                    "size_oz": round(ml / 29.5735, 2) if ml else None,
                    "original_price": price, "sale_price": price, "discount_pct": None,
                    "price_per_ml": ppm(price, ml), "in_stock": jo.get("avail", True),
                    "product_url": d.get("url", url),
                    "image_url": (d.get("image") or "").split("?")[0],
                })
        else:
            price = clean_price(d.get("fallbackPrice"))
            if not price:
                continue
            ml = size_to_ml(title)
            # Try size options for additional sizes
            sizes = d.get("sizeOptions") or [title]
            seen_ml = set()
            for sz in sizes:
                ml = size_to_ml(sz) or size_to_ml(title)
                if ml in seen_ml:
                    continue
                seen_ml.add(ml)
                offers.append({
                    "retailer": "Sephora", "input_brand": brand, "input_name": name,
                    "variant_title": title, "size_ml": ml,
                    "size_oz": round(ml / 29.5735, 2) if ml else None,
                    "original_price": price, "sale_price": price, "discount_pct": None,
                    "price_per_ml": ppm(price, ml), "in_stock": True,
                    "product_url": d.get("url", url),
                    "image_url": (d.get("image") or "").split("?")[0],
                })
    return offers


async def scrape_ulta(page, brand, name) -> list:
    """Scrape Ulta brand page (fragrance category) for matching products."""
    slug = re.sub(r"[^a-z0-9]+", "-", brand.lower()).strip("-")
    if not await goto(page, f"https://www.ulta.com/brand/{slug}?category=fragrance", 5000):
        return []
    # Check for valid brand page
    body_check = await page.evaluate("() => document.body.innerText")
    if "page not found" in body_check.lower() or "no results" in body_check.lower():
        return []
    # Scroll to load lazy products
    for _ in range(4):
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(800)
    links = await page.evaluate("""
        () => [...new Set([...document.querySelectorAll('a[href*="/p/"]')]
            .map(a => a.href).filter(h => h.includes('ulta.com/p/')))]
    """)
    nwords = set(re.sub(r"[^a-z0-9 ]", " ", name.lower()).split())
    offers = []
    for url in links:
        base_url = url.split("?")[0]
        if not await goto(page, url, 5000):
            continue
        d = await page.evaluate(r"""
            () => {
                const out = {url: window.location.href};
                // JSON-LD product info (name contains size, e.g. "Eros EDP - 3.4 oz")
                const lds = [...document.querySelectorAll('script[type="application/ld+json"]')]
                    .map(s => { try { return JSON.parse(s.innerText); } catch(e) { return null; } })
                    .filter(Boolean);
                const prod = lds.find(o => o['@type'] === 'Product') || {};
                out.name  = prod.name  || document.querySelector('h1')?.innerText?.trim() || '';
                out.brand = prod.brand || '';
                out.image = prod.image || '';
                // Prices: filter to plausible fragrance prices ($15-$600)
                const allPrices = [...document.querySelectorAll('[class*="pal-c-Text"]')]
                    .map(e => (e.innerText||'').trim())
                    .filter(t => /^\$[\d,.]+$/.test(t))
                    .map(t => parseFloat(t.replace(/[$,]/g,'')))
                    .filter(p => p >= 15 && p <= 600);
                out.prices = [...new Set(allPrices)];
                // Size variant links (same product, different SKU)
                out.skuLinks = [...new Set([...document.querySelectorAll('a[href*="/p/"][href*="?sku="]')]
                    .map(a => a.href))].slice(0, 20);
                // In-stock check
                out.inStock = !/out of stock|sold out/i.test(document.body.innerText);
                return out;
            }
        """)
        title = d.get("name", "")
        hay = title.lower() + " " + (d.get("brand") or "").lower()
        if nwords and sum(1 for w in nwords if w in hay) < max(1, len(nwords) // 2):
            continue
        prices = d.get("prices") or []
        if not prices:
            continue
        sale = prices[0]  # first price = displayed/default price
        ml = size_to_ml(title)  # JSON-LD name includes size e.g. "... - 3.4 oz"
        img = (d.get("image") or "").split("?")[0]

        # Visit each size SKU to get per-size price
        sku_links = d.get("skuLinks") or []
        if sku_links:
            seen_sku = set()
            for sku_url in [url] + sku_links:
                sku_key = sku_url.split("sku=")[-1]
                if sku_key in seen_sku:
                    continue
                seen_sku.add(sku_key)
                if sku_url != url:
                    if not await goto(page, sku_url, 4000):
                        continue
                sku_d = await page.evaluate(r"""
                    () => {
                        const lds = [...document.querySelectorAll('script[type="application/ld+json"]')]
                            .map(s => { try { return JSON.parse(s.innerText); } catch(e) { return null; } })
                            .filter(Boolean);
                        const prod = lds.find(o => o['@type'] === 'Product') || {};
                        const allPrices = [...document.querySelectorAll('[class*="pal-c-Text"]')]
                            .map(e => (e.innerText||'').trim())
                            .filter(t => /^\$[\d,.]+$/.test(t))
                            .map(t => parseFloat(t.replace(/[$,]/g,'')))
                            .filter(p => p >= 15 && p <= 600);
                        return {
                            name: prod.name || '',
                            price: allPrices[0] || null,
                            inStock: !/out of stock|sold out/i.test(document.body.innerText),
                            url: window.location.href,
                        };
                    }
                """)
                sku_price = sku_d.get("price")
                sku_name  = sku_d.get("name") or title
                sku_ml    = size_to_ml(sku_name)
                if not sku_price:
                    continue
                offers.append({
                    "retailer": "Ulta", "input_brand": brand, "input_name": name,
                    "variant_title": sku_name, "size_ml": sku_ml,
                    "size_oz": round(sku_ml / 29.5735, 2) if sku_ml else None,
                    "original_price": sku_price, "sale_price": sku_price, "discount_pct": None,
                    "price_per_ml": ppm(sku_price, sku_ml),
                    "in_stock": sku_d.get("inStock", True),
                    "product_url": sku_d.get("url", sku_url), "image_url": img,
                })
        else:
            offers.append({
                "retailer": "Ulta", "input_brand": brand, "input_name": name,
                "variant_title": title, "size_ml": ml,
                "size_oz": round(ml / 29.5735, 2) if ml else None,
                "original_price": sale, "sale_price": sale, "discount_pct": None,
                "price_per_ml": ppm(sale, ml), "in_stock": d.get("inStock", True),
                "product_url": d.get("url", url), "image_url": img,
            })
    return offers


RETAILERS = {
    "jomashop":   scrape_jomashop,
    "luckyscent": scrape_luckyscent,
    "sephora":    scrape_sephora,
    "ulta":       scrape_ulta,
}


# ─────────────────────────────────────────────────────────────────────────────
#  ENRICHMENT — Parfumo + Fragrantica
# ─────────────────────────────────────────────────────────────────────────────

async def _ddg_first(page, query: str, domain: str) -> str | None:
    """Use DuckDuckGo to find the canonical product URL on a given domain."""
    if not await goto(page, f"https://duckduckgo.com/?q={quote_plus(query)}&t=h_&ia=web", 4000):
        return None
    hrefs = await page.evaluate("""
        () => [...document.querySelectorAll('a[href]')].map(a => a.getAttribute('href')||'')
    """)
    for h in hrefs:
        real = h
        if "uddg=" in h:
            m = re.search(r"uddg=([^&]+)", h)
            if m:
                real = unquote(m.group(1))
        if domain in real and real.startswith("http"):
            return real.split("?")[0]
    return None


async def enrich_parfumo(page, brand, name) -> dict:
    """Scrape Parfumo characteristics. Best-effort — returns {} on failure."""
    url = await _ddg_first(page, f"{brand} {name} parfumo", "parfumo.com")
    if not url or "/Perfumes/" not in url:
        return {}
    if not await goto(page, url, 3500):
        return {}
    await debug_dump(page, f"parfumo_{brand}_{name}")
    data = await page.evaluate(r"""
        () => {
            const out  = {url: window.location.href};
            const body = document.body.innerText;

            // ── Rating: real page shows "7.8 / 10" then "335 Ratings" as text ──
            // Try structured data first, then text pattern.
            let rating = document.querySelector('[itemprop="ratingValue"]')?.innerText
                      || document.querySelector('[itemprop="ratingValue"]')?.getAttribute('content');
            if (!rating) {
                const m = body.match(/(\d(?:[.,]\d)?)\s*\/\s*10/);
                if (m) rating = m[1];
            }
            if (rating) out.rating = parseFloat(String(rating).replace(',', '.'));

            let votes = document.querySelector('[itemprop="ratingCount"]')?.innerText;
            if (!votes) {
                const m = body.match(/([\d,]+)\s*Ratings/i);
                if (m) votes = m[1];
            }
            if (votes) out.votes = parseInt(String(votes).replace(/\D/g,''));

            // ── Year: prefer the /Release_Years/ link, else any 19xx/20xx ──
            const yLink = document.querySelector('a[href*="/Release_Years/"]')?.innerText;
            const ymatch = (yLink || body).match(/\b(19|20)\d{2}\b/);
            if (ymatch) out.year = parseInt(ymatch[0]);

            // ── Gender from description text ──
            if (/for women and men|unisex/i.test(body)) out.gender = 'unisex';
            else if (/\bfor women\b/i.test(body)) out.gender = 'women';
            else if (/\bfor men\b/i.test(body)) out.gender = 'men';

            // ── Longevity / sillage from the descriptive sentence ──
            const lon = body.match(/longevity is ([\w-]+)/i);
            if (lon) out.longevity = lon[1];
            const sil = body.match(/sillage is ([\w-]+)/i);
            if (sil) out.sillage = sil[1];

            // ── Family / scent descriptor ("The scent is resinous-smoky") ──
            const fam = body.match(/scent is ([\w\s-]+?)\./i);
            if (fam) out.family = fam[1].trim();

            // ── Perfumer ──
            const perf = [...document.querySelectorAll('a[href*="/Perfumers/"]')]
                .map(a => a.innerText.trim()).filter(Boolean);
            if (perf.length) out.perfumer = [...new Set(perf)].join(', ');

            // ── Notes: links to /Notes/ ; try to split by pyramid headings ──
            const noteEls = [...document.querySelectorAll('a[href*="/Notes/"]')];
            const allNotes = [...new Set(noteEls.map(a => a.innerText.trim()).filter(Boolean))];
            out.notes_all = allNotes;
            // Attempt pyramid split: Parfumo wraps groups in elements whose text
            // starts with Top/Heart/Base. Walk note links and bucket by nearest heading.
            const buckets = {top: [], heart: [], base: []};
            for (const a of noteEls) {
                let node = a, label = '';
                for (let i = 0; i < 6 && node; i++) {
                    const t = (node.previousElementSibling?.innerText || '').toLowerCase();
                    if (t.includes('top'))   { label = 'top'; break; }
                    if (t.includes('heart') || t.includes('middle')) { label = 'heart'; break; }
                    if (t.includes('base'))  { label = 'base'; break; }
                    node = node.parentElement;
                }
                const nm = a.innerText.trim();
                if (label && nm && !buckets[label].includes(nm)) buckets[label].push(nm);
            }
            out.buckets = buckets;

            // ── Image ──
            const img = document.querySelector(
                '[itemprop="image"], img[src*="media.parfumo"], .p_image img')?.src;
            if (img) out.image = img;

            return out;
        }
    """)
    result = {
        "url_parfumo":    data.get("url"),
        "rating_parfumo": data.get("rating"),
        "votes_parfumo":  data.get("votes"),
        "year":           data.get("year"),
        "gender":         data.get("gender"),
        "longevity":      data.get("longevity"),
        "sillage":        data.get("sillage"),
        "perfumer":       data.get("perfumer"),
        "fragrance_family": data.get("family"),
        "image_url":      data.get("image"),
    }
    # Use pyramid buckets when available; else dump full set into middle_notes.
    buckets = data.get("buckets") or {}
    if buckets.get("top"):   result["top_notes"]    = buckets["top"]
    if buckets.get("heart"): result["middle_notes"] = buckets["heart"]
    if buckets.get("base"):  result["base_notes"]   = buckets["base"]
    if not any(buckets.get(k) for k in ("top", "heart", "base")) and data.get("notes_all"):
        result["middle_notes"] = data["notes_all"]
    return {k: v for k, v in result.items() if v not in (None, "", [])}


async def enrich_fragrantica(page, brand, name) -> dict:
    """Scrape Fragrantica characteristics. Cloudflare-protected — needs visible browser."""
    url = await _ddg_first(page, f"{brand} {name} fragrantica perfume", "fragrantica.com")
    if not url or "/perfume/" not in url:
        return {}
    if not await goto(page, url, 4000):
        return {}
    await debug_dump(page, f"fragrantica_{brand}_{name}")
    data = await page.evaluate(r"""
        () => {
            const out = {url: window.location.href};
            // Accords
            out.accords = [...document.querySelectorAll('.accord-bar')].map(e => e.innerText.trim()).filter(Boolean);
            // Notes pyramid — Fragrantica uses pyramid_level blocks
            const noteImgs = sel => [...document.querySelectorAll(sel)]
                .map(e => e.getAttribute('alt') || e.innerText?.trim()).filter(Boolean);
            // Sections labelled "Top Notes", "Middle Notes", "Base Notes"
            const pyramid = {};
            const blocks = [...document.querySelectorAll('#pyramid h4, #pyramid b, .notes-box')];
            // Fallback: all notes
            const allNotes = [...new Set([...document.querySelectorAll('a[href*="/notes/"] , [data-link*="/notes/"]')]
                .map(a => a.innerText.trim()).filter(Boolean))];
            out.notes_all = allNotes;
            // Rating
            const r = document.querySelector('[itemprop="ratingValue"]')?.innerText
                   || document.querySelector('.rating')?.getAttribute('content');
            if (r) out.rating = parseFloat(r);
            const v = document.querySelector('[itemprop="ratingCount"]')?.innerText;
            if (v) out.votes = parseInt(v.replace(/\D/g,''));
            // Year + gender from title/description
            const ttl = document.querySelector('h1')?.innerText || '';
            out.title = ttl;
            // Prefer the explicit launch phrase; only fall back to the title's
            // trailing year (Fragrantica h1 ends '... for women and men 2006').
            let ymatch = document.body.innerText.match(/launched in (\d{4})/i);
            if (!ymatch) ymatch = ttl.match(/(\d{4})\s*$/);
            if (ymatch) out.year = parseInt(ymatch[1]);
            if (/for women and men|unisex/i.test(ttl)) out.gender = 'unisex';
            else if (/for women/i.test(ttl)) out.gender = 'women';
            else if (/for men/i.test(ttl)) out.gender = 'men';
            const img = document.querySelector('[itemprop="image"], #mainpicbox img')?.src;
            if (img) out.image = img;
            return out;
        }
    """)
    result = {
        "url_fragrantica":    data.get("url"),
        "rating_fragrantica": data.get("rating"),
        "votes_fragrantica":  data.get("votes"),
        "year":               data.get("year"),
        "gender":             data.get("gender"),
        "main_accords":       data.get("accords"),
        "image_url":          data.get("image"),
    }
    if data.get("notes_all"):
        result["top_notes"] = data["notes_all"]   # store full set; pyramid split varies
    return {k: v for k, v in result.items() if v not in (None, "", [])}


# ─────────────────────────────────────────────────────────────────────────────
#  Browser factory
# ─────────────────────────────────────────────────────────────────────────────

async def make_page(pw):
    browser = await pw.chromium.launch(
        headless=HEADLESS,
        args=["--disable-blink-features=AutomationControlled",
              "--no-sandbox", "--disable-setuid-sandbox"],
    )
    ctx = await browser.new_context(
        viewport={"width": 1440, "height": 900},
        user_agent=USER_AGENT, locale="en-US", timezone_id="America/Chicago",
    )
    await ctx.add_init_script(STEALTH_JS)
    page = await ctx.new_page()
    return browser, page


# ─────────────────────────────────────────────────────────────────────────────
#  Orchestration
# ─────────────────────────────────────────────────────────────────────────────

async def scrape_one(page, conn, source: str, brand: str, name: str,
                     enrich: bool = True) -> int:
    """Scrape one fragrance from `source`, enrich, store. Returns offers stored."""
    print(f"\n  -- {brand} {name}  [{source}] --")

    # 1. Retailer offers (all sizes/variants)
    retail_fn = RETAILERS[source]
    try:
        offers = await retail_fn(page, brand, name)
    except Exception as e:
        print(f"    retailer error: {e}")
        offers = []
    print(f"    {len(offers)} offer(s) from {source}")

    # 2. Enrichment — Parfumo first, then Fragrantica fills only missing fields
    #    (both can return 'year'/'gender'/'image_url'; first-found wins).
    frag = {"brand": brand, "name": name}
    if enrich:
        try:
            pf = await enrich_parfumo(page, brand, name)
            if pf: print(f"    Parfumo: rating={pf.get('rating_parfumo')} "
                         f"notes={len(pf.get('middle_notes',[]))}")
            frag.update(pf)
        except Exception as e:
            print(f"    Parfumo error: {e}")
        try:
            fr = await enrich_fragrantica(page, brand, name)
            if fr: print(f"    Fragrantica: rating={fr.get('rating_fragrantica')} "
                         f"accords={len(fr.get('main_accords',[]))}")
            for k, v in fr.items():
                if frag.get(k) in (None, "", []):   # don't clobber Parfumo values
                    frag[k] = v
        except Exception as e:
            print(f"    Fragrantica error: {e}")

    # 3. Store
    fid = db.upsert_fragrance(conn, frag)
    stored = 0
    for off in offers:
        if db.insert_offer(conn, off, fid) > 0:
            stored += 1

    # 4. Back-fill size_ml for offers that still have none, using the most
    #    common known size for this fragrance as a best-guess estimate.
    inferred = db.infer_missing_sizes(conn, fid)
    if inferred:
        print(f"    inferred size for {inferred} offer(s) from sibling offers")

    print(f"    stored {stored} offers under fragrance_id={fid}")
    return stored


async def run(source: str, queries: list, enrich: bool = True):
    """queries: list of (brand, name) tuples. name may be '' for brand-only."""
    sources = list(RETAILERS) if source == "all" else [source]
    db.init_db()
    conn = db.connect()
    total = 0
    async with async_playwright() as pw:
        browser, page = await make_page(pw)
        try:
            for brand, name in queries:
                for src in sources:
                    total += await scrape_one(page, conn, src, brand, name, enrich)
        finally:
            await browser.close()
    conn.close()
    print(f"\nDone. {total} offers stored. Open the GUI with:  streamlit run app.py")


# ─────────────────────────────────────────────────────────────────────────────
#  CLI
# ─────────────────────────────────────────────────────────────────────────────

def _parse_args(argv):
    import argparse
    p = argparse.ArgumentParser(description="Polite-but-implacable fragrance scraper")
    p.add_argument("--source", choices=list(RETAILERS) + ["all"], default="luckyscent",
                   help="retailer to scrape offers from ('all' hits every site)")
    p.add_argument("--brand", help="house / brand name")
    p.add_argument("--name", default="", help="fragrance name (optional)")
    p.add_argument("--no-enrich", action="store_true",
                   help="skip Parfumo/Fragrantica enrichment")
    p.add_argument("--file", help="text file: one 'Brand | Fragrance' per line")
    p.add_argument("--headful", action="store_true", help="force visible browser (default)")
    p.add_argument("--headless", action="store_true", help="force headless (will be blocked)")
    p.add_argument("--debug", action="store_true",
                   help="dump raw HTML + screenshot + selector probe for each "
                        "Parfumo/Fragrantica page into ./debug/ (for fixing selectors)")
    return p.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv or sys.argv[1:])
    global HEADLESS, DEBUG
    if args.headless:
        HEADLESS = True
    if args.debug:
        DEBUG = True
        print("  DEBUG mode -- raw HTML, screenshots, and selector probes "
              "will be saved to ./debug/")
    queries = []
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "|" in line:
                    b, n = line.split("|", 1)
                    queries.append((b.strip(), n.strip()))
                else:
                    queries.append((line, ""))
    elif args.brand:
        queries.append((args.brand, args.name))
    else:
        print("Provide --brand 'Profumum Roma' [--name 'Olibanum'] or --file list.txt")
        return
    asyncio.run(run(args.source, queries, enrich=not args.no_enrich))


if __name__ == "__main__":
    main()
