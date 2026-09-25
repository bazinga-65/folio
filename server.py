#!/usr/bin/env python3
"""
Net worth tracker: price server.

Fetches live quotes from Yahoo Finance, serves the dashboard (index.html),
stores your holdings in data.json next to this file, and records your net
worth once a day in history.jsonl.
No third-party packages needed. Python 3.8+.

Run:   python3 server.py
Open:  http://localhost:8787

Options (environment variables):
  PORT=8787        port to listen on
  HOST=127.0.0.1   interface to bind (keep the default unless you know why)
  DATA_FILE=...    where holdings are stored (default: data.json next to server.py)
  HISTORY_FILE=... daily net worth records (default: history.jsonl next to DATA_FILE)
  LOG_DIR=...      where server.log is written (default: logs/ next to server.py)
"""
import ipaddress
import json
import datetime
import logging
import os
import re
import shutil
import socket
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen

from history import History

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
HOST_TIMEOUT = 4  # a healthy answer takes well under a second
HOST_COOLDOWN = 30  # seconds a host that timed out or was unreachable is skipped
SLOW_BATCH = 5  # seconds; a batch slower than this is logged even when it succeeds
MAX_SYMBOLS = 100
LIST_KEYS = ("us", "in", "bcash", "bank", "hand", "pf", "other")
DAILY_BACKUP_RE = re.compile(r"^data-\d{4}-\d{2}-\d{2}\.json$")
VERSION_BACKUP_RE = re.compile(r"^data-v-\d{8}T\d{6}(?:-\d+)?\.json$")

DATA_FILE = os.environ.get("DATA_FILE", os.path.join(HERE, "data.json"))
BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(DATA_FILE)), "data-backups")
HISTORY_FILE = os.environ.get("HISTORY_FILE", os.path.join(os.path.dirname(os.path.abspath(DATA_FILE)), "history.jsonl"))
LOG_DIR = os.environ.get("LOG_DIR", os.path.join(HERE, "logs"))
LOG_FILE = os.path.join(LOG_DIR, "server.log")
MAX_BODY = 5 * 1024 * 1024
KEEP_BACKUPS = 30

_cache = {}
_cache_lock = threading.Lock()
_data_lock = threading.Lock()
_host_down_until = {}
_host_lock = threading.Lock()
_batch_lock = threading.Lock()
_batch_state = {"failing": False, "symbol_errors": {}}
_log = logging.getLogger("folio")
HISTORY = None


class RevConflict(Exception):
    def __init__(self, rev):
        self.rev = rev


def setup_logging():
    """server.log in LOG_DIR, rotated at 1 MB. Also the terminal when run by hand;
    under launchd stderr is logs/launchd.log, which then only gets crashes."""
    _log.setLevel(logging.INFO)
    _log.propagate = False
    fmt = logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%dT%H:%M:%S")
    handlers = []
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        handlers.append(RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8"))
    except OSError as exc:
        print(f"Could not write {LOG_FILE} ({exc}); logging to the terminal only.", file=sys.stderr)
    if sys.stderr.isatty() or not handlers:
        handlers.append(logging.StreamHandler(sys.stderr))
    for h in handlers:
        h.setFormatter(fmt)
        _log.addHandler(h)


def log_line(msg):
    _log.info(msg)


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


def _host_ok(host):
    with _host_lock:
        return time.monotonic() >= _host_down_until.get(host, 0)


def _host_failed(host):
    """Skip this host for a while, so one that hangs (for example just after
    the Mac wakes, before the network is back) cannot use up a whole batch."""
    with _host_lock:
        _host_down_until[host] = time.monotonic() + HOST_COOLDOWN


def _short(exc):
    return str(exc).split("\n", 1)[0][:200]


def _chart_error(exc):
    try:
        return ((json.loads(exc.read()).get("chart") or {}).get("error") or {}).get("description") or ""
    except Exception:
        return ""


def yahoo_chart(sym, query, deadline):
    """Fetch one chart from the first healthy Yahoo host that answers.
    Returns (result, None) or (None, (kind, message)), where kind is:
      symbol    Yahoo has no data for this symbol; retrying will not help
      http      Yahoo answered with an error status (rate limit, outage)
      timeout   a host did not answer within HOST_TIMEOUT
      network   a host could not be reached (DNS, refused, reset)
      offline   every host failed within HOST_COOLDOWN, so none was tried
      deadline  the batch ran out of time before this symbol was tried"""
    err = None
    for host in YAHOO_HOSTS:
        if not _host_ok(host):
            continue
        remaining = deadline - time.monotonic()
        if remaining < 0.5:
            return None, err or ("deadline", "not tried before the batch deadline")
        url = f"https://{host}/v8/finance/chart/{quote(sym)}?{query}"
        try:
            req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urlopen(req, timeout=min(HOST_TIMEOUT, remaining)) as resp:
                data = json.load(resp)
        except HTTPError as exc:
            desc = _chart_error(exc)
            if exc.code == 404:
                return None, ("symbol", desc or "symbol not found")
            err = ("http", f"HTTP {exc.code} from {host}" + (f": {desc}" if desc else ""))
            continue
        except URLError as exc:
            _host_failed(host)
            if isinstance(exc.reason, socket.timeout):
                err = ("timeout", f"{host} did not answer in {HOST_TIMEOUT}s")
            else:
                err = ("network", f"{host}: {_short(exc.reason)}")
            continue
        except socket.timeout:
            _host_failed(host)
            err = ("timeout", f"{host} stopped answering mid-response")
            continue
        except ValueError as exc:
            err = ("http", f"{host}: unreadable response ({_short(exc)})")
            continue
        except OSError as exc:
            _host_failed(host)
            err = ("network", f"{host}: {_short(exc)}")
            continue
        chart = data.get("chart") or {}
        result = (chart.get("result") or [None])[0]
        if not result:
            return None, ("symbol", (chart.get("error") or {}).get("description") or "symbol not found")
        return result, None
    return None, err or ("offline", f"every Yahoo host failed in the last {HOST_COOLDOWN}s")


def fetch_symbol(sym, deadline):
    hit = _cache_get(sym)
    if hit is not None:
        return hit
    result, err = yahoo_chart(sym, "interval=1d&range=5d", deadline)
    if err is None:
        meta = result.get("meta", {})
        price = meta.get("regularMarketPrice")
        if price is None:
            err = ("symbol", "no price in response")
    if err:
        out = {"error": err[1], "kind": err[0]}
        if err[0] in ("symbol", "http"):
            _cache_put(sym, out, ERROR_TTL)
        return out

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


def _log_batch(symbols, results, elapsed):
    """One line per batch that failed or was slow, one when quotes recover, and
    one per symbol whose own error (e.g. a delisted ticker) is new."""
    failed = []
    with _batch_lock:
        seen = _batch_state["symbol_errors"]
        for sym, r in zip(symbols, results):
            kind = r.get("kind")
            if kind == "symbol":
                if seen.get(sym) != r["error"]:
                    seen[sym] = r["error"]
                    log_line(f"quote {sym}: {r['error']}")
            else:
                seen.pop(sym, None)
                if kind:
                    failed.append((sym, r))
        was_failing = _batch_state["failing"]
        _batch_state["failing"] = bool(failed)
    if failed:
        kinds = ", ".join(f"{k} {n}" for k, n in Counter(r["kind"] for _, r in failed).most_common())
        sym, r = next(((s, r) for s, r in failed if r["kind"] not in ("offline", "deadline")), failed[0])
        log_line(f"quotes: {len(failed)}/{len(symbols)} failed in {elapsed:.1f}s ({kinds}); e.g. {sym}: {r['error']}")
    elif was_failing:
        log_line(f"quotes: recovered, {len(symbols)} ok in {elapsed:.1f}s")
    elif elapsed > SLOW_BATCH:
        log_line(f"quotes: slow, {len(symbols)} ok in {elapsed:.1f}s")


def fetch_many(symbols):
    start = time.monotonic()
    deadline = start + QUOTE_DEADLINE
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda sym: fetch_symbol(sym, deadline), symbols))
    _log_batch(symbols, results, time.monotonic() - start)
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
        if not self.path.startswith(("/api/data", "/api/history")):
            self._cors()  # quotes are public data; holdings and history never get CORS headers
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
        if HISTORY:
            HISTORY.kick()
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

        if url.path == "/api/history":
            if not self._local_only_ok():
                return self._json(403, {"error": "forbidden"}, cors=False)
            days = HISTORY.summary() if HISTORY else []
            return self._json(200, {"days": days}, cors=False)

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


def _portfolio_or_none():
    try:
        return read_stored()[0]
    except (ValueError, OSError) as exc:
        log_line(f"history: could not read {DATA_FILE}: {exc}")
        return None


if __name__ == "__main__":
    setup_logging()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    log_line(f"Net worth tracker running at http://localhost:{PORT} (pid {os.getpid()}, Python {sys.version.split()[0]})")
    log_line(f"Holdings: {DATA_FILE}  History: {HISTORY_FILE}  Log: {LOG_FILE}")
    HISTORY = History(HISTORY_FILE, BACKUP_DIR, _portfolio_or_none, yahoo_chart, log_line)
    HISTORY.start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log_line("Stopped.")
