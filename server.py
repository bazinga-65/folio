#!/usr/bin/env python3
"""
Net worth tracker: price server.

Fetches live quotes from Yahoo Finance, serves the dashboard (index.html), and
stores your holdings in data.json next to this file.
No third-party packages needed. Python 3.8+.

Run:   python3 server.py
Open:  http://localhost:8787

Options (environment variables):
  PORT=8787        port to listen on
  HOST=127.0.0.1   interface to bind (keep the default unless you know why)
  DATA_FILE=...    where holdings are stored (default: data.json next to server.py)
"""
import ipaddress
import json
import datetime
import os
import re
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen

HERE = os.path.dirname(os.path.abspath(__file__))
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8787"))

YAHOO_HOSTS = ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
SYMBOL_RE = re.compile(r"^[A-Za-z0-9.^=&_\-]{1,24}$")
CACHE_TTL = 20  # seconds; stops rapid refreshes from hammering Yahoo
ERROR_TTL = 15  # seconds; a failed symbol is not fetched again on every refresh
QUOTE_DEADLINE = 16  # seconds for the whole batch; the page stops waiting at 20
HOST_TIMEOUT = 8
MAX_SYMBOLS = 100
LIST_KEYS = ("us", "in", "bcash", "bank", "pf", "other")
DAILY_BACKUP_RE = re.compile(r"^data-\d{4}-\d{2}-\d{2}\.json$")
VERSION_BACKUP_RE = re.compile(r"^data-v-\d{8}T\d{6}(?:-\d+)?\.json$")

DATA_FILE = os.environ.get("DATA_FILE", os.path.join(HERE, "data.json"))
BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(DATA_FILE)), "data-backups")
MAX_BODY = 5 * 1024 * 1024
KEEP_BACKUPS = 30

_cache = {}
_cache_lock = threading.Lock()
_data_lock = threading.Lock()


class RevConflict(Exception):
    def __init__(self, rev):
        self.rev = rev


def log_line(msg):
    print(f"{datetime.datetime.now().isoformat(timespec='seconds')} {msg}", file=sys.stderr, flush=True)


def _unwrap(stored):
    """Return (portfolio, rev). Older files are the portfolio itself, at rev 1."""
    if (
        isinstance(stored, dict)
        and isinstance(stored.get("rev"), int)
        and isinstance(stored.get("portfolio"), dict)
    ):
        return stored["portfolio"], stored["rev"]
    return stored, 1


def read_stored():
    """Return (portfolio, rev). portfolio is None when the file does not exist yet.
    A corrupt file raises, so the dashboard never overwrites it by mistake."""
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            stored = json.load(f)
    except FileNotFoundError:
        return None, 0
    if not isinstance(stored, dict):
        raise ValueError("data file is not a JSON object")
    return _unwrap(stored)


def validate_portfolio(obj):
    if not isinstance(obj, dict):
        raise ValueError("expected a JSON object")
    if not isinstance(obj.get("settings"), dict):
        raise ValueError("expected a settings object")
    for key in LIST_KEYS:
        if key in obj and not isinstance(obj[key], list):
            raise ValueError(f"expected {key} to be a list")


def _prune(names, pattern):
    matched = sorted(name for name in names if pattern.match(name))
    for old in matched[:-KEEP_BACKUPS]:
        try:
            os.remove(os.path.join(BACKUP_DIR, old))
        except OSError:
            pass


def _backup_current():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    day = os.path.join(BACKUP_DIR, f"data-{datetime.date.today().isoformat()}.json")
    if not os.path.exists(day):
        shutil.copy2(DATA_FILE, day)
    stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    version = os.path.join(BACKUP_DIR, f"data-v-{stamp}.json")
    n = 2
    while os.path.exists(version):
        version = os.path.join(BACKUP_DIR, f"data-v-{stamp}-{n}.json")
        n += 1
    shutil.copy2(DATA_FILE, version)
    names = os.listdir(BACKUP_DIR)
    _prune(names, DAILY_BACKUP_RE)
    _prune(names, VERSION_BACKUP_RE)


def write_data(obj, expected_rev):
    """Replace the file only when expected_rev is the rev currently on disk.
    Returns the new rev. Raises RevConflict when another save landed first."""
    validate_portfolio(obj)
    with _data_lock:
        current, rev = read_stored()
        if expected_rev != rev:
            raise RevConflict(rev)
        if current is not None:
            _backup_current()
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"rev": rev + 1, "portfolio": obj}, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, DATA_FILE)  # atomic: never leaves a half-written file
        return rev + 1


def _cache_get(sym):
    with _cache_lock:
        hit = _cache.get(sym)
        if hit and time.monotonic() - hit[0] < hit[2]:
            return hit[1]
    return None


def _cache_put(sym, payload, ttl):
    with _cache_lock:
        _cache[sym] = (time.monotonic(), payload, ttl)


def fetch_symbol(sym, deadline):
    hit = _cache_get(sym)
    if hit is not None:
        return hit
    if time.monotonic() >= deadline:
        return {"error": "quote deadline exceeded"}

    last_error = "unknown error"
    for host in YAHOO_HOSTS:
        remaining = deadline - time.monotonic()
        if remaining < 0.5:
            last_error = "quote deadline exceeded"
            break
        url = f"https://{host}/v8/finance/chart/{quote(sym)}?interval=1d&range=5d"
        try:
            req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urlopen(req, timeout=min(HOST_TIMEOUT, remaining)) as resp:
                data = json.load(resp)

            result = (data.get("chart", {}).get("result") or [None])[0]
            if not result:
                err = data.get("chart", {}).get("error") or {}
                last_error = err.get("description", "symbol not found")
                continue

            meta = result.get("meta", {})
            price = meta.get("regularMarketPrice")
            if price is None:
                last_error = "no price in response"
                continue

            quotes = (result.get("indicators", {}).get("quote") or [{}])[0]
            closes = [c for c in (quotes.get("close") or []) if c is not None]
            # Previous session close = second-to-last daily bar.
            prev = closes[-2] if len(closes) >= 2 else (
                meta.get("previousClose") or meta.get("chartPreviousClose")
            )

            out = {
                "price": price,
                "prev": prev,
                "currency": meta.get("currency"),
                "name": meta.get("shortName") or meta.get("longName"),
                "exchange": meta.get("exchangeName"),
                "time": meta.get("regularMarketTime"),
            }
            _cache_put(sym, out, CACHE_TTL)
            return out
        except Exception as exc:  # network error, bad JSON, HTTP error
            last_error = str(exc).split("\n", 1)[0][:200]

    if last_error == "quote deadline exceeded":
        return {"error": last_error}
    log_line(f"quote {sym}: {last_error}")
    out = {"error": last_error}
    _cache_put(sym, out, ERROR_TTL)
    return out


def fetch_many(symbols):
    deadline = time.monotonic() + QUOTE_DEADLINE
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda sym: fetch_symbol(sym, deadline), symbols))
    late = sum(1 for item in results if item.get("error") == "quote deadline exceeded")
    if late:
        log_line(f"quotes: deadline, {late} symbol(s) not fetched")
    return dict(zip(symbols, results))


class Handler(BaseHTTPRequestHandler):
    server_version = "NetWorthPrices/1.0"

    def log_message(self, fmt, *args):
        pass  # keep the terminal quiet

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _peer_is_loopback(self):
        ip = self.client_address[0]
        if ip.startswith("::ffff:"):
            ip = ip[7:]
        try:
            return ipaddress.ip_address(ip).is_loopback
        except ValueError:
            return False

    def _local_only_ok(self):
        """Holdings are private: the connection itself must be from this
        machine, and the page must be one this server served (blocks other
        websites and DNS-rebinding tricks). A widened HOST cannot spoof this
        with a Host header."""
        if not self._peer_is_loopback():
            return False
        hosts = {f"localhost:{PORT}", f"127.0.0.1:{PORT}", f"[::1]:{PORT}", f"{HOST}:{PORT}"}
        if self.headers.get("Host", "") not in hosts:
            return False
        origin = self.headers.get("Origin")
        return not origin or origin in {f"http://{h}" for h in hosts}

    def _if_match(self):
        raw = self.headers.get("If-Match", "").strip().strip('"')
        if not raw.isdigit():
            return None
        return int(raw)

    def _send(self, code, body, ctype, cors=True):
        self.send_response(code)
        if cors:
            self._cors()
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, payload, cors=True):
        self._send(code, json.dumps(payload).encode(), "application/json; charset=utf-8", cors)

    def do_OPTIONS(self):
        self.send_response(204)
        if not self.path.startswith("/api/data"):
            self._cors()  # quotes are public data; holdings never get CORS headers
        self.end_headers()

    def do_PUT(self):
        if urlparse(self.path).path != "/api/data":
            return self._json(404, {"error": "not found"})
        if not self._local_only_ok():
            return self._json(403, {"error": "forbidden"}, cors=False)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            return self._json(413, {"error": "body missing or too large"}, cors=False)
        expected = self._if_match()
        if expected is None:
            return self._json(428, {"error": "If-Match revision required"}, cors=False)
        try:
            obj = json.loads(self.rfile.read(length))
            validate_portfolio(obj)
        except ValueError as exc:
            return self._json(400, {"error": f"invalid JSON: {exc}"}, cors=False)
        try:
            rev = write_data(obj, expected)
        except RevConflict as exc:
            return self._json(409, {"error": "conflict", "rev": exc.rev}, cors=False)
        except ValueError as exc:
            return self._json(500, {"error": f"could not read data file: {exc}"}, cors=False)
        except OSError as exc:
            return self._json(500, {"error": f"could not write data file: {exc}"}, cors=False)
        self._json(200, {"ok": True, "rev": rev}, cors=False)

    def do_GET(self):
        url = urlparse(self.path)

        if url.path == "/api/health":
            return self._json(200, {"ok": True})

        if url.path == "/api/data":
            if not self._local_only_ok():
                return self._json(403, {"error": "forbidden"}, cors=False)
            try:
                data, rev = read_stored()
            except (ValueError, OSError) as exc:
                return self._json(500, {"error": f"could not read data file: {exc}"}, cors=False)
            return self._json(200, {"exists": data is not None, "data": data, "rev": rev}, cors=False)

        if url.path == "/api/quotes":
            raw = parse_qs(url.query).get("symbols", [""])[0]
            symbols = []
            for s in raw.split(","):
                s = s.strip()
                if s and SYMBOL_RE.match(s) and s not in symbols:
                    symbols.append(s)
            symbols = symbols[:MAX_SYMBOLS]
            if not symbols:
                return self._json(400, {"error": "Pass ?symbols=AAPL,RELIANCE.NS,USDINR=X"})
            return self._json(200, {"quotes": fetch_many(symbols), "fetchedAt": int(time.time())})

        if url.path in ("/", "/index.html"):
            try:
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                return self._json(404, {"error": "index.html not found next to server.py"})

        self._json(404, {"error": "not found"})


if __name__ == "__main__":
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Net worth tracker running at http://localhost:{PORT}  (Ctrl+C to stop)", flush=True)
    print(f"Holdings are stored in {DATA_FILE}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
