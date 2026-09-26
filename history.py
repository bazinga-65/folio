"""
Daily net worth history.

One record per local calendar date, kept in history.jsonl (one JSON object per
line). Every date is valued at that day's closing prices, so a date the server
missed (laptop closed or asleep) is filled in later with the number it would
have had. A date stays provisional, and is re-valued on every pass, until noon
the next day, by which time every market has closed for it.

Holdings only change through the dashboard, which needs this server, so a
missed date carries the holdings of the record before it.
"""
import bisect
import datetime
import json
import os
import re
import shutil
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

LIST_KEYS = ("us", "in", "bcash", "bank", "hand", "pf", "other")
PRICED = ("us", "in", "other")
# code, Yahoo symbol, history fx key, settings fallback field. Rates match index.html.
EXTRA_FX = (
    ("THB", "USDTHB=X", "USDTHB", "thbManual"),
    ("AED", "USDAED=X", "USDAED", "aedManual"),
)
FX_FALLBACK = {"USDINR=X": 85.0, "USDTHB=X": 33.0, "USDAED=X": 3.67}  # the dashboard's defaults
SETTLE_HOURS = 36  # from the start of a date: noon the next day
GIVE_UP_DAYS = 7  # a date still missing prices after this keeps its best guess
MAX_BACKFILL_DAYS = 400
LOOKBACK_DAYS = 10  # so a weekend or holiday still finds the previous close
PASS_EVERY = 15 * 60
RETRY_EVERY = 2 * 60
CHECK_EVERY = 60
SETTLE_AFTER_SAVE = 3  # seconds to let a burst of saves finish before a pass
FETCH_DEADLINE = 60
FETCH_WORKERS = 4
KEEP_BACKUPS = 30
BACKUP_RE = re.compile(r"^history-\d{4}-\d{2}-\d{2}\.jsonl$")


def yahoo_symbol(kind, h):
    s = str(h.get("symbol") or "").strip().upper()
    if not s:
        return ""
    if kind == "in" and not s.endswith((".NS", ".BO")):
        return s + (".BO" if h.get("exchange") == "BSE" else ".NS")
    return s


def _num(v):
    try:
        return float(v) if v not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


def _day(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).date().isoformat()


def daily_closes(result):
    """[(exchange-local date, close)] sorted by date. The latest price stands in
    for the current session, whose bar Yahoo often leaves empty or duplicates."""
    meta = result.get("meta") or {}
    offset = meta.get("gmtoffset") or 0
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    by_day = {}
    for ts, close in zip(result.get("timestamp") or [], quote.get("close") or []):
        if ts is not None and close is not None:
            by_day[_day(ts + offset)] = float(close)
    price, at = meta.get("regularMarketPrice"), meta.get("regularMarketTime")
    if price is not None and at:
        by_day[_day(at + offset)] = float(price)
    return sorted(by_day.items())


def close_on(series, day):
    if not series:
        return None
    i = bisect.bisect_right(series, (day, float("inf")))
    return series[i - 1][1] if i else None


def close_after(series, day):
    if not series:
        return None
    i = bisect.bisect_right(series, (day, float("inf")))
    return series[i][1] if i < len(series) else None


def strip_holdings(portfolio):
    """The parts of the portfolio a snapshot needs to be re-valued later."""
    out = {}
    for kind in LIST_KEYS:
        out[kind] = [{k: v for k, v in h.items() if k != "log"} for h in portfolio.get(kind) or [] if isinstance(h, dict)]
    return out


def symbols_for(holdings):
    syms = {"USDINR=X"}
    for kind in PRICED:
        for h in holdings.get(kind) or []:
            s = yahoo_symbol(kind, h)
            if s:
                syms.add(s)
    curs = {h.get("currency") for k in ("bcash", "bank", "hand", "other") for h in holdings.get(k) or []}
    for code, sym, _key, _manual in EXTRA_FX:
        if code in curs:
            syms.add(sym)
    return syms


def _prices(rec):
    return {i["sym"]: i["price"] for i in (rec or {}).get("items") or [] if i.get("sym") and i.get("price") is not None}


def value_day(day, holdings, settings, closes, prev, own=None):
    """Value one date the way the dashboard does, using closes for that date.
    A symbol with no close falls back to this date's earlier record (own), then
    the previous date's record, then its next known close, then the fallback
    price, then cost, and is listed in missing."""
    prev_prices = {**_prices(prev), **_prices(own)}
    prev_fx = {**((prev or {}).get("fx") or {}), **((own or {}).get("fx") or {})}
    missing = set()

    def price_of(sym, manual):
        c = close_on(closes.get(sym), day)
        if c is not None:
            return c
        missing.add(sym)
        if sym in prev_prices:
            return prev_prices[sym]
        c = close_after(closes.get(sym), day)
        if c is not None:
            return c
        return _num(manual) if manual not in (None, "") else None

    def rate(sym, key, manual):
        r = price_of(sym, None)
        if r is None:
            r = prev_fx.get(key) or (_num(manual) or None) or FX_FALLBACK[sym]
        return r

    held = symbols_for(holdings)
    usdinr = rate("USDINR=X", "USDINR", settings.get("fxManual"))
    extra_rates = {}
    for code, sym, key, manual in EXTRA_FX:
        if sym in held:
            extra_rates[code] = (key, rate(sym, key, settings.get(manual)))

    def to_inr(v, cur):
        if cur == "USD":
            return v * usdinr
        hit = extra_rates.get(cur)
        if hit:
            return v / hit[1] * usdinr
        return v  # INR, and anything unknown, as the dashboard treats it

    items, cats = [], {k: 0.0 for k in LIST_KEYS}
    for kind in LIST_KEYS:
        for h in holdings.get(kind) or []:
            sym = yahoo_symbol(kind, h) if kind in PRICED else ""
            qty, price = _num(h.get("qty")), None
            if kind in ("us", "in"):
                cur = "USD" if kind == "us" else "INR"
                price = price_of(sym, h.get("manualPrice")) if sym else None
                value = qty * (price if price is not None else _num(h.get("avg")))
            elif kind == "other":
                cur = h.get("currency") or "INR"
                price = price_of(sym, h.get("price")) if sym else None
                value = qty * price if price is not None else _num(h.get("value"))
            else:
                cur = "INR" if kind == "pf" else (h.get("currency") or "INR")
                value = _num(h.get("balance"))
            inr = to_inr(value, cur)
            cats[kind] += inr
            item = {"k": kind, "id": h.get("id"), "cur": cur, "value": round(value, 4), "inr": round(inr, 2)}
            if sym:
                item.update(sym=sym, qty=qty, price=price)
            items.append(item)

    fx = {"USDINR": usdinr}
    for _code, (key, r) in extra_rates.items():
        fx[key] = r
    return {
        "date": day,
        "net": round(sum(cats.values()), 2),
        "cats": {k: round(v, 2) for k, v in cats.items()},
        "fx": fx,
        "missing": sorted(missing),
        "items": items,
        "holdings": holdings,
    }


class History:
    def __init__(self, path, backup_dir, load_portfolio, fetch_chart, log):
        self.path = path
        self.backup_dir = backup_dir
        self.load_portfolio = load_portfolio  # () -> portfolio dict or None
        self.fetch_chart = fetch_chart  # (sym, query, deadline) -> (result, None) | (None, (kind, msg))
        self.log = log
        self._lock = threading.Lock()
        self._kick = threading.Event()
        self._last_missing = None
        self._last_errors = None

    # ---------- storage ----------
    def read(self):
        records = []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for n, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        self.log(f"history: skipping unreadable line {n} of {self.path}")
                        continue
                    if isinstance(rec, dict) and isinstance(rec.get("date"), str):
                        records.append(rec)
        except FileNotFoundError:
            pass
        records.sort(key=lambda r: r["date"])
        return records

    def summary(self):
        with self._lock:
            records = self.read()
        keys = ("date", "net", "cats", "fx", "final", "filled")
        return [dict({k: r.get(k) for k in keys}, missing=len(r.get("missing") or [])) for r in records]

    def _write(self, records):
        if os.path.exists(self.path):
            os.makedirs(self.backup_dir, exist_ok=True)
            day = os.path.join(self.backup_dir, f"history-{datetime.date.today().isoformat()}.jsonl")
            if not os.path.exists(day):
                shutil.copy2(self.path, day)
                old = sorted(n for n in os.listdir(self.backup_dir) if BACKUP_RE.match(n))
                for name in old[:-KEEP_BACKUPS]:
                    try:
                        os.remove(os.path.join(self.backup_dir, name))
                    except OSError:
                        pass
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    # ---------- prices ----------
    def _fetch(self, symbols, start):
        p1 = int(datetime.datetime.combine(start, datetime.time.min).timestamp())
        p2 = int(time.time()) + 86400
        query = f"interval=1d&period1={p1}&period2={p2}"
        deadline = time.monotonic() + FETCH_DEADLINE
        symbols = sorted(symbols)
        with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
            results = list(pool.map(lambda s: self.fetch_chart(s, query, deadline), symbols))
        closes, errors = {}, {}
        for sym, (result, err) in zip(symbols, results):
            if result is not None:
                closes[sym] = daily_closes(result)
            else:
                errors[sym] = err
        return closes, errors

    # ---------- one pass ----------
    def run_pass(self, now=None):
        """Bring every open date up to date. Returns True when some price was
        missing, so the caller retries sooner."""
        portfolio = self.load_portfolio()
        if portfolio is None:
            return False
        now = now or time.time()
        today = datetime.datetime.fromtimestamp(now).date()
        today_s = today.isoformat()
        settings = portfolio.get("settings") or {}

        with self._lock:
            records = {r["date"]: r for r in self.read()}
            last = max(records) if records else None
            open_days = [d for d, r in records.items() if not r.get("final") and d < today_s]
            gap = []
            if last and last < today_s:
                d = max(datetime.date.fromisoformat(last) + datetime.timedelta(days=1),
                        today - datetime.timedelta(days=MAX_BACKFILL_DAYS))
                while d < today:
                    gap.append(d.isoformat())
                    d += datetime.timedelta(days=1)
            todo = set(open_days) | set(gap) | {today_s}

            # holdings per date: today's are current, a stored date keeps its own,
            # a missed date carries the record before it
            holdings, prev = {}, None
            for d in sorted(set(records) | todo):
                if d == today_s:
                    holdings[d] = strip_holdings(portfolio)
                elif d in records:
                    holdings[d] = records[d].get("holdings") or (prev and holdings.get(prev)) or {}
                else:
                    holdings[d] = holdings.get(prev) or {}
                prev = d

            symbols = set().union(*(symbols_for(holdings[d]) for d in todo))
            start = datetime.date.fromisoformat(min(todo)) - datetime.timedelta(days=LOOKBACK_DAYS)
            closes, errors = self._fetch(symbols, start)

            new_days, finalized, prev_rec = [], [], None
            for d in sorted(set(records) | todo):
                if d not in todo:
                    prev_rec = records[d]
                    continue
                rec = value_day(d, holdings[d], settings, closes, prev_rec, records.get(d))
                existed = d in records
                rec["filled"] = records[d].get("filled", False) if existed else d != today_s
                start_of_day = datetime.datetime.combine(datetime.date.fromisoformat(d), datetime.time.min).timestamp()
                settled = now >= start_of_day + SETTLE_HOURS * 3600
                stale = (today - datetime.date.fromisoformat(d)).days > GIVE_UP_DAYS
                rec["final"] = settled and (not rec["missing"] or stale)
                rec["at"] = int(now)
                if not existed:
                    new_days.append(d)
                if rec["final"]:
                    finalized.append(d)
                records[d] = rec
                prev_rec = rec

            self._write([records[d] for d in sorted(records)])

        self._report(new_days, finalized, today_s, records, errors, len(symbols))
        return any(records[d]["missing"] for d in todo if d in records)

    def _report(self, new_days, finalized, today_s, records, errors, n_symbols):
        filled = [d for d in new_days if d != today_s]
        if filled:
            span = filled[0] if len(filled) == 1 else f"{filled[0]} to {filled[-1]}"
            self.log(f"history: filled {len(filled)} missed day(s), {span}")
        if today_s in new_days:
            self.log(f"history: started {today_s}")
        if finalized:
            self.log(f"history: settled {', '.join(finalized)}")
        err_kinds = Counter(e[0] for e in errors.values())
        if errors and err_kinds != self._last_errors:
            sym, (kind, msg) = next(iter(sorted(errors.items())))
            kinds = ", ".join(f"{k} {n}" for k, n in err_kinds.most_common())
            self.log(f"history: price fetch failed for {len(errors)}/{n_symbols} symbol(s) ({kinds}); e.g. {sym}: {msg}")
        elif not errors and self._last_errors:
            self.log("history: price fetch recovered")
        self._last_errors = err_kinds if errors else None
        missing = sorted({s for r in records.values() if not r.get("final") for s in r.get("missing") or []})
        if missing != self._last_missing and not errors:
            if missing:
                self.log(f"history: no close yet for {', '.join(missing[:10])}{' …' if len(missing) > 10 else ''}; using last known")
        self._last_missing = missing

    # ---------- background loop ----------
    def kick(self):
        """Holdings changed: refresh today's record soon."""
        self._kick.set()

    def start(self):
        threading.Thread(target=self._loop, name="history", daemon=True).start()

    def _loop(self):
        last_pass, last_day, retry_soon = 0.0, None, False
        last_seen = time.time()
        while True:
            kicked = self._kick.wait(0 if last_day is None else CHECK_EVERY)
            if kicked:
                time.sleep(SETTLE_AFTER_SAVE)
                self._kick.clear()
            now = time.time()
            if now - last_seen > 3 * CHECK_EVERY:
                self.log(f"resumed after {(now - last_seen) / 60:.0f} min without running (sleep or pause)")
            last_seen = now
            today = datetime.date.today()
            wait = RETRY_EVERY if retry_soon else PASS_EVERY
            if not (kicked or today != last_day or now - last_pass >= wait):
                continue
            try:
                retry_soon = self.run_pass(now)
            except Exception as exc:  # keep the loop alive; the next pass tries again
                self.log(f"history: pass failed: {type(exc).__name__}: {exc}")
                retry_soon = True
            last_pass, last_day = time.time(), today
            last_seen = time.time()
