#!/usr/bin/env python3
"""
Solar Guitars free-product checker
==================================

Checks the Solar Guitars webstore every minute for products priced at 0 and
notifies you (email + Windows desktop notification + opens the page in your
browser) with the product name and link.

Each round it looks at:
  1. The store pages listed in config.ini, e.g.
     https://www.solar-guitars.com/shop/?min=0&max=1
     https://www.solar-guitars.com/black-sabbath/?min=0&max=1
     (server-rendered; out-of-stock items lack the "inStock" class)
  2. The WooCommerce Store API, which covers every product, including
     accessories the pages don't list.

A few old products already sit at 0 but are out of stock. They are recorded
silently on the first run, so you only hear about something new, or about one
of those coming back in stock.

Usage:
  python solar_checker.py             run forever (Ctrl+C to stop)
  python solar_checker.py --dry-run   check once, print what was found, no notifications
  python solar_checker.py --test      send a test email + desktop notification
  python solar_checker.py --test URL  same, using the first product listed on URL
  python solar_checker.py --max-minutes 340   stop after ~340 minutes (used on GitHub Actions)

Settings live in config.ini; private ones (email login) in config.local.ini or
SOLAR_* environment variables, e.g. SOLAR_SMTP_PASSWORD.

Standard library only, Python 3.8+.
"""
from __future__ import annotations

import argparse
import base64
import configparser
import ctypes
import html
import json
import logging
import logging.handlers
import os
import re
import smtplib
import ssl
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape, quoteattr

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.ini"
LOCAL_CONFIG_PATH = HERE / "config.local.ini"  # your private settings, not committed
STATE_PATH = HERE / "state.json"
LOG_PATH = HERE / "checker.log"

STORE_API = "https://www.solar-guitars.com/wp-json/wc/store/v1/products"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
HTTP_TIMEOUT = 30

log = logging.getLogger("solar")


# --------------------------------------------------------------------- config

@dataclass
class Config:
    pages: list
    use_store_api: bool
    max_price: float
    interval: int
    open_browser: bool
    desktop: bool
    prevent_sleep: bool
    warn_after_failures: int
    email_enabled: bool
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    email_from: str
    email_to: list

    def email_problem(self) -> str | None:
        """Why email can't be sent, or None if it looks configured."""
        if not self.email_enabled:
            return "email is disabled in config.ini"
        missing = [k for k in ("smtp_host", "smtp_user", "smtp_password") if not getattr(self, k)]
        if missing:
            return (f"set {', '.join(missing)} in config.local.ini "
                    f"(on GitHub: secrets {', '.join('SOLAR_' + k.upper() for k in missing)})")
        return None


# Every setting can also come from an environment variable named SOLAR_<KEY>,
# e.g. SOLAR_SMTP_PASSWORD. That's how GitHub Actions secrets reach the checker.
CONFIG_KEYS = {
    "checker": ("pages", "use_store_api", "max_price", "interval_seconds", "open_browser",
                "desktop_notification", "prevent_sleep", "warn_after_failures"),
    "email": ("smtp_host", "smtp_port", "smtp_user", "smtp_password", "email_from", "email_to"),
}


def load_config() -> Config:
    if not CONFIG_PATH.exists():
        sys.exit(f"Missing {CONFIG_PATH} - it should sit next to solar_checker.py.")
    cp = configparser.ConfigParser(interpolation=None)
    cp.read_dict({"checker": {}, "email": {}})
    cp.read([CONFIG_PATH, LOCAL_CONFIG_PATH], encoding="utf-8")  # local file wins
    for section, keys in CONFIG_KEYS.items():
        for key in keys:
            value = os.environ.get(f"SOLAR_{key.upper()}", "").strip()
            if value:  # unset GitHub secrets arrive as empty strings
                cp[section][key] = value
    c, e = cp["checker"], cp["email"]
    on_windows = os.name == "nt"
    smtp_user = e.get("smtp_user", "").strip()
    return Config(
        pages=c.get("pages", "https://www.solar-guitars.com/shop/?min=0&max=1").split(),
        use_store_api=c.getboolean("use_store_api", True),
        max_price=c.getfloat("max_price", 0.0),
        interval=max(15, c.getint("interval_seconds", 60)),
        # No screen to show anything on a CI server (GitHub Actions sets CI=true).
        open_browser=c.getboolean("open_browser", True) and not os.environ.get("CI"),
        desktop=c.getboolean("desktop_notification", True) and on_windows,
        prevent_sleep=c.getboolean("prevent_sleep", True) and on_windows,
        warn_after_failures=max(1, c.getint("warn_after_failures", 10)),
        email_enabled=e.getboolean("enabled", True),
        smtp_host=e.get("smtp_host", "").strip(),
        smtp_port=e.getint("smtp_port", 587),
        smtp_user=smtp_user,
        smtp_password=e.get("smtp_password", "").strip(),
        # Both default to the account you log in with.
        email_from=e.get("email_from", "").strip() or smtp_user,
        email_to=[a.strip() for a in e.get("email_to", "").split(",") if a.strip()] or [smtp_user],
    )


# ------------------------------------------------------------------- fetching

@dataclass
class Product:
    pid: str
    name: str
    url: str
    price: float
    available: bool
    currency: str = "€"

    @property
    def price_text(self) -> str:
        return f"{self.currency}{self.price:.2f}"

    @property
    def status_text(self) -> str:
        return "AVAILABLE (in stock)" if self.available else "listed, but out of stock / not buyable yet"


def http_get(url: str) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
        "Cache-Control": "no-cache",
    })
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


def clean_text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


LI_RE = re.compile(r'<li\s+class="(listing-product\b[^"]*)"', re.I)


def parse_shop_page(page: str) -> list:
    """Products listed on the shop page (it is already price-filtered by the URL)."""
    start = page.find('id="listing-filters"')
    if start < 0:
        raise ValueError('product list (id="listing-filters") not found - did the page layout change?')
    end = page.find('class="pagination"', start)
    chunks = LI_RE.split(page[start:end if end > 0 else len(page)])
    products = []
    for classes, body in zip(chunks[1::2], chunks[2::2]):
        url_m = re.search(r'href="(https?://[^"]+/product/[^"]+)"', body)
        if not url_m:
            continue
        url = html.unescape(url_m.group(1))
        name_m = re.search(r'class="item-compare-title"[^>]*>(.*?)</h5>', body, re.S)
        name = clean_text(name_m.group(1)) if name_m else url.rstrip("/").rsplit("/", 1)[-1]
        price_m = re.search(r'class="price_for_filter"[^>]*>\s*([\d.,]+)', body)
        # No readable price on a page filtered to <= 1: treat it as free rather than miss it.
        price = float(price_m.group(1).replace(",", "")) if price_m else 0.0
        id_m = re.search(r'data-product-id="(\d+)"', body)
        products.append(Product(
            pid=id_m.group(1) if id_m else url,
            name=name,
            url=url,
            price=price,
            available="inStock" in classes.split(),
        ))
    return products


def fetch_store_api(max_price: float) -> list:
    """Every product at or below max_price, via the WooCommerce Store API."""
    # Store API price filters are in minor units (cents); +1 so a limit of 0
    # isn't read as "no limit".
    max_cents = int(round(max_price * 100)) + 1
    products = []
    for page in range(1, 11):
        query = urllib.parse.urlencode(
            {"min_price": 0, "max_price": max_cents, "per_page": 100, "page": page})
        data = json.loads(http_get(f"{STORE_API}?{query}"))
        if not isinstance(data, list):
            raise ValueError(f"unexpected Store API response: {str(data)[:200]}")
        for item in data:
            prices = item.get("prices") or {}
            minor = int(prices.get("currency_minor_unit") or 2)
            price = float(prices.get("price") or 0) / 10 ** minor
            products.append(Product(
                pid=str(item.get("id")),
                name=html.unescape(item.get("name") or "").strip() or f"Product {item.get('id')}",
                url=item.get("permalink") or "",
                price=price,
                available=bool(item.get("is_in_stock")) and bool(item.get("is_purchasable", True)),
                currency=html.unescape(prices.get("currency_symbol") or "€"),
            ))
        if len(data) < 100:
            break
    return products


def check(cfg: Config):
    """Return ({pid: Product} at or below max_price, {source: count}, [errors])."""
    sources = []
    if cfg.use_store_api:  # first, so its fuller product names win when merging
        sources.append(("Store API", lambda: fetch_store_api(cfg.max_price)))
    # Fetch each URL exactly as given: the site serves them uncached, and extra
    # query parameters make it drop the product list.
    for url in cfg.pages:
        label = (urllib.parse.urlsplit(url).path.strip("/") or url) + " page"
        sources.append((label, lambda url=url: parse_shop_page(http_get(url))))

    found, by_url, counts, errors = {}, {}, {}, []
    for label, fetch in sources:
        try:
            items = [p for p in fetch() if p.price <= cfg.max_price + 1e-9]
        except Exception as exc:  # network error, HTTP error, bad JSON, layout change...
            errors.append(f"{label}: {exc}")
            continue
        counts[label] = len(items)
        for p in items:
            key = p.pid if p.pid in found else by_url.get(p.url, p.pid)
            if key in found:
                found[key].available = found[key].available or p.available
            else:
                found[key] = p
                by_url[p.url] = key
    return found, counts, errors


# -------------------------------------------------------------- notifications

TOAST_PS = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($env:SOLAR_TOAST_XML)
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($xml))
"""


def desktop_notify(title: str, text: str, url: str | None = None) -> bool:
    """Windows toast notification; clicking it opens `url`."""
    if os.name != "nt":
        return False
    launch = f' activationType="protocol" launch={quoteattr(url)}' if url else ""
    xml = (f'<toast{launch} duration="long"><visual><binding template="ToastGeneric">'
           f'<text>{xml_escape(title)}</text><text>{xml_escape(text)}</text>'
           f'</binding></visual><audio src="ms-winsoundevent:Notification.Reminder"/></toast>')
    encoded = base64.b64encode(TOAST_PS.encode("utf-16-le")).decode("ascii")
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            env={**os.environ, "SOLAR_TOAST_XML": xml},
            capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        log.warning("Desktop notification failed: %s", exc)
        return False
    if result.returncode != 0:
        log.warning("Desktop notification failed: %s", (result.stderr or "").strip()[:300])
        return False
    return True


def send_email(cfg: Config, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.email_from
    msg["To"] = ", ".join(cfg.email_to)
    msg.set_content(body)
    context = ssl.create_default_context()
    if cfg.smtp_port == 465:
        with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, context=context, timeout=30) as smtp:
            smtp.login(cfg.smtp_user, cfg.smtp_password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30) as smtp:
            smtp.starttls(context=context)
            smtp.login(cfg.smtp_user, cfg.smtp_password)
            smtp.send_message(msg)


def email_error_hint(exc: Exception) -> str:
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return (f"{exc} -- login rejected. For Gmail you need an App Password "
                "(https://myaccount.google.com/apppasswords), not your normal password.")
    return str(exc)


def build_alert_email(products: list, cfg: Config):
    available = [p for p in products if p.available]
    lead = available[0] if available else products[0]
    more = f" (+{len(products) - 1} more)" if len(products) > 1 else ""
    if available:
        subject = f"Solar Guitars: FREE product available - {lead.name}{more}"
    else:
        subject = f"Solar Guitars: new {lead.price_text} product listed (not buyable yet) - {lead.name}{more}"
    lines = [f"The Solar Guitars checker found {len(products)} product(s) at "
             f"{lead.currency}{cfg.max_price:.2f}:", ""]
    for p in products:
        lines += [f"* {p.name}",
                  f"  Price:  {p.price_text}",
                  f"  Status: {p.status_text}",
                  f"  Link:   {p.url}",
                  ""]
    lines += ["Pages watched:"] + [f"  {url}" for url in cfg.pages]
    lines += ["", f"Checked at {datetime.now():%Y-%m-%d %H:%M:%S}"]
    return subject, "\n".join(lines)


# -------------------------------------------------------------------- watcher

def load_state() -> dict:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(data.get("products"), dict):
            return {"initialized": bool(data.get("initialized")), "products": data["products"]}
    except FileNotFoundError:
        pass
    except Exception as exc:
        log.warning("Ignoring unreadable %s: %s", STATE_PATH.name, exc)
    return {"initialized": False, "products": {}}


def save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


class Watcher:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.state = load_state()
        self.pending_email = []  # alerted products whose email hasn't gone out yet
        self.fail_streak = 0
        self.warned = False

    def tick(self) -> None:
        found, counts, errors = check(self.cfg)

        if not counts:  # every source failed
            self.fail_streak += 1
            log.warning("Check failed (%d in a row): %s", self.fail_streak, " | ".join(errors))
            if self.fail_streak >= self.cfg.warn_after_failures and not self.warned:
                self.warned = True
                self.warn_unreachable(errors)
            self.flush_email()
            return
        if self.warned:
            log.info("Site is reachable again.")
        self.fail_streak, self.warned = 0, False
        for err in errors:
            log.warning("Partial check, %s", err)

        seen = self.state["products"]
        baseline_run = not self.state["initialized"]
        alerts = []
        for pid, p in found.items():
            prev = (seen.get(pid) or {}).get("status")
            if p.available and prev != "available":
                alerts.append(p)  # newly buyable at 0
            elif not p.available and prev is None and not baseline_run:
                alerts.append(p)  # brand-new 0 listing, not buyable yet
            seen[pid] = {"status": "available" if p.available else "listed",
                         "name": p.name, "url": p.url}
        if not errors:
            # Only forget products (and finish the baseline) when every source answered,
            # so a source that failed this round can't make old items look new next round.
            for pid in [k for k in seen if k not in found]:
                del seen[pid]
            self.state["initialized"] = True
        save_state(self.state)

        buyable = sum(p.available for p in found.values())
        sources = ", ".join(f"{k}: {v}" for k, v in counts.items())
        log.info("Checked - %d product(s) at %.2f, %d buyable  [%s]",
                 len(found), self.cfg.max_price, buyable, sources)
        if baseline_run and found and not errors:
            log.info("First run: %d existing out-of-stock product(s) at 0 recorded; "
                     "you'll be alerted if they come back in stock or something new appears.",
                     len(found) - buyable)
        if alerts:
            self.alert(alerts)
        self.flush_email()

    def alert(self, products: list) -> None:
        for p in products:
            log.warning("*** FOUND: %s | %s | %s | %s", p.name, p.price_text, p.status_text, p.url)
        available = [p for p in products if p.available]
        lead = available[0] if available else products[0]
        if self.cfg.desktop:
            title = f"FREE on Solar Guitars: {lead.name}" if available else f"New {lead.price_text} listing: {lead.name}"
            extra = f" (+{len(products) - 1} more)" if len(products) > 1 else ""
            desktop_notify(title, f"{lead.price_text} - {lead.status_text}{extra}. Click to open.", lead.url)
        if self.cfg.open_browser:
            for p in available[:3]:
                webbrowser.open(p.url)
        self.pending_email.extend(products)

    def flush_email(self) -> None:
        if not self.pending_email:
            return
        problem = self.cfg.email_problem()
        if problem:
            log.warning("Not emailing (%s).", problem)
            self.pending_email.clear()
            return
        subject, body = build_alert_email(self.pending_email, self.cfg)
        try:
            send_email(self.cfg, subject, body)
        except Exception as exc:
            log.error("Email failed, will retry next check: %s", email_error_hint(exc))
            return
        log.info("Email sent.")
        self.pending_email.clear()

    def warn_unreachable(self, errors: list) -> None:
        minutes = self.fail_streak * self.cfg.interval // 60
        text = (f"The Solar Guitars checker couldn't reach the store for the last "
                f"{self.fail_streak} checks (~{minutes} min), so it may miss a free product.\n\n"
                + "\n".join(errors) + "\n\nIt keeps trying and will carry on once the site answers.")
        log.error("Site unreachable for %d checks in a row - sending a warning.", self.fail_streak)
        if self.cfg.desktop:
            desktop_notify("Solar checker can't reach the store", errors[0][:200])
        if not self.cfg.email_problem():
            try:
                send_email(self.cfg, "Solar Guitars checker: can't reach the store", text)
            except Exception as exc:
                log.error("Warning email failed: %s", email_error_hint(exc))


# ----------------------------------------------------------------------- main

def setup_logging() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    logfile = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    for handler in (console, logfile):
        handler.setFormatter(fmt)
        log.addHandler(handler)
    log.setLevel(logging.INFO)


def keep_awake() -> None:
    """Stop Windows from going to sleep while the checker runs (the screen may still turn off)."""
    if os.name == "nt":
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)


def run_dry(cfg: Config) -> None:
    found, counts, errors = check(cfg)
    for err in errors:
        print(f"ERROR  {err}")
    print(f"Sources: {counts}")
    known = load_state()["products"]
    print(f"{len(found)} product(s) at or below {cfg.max_price:.2f}:")
    for p in found.values():
        tag = "already known" if p.pid in known else "new"
        print(f"  - [{'BUYABLE' if p.available else 'not buyable'}] [{tag}] {p.name} | {p.price_text} | {p.url}")


def run_test(cfg: Config, page_url: str | None) -> int:
    """Send the alert notifications for a sample product (the first one on page_url, if given).
    Returns a process exit code, so a failed test shows up red on GitHub."""
    if page_url:
        try:
            products = parse_shop_page(http_get(page_url))
        except Exception as exc:
            print(f"Couldn't read {page_url}: {exc}")
            return 1
        if not products:
            print(f"No products listed on {page_url} - try a wider price range.")
            return 1
        sample = products[0]
        print(f"Test product (first on that page): {sample.name} | {sample.price_text} | {sample.url}")
    else:
        sample = Product("0", "TEST PRODUCT", cfg.pages[0], 0.0, True)
    if cfg.desktop:
        ok = desktop_notify(f"[TEST] FREE on Solar Guitars: {sample.name}",
                            f"{sample.price_text} - this is only a test. Click to open.", sample.url)
        print("Desktop notification:", "shown" if ok else "FAILED (see message above)")
    problem = cfg.email_problem()
    if problem:
        print(f"Email NOT sent: {problem}")
        return 1
    subject, body = build_alert_email([sample], cfg)
    body = ("*** THIS IS A TEST - the product below is not actually free. ***\n"
            "This is what a real alert from the Solar Guitars checker looks like.\n\n" + body)
    try:
        send_email(cfg, "[TEST] " + subject, body)
    except Exception as exc:
        print(f"Email FAILED: {email_error_hint(exc)}")
        return 1
    print(f"Test email sent to {len(cfg.email_to)} recipient(s) - check your inbox (and spam folder).")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Alert when Solar Guitars lists a product at 0.")
    parser.add_argument("--dry-run", action="store_true", help="check once and print, no notifications")
    parser.add_argument("--test", nargs="?", const="", metavar="PAGE_URL",
                        help="send a test email + desktop notification "
                             "(for the first product on PAGE_URL, if given)")
    parser.add_argument("--max-minutes", type=float, metavar="N",
                        help="stop after about N minutes (GitHub Actions jobs are limited to 6 hours)")
    args = parser.parse_args()

    cfg = load_config()
    setup_logging()
    if args.test is not None:
        sys.exit(run_test(cfg, args.test or None))
    if args.dry_run:
        return run_dry(cfg)

    if cfg.prevent_sleep:
        keep_awake()
    problem = cfg.email_problem()
    log.info("Solar checker started: every %ds, alerting on products at or below %.2f.",
             cfg.interval, cfg.max_price)
    # Count only: logs on GitHub are public in a public repo, so no addresses here.
    log.info("Email: %s", f"OFF ({problem})" if problem else f"on, to {len(cfg.email_to)} recipient(s)")
    deadline = time.monotonic() + args.max_minutes * 60 if args.max_minutes else None
    watcher = Watcher(cfg)
    try:
        while True:
            started = time.monotonic()
            try:
                watcher.tick()
            except Exception:
                log.exception("Unexpected error during check (will keep going)")
            if deadline and time.monotonic() + cfg.interval > deadline:
                log.info("Reached --max-minutes %g, stopping.", args.max_minutes)
                break
            time.sleep(max(1.0, cfg.interval - (time.monotonic() - started)))
    except KeyboardInterrupt:
        log.info("Stopped.")


if __name__ == "__main__":
    main()
