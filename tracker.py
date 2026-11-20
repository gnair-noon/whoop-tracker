#!/usr/bin/env python3
"""WHOOP price tracker v2: real headless Chrome (Playwright), USD normalisation,
drop alerts (email + WhatsApp), dashboard data in docs/prices.json.

  python tracker.py             # full run
  python tracker.py --no-alerts # read prices + write dashboard data, send nothing
"""
import csv, datetime as dt, html as htmllib, json, os, random, re, smtplib, sys
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlparse
import requests

BASE = Path(__file__).parent
CFG = json.loads((BASE / "config.json").read_text())
STATE_FILE, HIST_FILE, OUT_FILE = BASE / "state.json", BASE / "history.csv", BASE / "docs" / "prices.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
ALERT_ON_RISE = os.getenv("ALERT_ON_RISE") == "1"
FAIL_ALERT_AT = 6                       # alert once after N consecutive failed reads
OOS_WORDS = ("out of stock", "currently unavailable", "sold out", "temporarily out of stock")
BLOCK_WORDS = ("captcha", "robot check", "access denied", "verify you are human", "unusual traffic")

# CSS selectors tried in order against the RENDERED page (the price a shopper sees, incl. deal prices).
SELECTORS = {
    "amazon": ["#corePriceDisplay_desktop_feature_div .priceToPay .a-offscreen",
               "#corePrice_feature_div .a-price .a-offscreen", "#apex_desktop .a-offscreen",
               ".a-price .a-offscreen"],
    "noon": ['[data-qa="div-price-now"]', '[data-qa="div-price"]', '[class*="priceNow"]'],
    "walmart": ['[itemprop="price"]', '[data-testid="price-wrap"] span', 'span[data-automation="buybox-price"]'],
    "generic": ['[itemprop="price"]', '[data-automation="product-price"]', '[class*="productPricing"] [class*="price"]'],
}


# ------------------------------------------------------------------ helpers
def num_from(text):
    m = re.search(r"(\d[\d,]*(?:\.\d{1,2})?)", (text or "").replace("\u00a0", " "))
    return float(m.group(1).replace(",", "")) if m else None


def _walk(o):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)


def parse_structured(html):
    """Fallback: JSON-LD / inline offers / Walmart __NEXT_DATA__. May be the BASE price, not the sale price."""
    for blob in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S):
        try:
            data = json.loads(htmllib.unescape(blob))
        except Exception:
            continue
        for node in _walk(data):
            offers = node.get("offers")
            for off in (offers if isinstance(offers, list) else [offers] if offers else []):
                if isinstance(off, dict):
                    p = off.get("price") or off.get("lowPrice")
                    if p and num_from(str(p)):
                        return num_from(str(p)), off.get("priceCurrency")
    m = re.search(r'"offers":\{"price":"?([\d.,]+)"?,"priceCurrency":"([A-Z]{3})"', html)
    if m:
        return num_from(m.group(1)), m.group(2)
    m = re.search(r'"currentPrice":\{"price":([\d.]+)', html)
    if m:
        return float(m.group(1)), None
    return None, None


# ------------------------------------------------------------------ FX
def get_rates(state):
    try:
        rates = requests.get("https://open.er-api.com/v6/latest/USD", timeout=15).json()["rates"]
        state["rates"] = {k: rates[k] for k in ("AED", "CAD", "GBP", "USD")}
    except Exception as e:
        print(f"[fx] live rate failed ({e}); using last known")
        state.setdefault("rates", {"AED": 3.6725, "CAD": 1.41, "GBP": 0.76, "USD": 1.0})
    state["rates"]["AED"] = 3.6725            # pegged
    return state["rates"]


# ------------------------------------------------------------------ reading prices
class Browser:
    def __init__(self):
        from playwright.sync_api import sync_playwright
        self.pw = sync_playwright().start()
        opts = {"headless": True, "args": ["--disable-blink-features=AutomationControlled"]}
        proxy = os.getenv("PROXY_URL")        # optional residential proxy: http://user:pass@host:port
        if proxy:
            u = urlparse(proxy)
            opts["proxy"] = {"server": f"{u.scheme}://{u.hostname}:{u.port}",
                             "username": u.username or "", "password": u.password or ""}
        self.browser = self.pw.chromium.launch(**opts)

    def read(self, url, extractor):
        ctx = self.browser.new_context(user_agent=UA, locale="en-US", viewport={"width": 1366, "height": 900})
        page = ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(random.randint(2500, 4500))
            html = page.content()
            low = html.lower()
            if any(w in low[:200000] for w in BLOCK_WORDS) and "a-price" not in low:
                raise RuntimeError("blocked (bot check)")
            for sel in SELECTORS.get(extractor, SELECTORS["generic"]):
                el = page.query_selector(sel)
                if el:
                    n = num_from(el.get_attribute("content") or el.inner_text())
                    if n and n > 0:
                        return {"local": n, "src": "page", "instock": True}
            n, cur = parse_structured(html)
            body = page.inner_text("body").lower() if page.query_selector("body") else ""
            if n and not any(w in body for w in OOS_WORDS):
                return {"local": n, "src": "base", "instock": True, "cur": cur}
            if any(w in body for w in OOS_WORDS):
                return {"local": None, "src": "page", "instock": False}
            raise RuntimeError("no price found (layout changed?)")
        finally:
            ctx.close()

    def close(self):
        self.browser.close(); self.pw.stop()


def read_api_fallback(url):
    key = os.getenv("SCRAPER_API_KEY")
    if not key:
        raise RuntimeError("no fallback configured")
    r = requests.get("https://api.scraperapi.com/", timeout=120,
                     params={"api_key": key, "url": url, "render": "true"})
    r.raise_for_status()
    n, cur = parse_structured(r.text)
    if not n:
        raise RuntimeError("fallback found no price")
    return {"local": n, "src": "base", "instock": True, "cur": cur}


def read_bestbuy_api(sku):
    r = requests.get(f"https://api.bestbuy.com/v1/products/{sku}.json", timeout=20,
                     params={"apiKey": os.environ["BESTBUY_API_KEY"],
                             "show": "salePrice,regularPrice,onlineAvailability"})
    r.raise_for_status()
    j = r.json()
    return {"local": float(j["salePrice"]), "src": "page", "instock": bool(j.get("onlineAvailability", True))}


def get_reading(br, tkey, target, sku):
    ref = target["urls"].get(sku, "")
    if not ref or ref.startswith("PASTE_"):
        raise RuntimeError("URL not configured")
    if target["extractor"] == "bestbuy_api":
        return read_bestbuy_api(ref)
    try:
        return br.read(ref, target["extractor"])
    except Exception as e:
        if os.getenv("SCRAPER_API_KEY"):
            print(f"  browser failed ({e}); trying API fallback")
            return read_api_fallback(ref)
        raise


# ------------------------------------------------------------------ alerts
def send_email(subject, body):
    to, user, pw = os.getenv("ALERT_EMAIL_TO"), os.getenv("SMTP_USER"), os.getenv("SMTP_PASS")
    if not (to and user and pw):
        print("[email] not configured"); return
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.set_content(body)
    with smtplib.SMTP_SSL(os.getenv("SMTP_HOST", "smtp.gmail.com"), int(os.getenv("SMTP_PORT", "465"))) as s:
        s.login(user, pw); s.send_message(msg)


def send_whatsapp(text):
    to = os.getenv("WHATSAPP_TO")
    if not to:
        print("[whatsapp] not configured"); return
    if os.getenv("TWILIO_SID"):
        requests.post(f"https://api.twilio.com/2010-04-01/Accounts/{os.environ['TWILIO_SID']}/Messages.json",
                      auth=(os.environ["TWILIO_SID"], os.environ["TWILIO_TOKEN"]), timeout=20,
                      data={"From": os.environ["TWILIO_WHATSAPP_FROM"], "To": f"whatsapp:{to}",
                            "Body": text}).raise_for_status()
    elif os.getenv("CALLMEBOT_KEY"):
        requests.get("https://api.callmebot.com/whatsapp.php", timeout=20,
                     params={"phone": to.lstrip("+"), "text": text, "apikey": os.environ["CALLMEBOT_KEY"]}).raise_for_status()


def notify(subject, body, send=True):
    print(f"[ALERT] {subject}\n{body}\n")
    if not send:
        return
    for fn, arg in ((send_email, (subject, body)), (send_whatsapp, (f"{subject}\n\n{body}",))):
        try:
            fn(*arg)
        except Exception as e:
            print(f"[notify] {fn.__name__} failed: {e}")


# ------------------------------------------------------------------ main
def to_usd(v, cur, rates):
    return None if v is None else round(v / rates[cur], 2)


def run(send=True, reader=None):
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    rates = get_rates(state)
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    br = None if reader else Browser()
    read = reader or (lambda tkey, t, sku: get_reading(br, tkey, t, sku))
    rows = {sku: {"sku": sku, "name": name, "cells": {}} for sku, name in CFG["skus"].items()}
    changes = state.setdefault("_changes", [])
    try:
        for tkey, t in CFG["targets"].items():
            for sku, name in CFG["skus"].items():
                k = f"{tkey}|{sku}"
                st = state.setdefault(k, {"fails": 0})
                cur = t["currency"]
                try:
                    r = read(tkey, t, sku)
                    cur = r.get("cur") or cur
                    if cur not in rates:
                        raise RuntimeError(f"unsupported currency {cur}")
                    st["fails"] = 0
                except Exception as e:
                    st["fails"] = st.get("fails", 0) + 1
                    print(f"[{name} / {t['site']}] {e}")
                    if "not configured" not in str(e) and st["fails"] == FAIL_ALERT_AT:
                        notify(f"Tracker issue: {t['site']} / {name}",
                               f"{FAIL_ALERT_AT} failed reads in a row: {e}", send)
                    rows[sku]["cells"][tkey] = {"local": st.get("local"), "currency": st.get("currency", cur),
                                                "usd": to_usd(st.get("local"), st.get("currency", cur), rates),
                                                "status": "unconfigured" if "not configured" in str(e) else "error",
                                                "ts": st.get("ts"), "src": st.get("src"), "prev": st.get("prev")}
                    continue
                price, old, old_stock = r["local"], st.get("local"), st.get("instock", True)
                if price is not None and old is not None and abs(price - old) >= 0.005:
                    if price < old or ALERT_ON_RISE:
                        verb = "DROPPED" if price < old else "ROSE"
                        body = (f"{name} on {t['site']} {verb}\n  Was: {old:.2f} {cur} (~${to_usd(old, cur, rates):.2f})\n"
                                f"  Now: {price:.2f} {cur} (${to_usd(price, cur, rates):.2f})\n  Link: {t['urls'][sku]}\n  Checked: {now} UTC")
                        notify(f"WHOOP price {verb.lower()}: {name} @ {t['site']}", body, send)
                    changes.insert(0, {"ts": now, "sku": sku, "name": name, "site": t["site"], "old": old,
                                       "new": price, "currency": cur})
                    st["prev"] = old
                elif price is not None and old is None and st.get("instock") is False:
                    notify(f"WHOOP back in stock: {name} @ {t['site']}", f"Now {price:.2f} {cur}", send)
                if price is not None:
                    st.update(local=price, currency=cur)
                st.update(instock=r["instock"], src=r["src"], ts=now)
                rows[sku]["cells"][tkey] = {"local": price if price is not None else st.get("local"), "currency": cur,
                                            "usd": to_usd(price if price is not None else st.get("local"), cur, rates),
                                            "status": "ok" if r["instock"] else "oos", "ts": now,
                                            "src": r["src"], "prev": st.get("prev")}
                print(f"{name:12} {t['site']:16} {price} {cur}  [{r['src']}]")
    finally:
        if br:
            br.close()
    del changes[40:]
    out = {"updated": now, "rates": rates,
           "columns": [{"key": k, "site": t["site"], "currency": t["currency"]} for k, t in CFG["targets"].items()],
           "rows": list(rows.values()), "changes": changes[:15]}
    OUT_FILE.parent.mkdir(exist_ok=True)
    OUT_FILE.write_text(json.dumps(out, indent=1))
    STATE_FILE.write_text(json.dumps(state, indent=1))
    with HIST_FILE.open("a", newline="") as f:
        w = csv.writer(f)
        if f.tell() == 0:
            w.writerow(["ts", "sku", "name", "site", "local", "currency", "usd", "status"])
        for row in rows.values():
            for tkey, c in row["cells"].items():
                w.writerow([now, row["sku"], row["name"], CFG["targets"][tkey]["site"], c["local"], c["currency"], c["usd"], c["status"]])


if __name__ == "__main__":
    run(send="--no-alerts" not in sys.argv)
