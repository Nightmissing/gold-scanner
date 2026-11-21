#!/usr/bin/env python3
"""
XAUUSD live scanner: MSNR levels + ICT sweep / MSS / FVG retest entries.

Same logic as the web scanner. Polls closed candles, prints setups and
sends Telegram alerts for new setups and status changes.

Sources
  mt5  : MetaTrader5 terminal (Windows). Real broker XAUUSD. pip install MetaTrader5
  yahoo: Yahoo Finance gold futures (GC=F). No install needed. Delayed and priced
         differently from spot, so treat levels as approximate.
  twelvedata: spot XAU/USD. No install needed. Free key: set TWELVEDATA_KEY.
         Free plan is rate limited, so use --interval 120 or more.
  demo : built-in sample data, to test the engine without any feed

Examples
  python xauusd_live_scanner.py --source demo --once
  python xauusd_live_scanner.py --source mt5 --tf M15 --min-grade B
  python xauusd_live_scanner.py --source yahoo --tf M15
  python xauusd_live_scanner.py --source twelvedata --tf M15 --interval 120

Telegram (optional): set TELEGRAM_TOKEN and TELEGRAM_CHAT_ID environment variables.
General information only, not financial advice. Test on a demo account first.
"""
import argparse, json, os, sys, time, urllib.parse, urllib.request
from datetime import datetime

# ---------------------------------------------------------------- engine

def atr(c, n):
    k = min(n, len(c) - 1)
    s = 0.0
    for i in range(len(c) - k, len(c)):
        s += max(c[i]["h"] - c[i]["l"], abs(c[i]["h"] - c[i - 1]["c"]), abs(c[i]["l"] - c[i - 1]["c"]))
    return s / k

def swings(c, n):
    H, L = [], []
    for i in range(n, len(c) - n):
        hi = lo = True
        for j in range(1, n + 1):
            if c[i]["h"] <= c[i - j]["h"] or c[i]["h"] <= c[i + j]["h"]:
                hi = False
            if c[i]["l"] >= c[i - j]["l"] or c[i]["l"] >= c[i + j]["l"]:
                lo = False
        if hi: H.append({"i": i, "p": c[i]["h"]})
        if lo: L.append({"i": i, "p": c[i]["l"]})
    return H, L

def bias(H, L):
    if len(H) < 2 or len(L) < 2:
        return 0
    a = H[-1]["p"] > H[-2]["p"]
    b = L[-1]["p"] > L[-2]["p"]
    return 1 if a and b else (-1 if (not a and not b) else 0)

def levels(c, H, L):
    out = []
    for h in H:
        if max([x["h"] for x in c[h["i"] + 1:]] or [-1e18]) < h["p"]:
            out.append({"p": h["p"], "k": "R", "i": h["i"]})
    for l in L:
        if min([x["l"] for x in c[l["i"] + 1:]] or [1e18]) > l["p"]:
            out.append({"p": l["p"], "k": "S", "i": l["i"]})
    return [x for x in out if x["i"] >= len(c) - 200]

def fvgs(c, A):
    out = []
    for i in range(2, len(c)):
        b = None
        if c[i]["l"] > c[i - 2]["h"]:
            b = {"dir": 1, "top": c[i]["l"], "bot": c[i - 2]["h"], "i": i}
        elif c[i]["h"] < c[i - 2]["l"]:
            b = {"dir": -1, "top": c[i - 2]["l"], "bot": c[i]["h"], "i": i}
        if not b or b["top"] - b["bot"] < 0.15 * A:
            continue
        ok = True
        for k in range(i + 1, len(c)):
            if (b["dir"] == 1 and c[k]["c"] < b["bot"]) or (b["dir"] == -1 and c[k]["c"] > b["top"]):
                ok = False
                break
        if ok:
            out.append(b)
    return out

def mirror(c):
    return [{"t": x.get("t"), "o": -x["o"], "h": -x["l"], "l": -x["h"], "c": -x["c"]} for x in c]

def status(c, s):
    k0 = -1
    for k in range(max(s["m"], s["z"]["i"]) + 1, len(c)):
        if c[k]["h"] >= s["entry"]:
            k0 = k
            break
        if c[k]["l"] <= s["tps"][0]:
            return {"t": "MISSED (TP1 reached without retest)", "live": False, "code": "missed"}
    if k0 < 0:
        return {"t": "WAITING FOR RETEST", "live": True, "code": "wait"}
    hit = 0
    for k in range(k0, len(c)):
        if c[k]["h"] >= s["sl"]:
            return {"t": ("TP%d hit, then stopped" % hit) if hit else "STOPPED OUT", "live": False, "code": "stop"}
        while hit < 3 and c[k]["l"] <= s["tps"][hit]:
            hit += 1
        if hit == 3:
            return {"t": "ALL TARGETS HIT", "live": False, "code": "tp3"}
    return {"t": ("TP%d hit, trade open" % hit) if hit else "ENTRY TRIGGERED", "live": True,
            "code": "tp%d" % hit if hit else "open"}

def detect_sell(c, A, n):
    H, L = swings(c, n)
    out = []
    for h in H:
        j = next((k for k in range(h["i"] + 1, len(c)) if c[k]["h"] > h["p"]), -1)
        if j < 0 or c[j]["c"] >= h["p"] or j < len(c) - 90:
            continue
        sh = max(c[k]["h"] for k in range(j, min(j + 2, len(c) - 1) + 1))
        ref = None
        for l in L:
            if h["i"] < l["i"] < j:
                ref = l
        if not ref:
            continue
        m = -1
        for k in range(j + 1, min(len(c), j + 26)):
            if c[k]["c"] > sh:
                break
            if c[k]["c"] < ref["p"]:
                m = k
                break
        if m < 0:
            continue
        z = None
        for f in fvgs(c[:min(len(c), m + 1)], A):
            if f["dir"] == -1 and f["i"] > j and (not z or f["top"] - f["bot"] > z["top"] - z["bot"]):
                z = {"top": f["top"], "bot": f["bot"], "i": f["i"], "t": "FVG"}
        if not z:
            for k in range(m, j - 1, -1):
                if c[k]["c"] > c[k]["o"]:
                    z = {"top": c[k]["h"], "bot": c[k]["l"], "i": m, "t": "OB"}
                    break
        if not z:
            continue
        e = (z["top"] + z["bot"]) / 2
        sl = sh + 0.25 * A
        risk = sl - e
        if risk <= 0:
            continue
        lows = sorted([l["p"] for l in L if l["p"] < e - 0.3 * A
                       and all(c[q]["l"] > l["p"] for q in range(l["i"] + 1, len(c)))], reverse=True)
        tps = []
        for p in lows:
            if len(tps) < 3 and (not tps or p < tps[-1] - 0.5 * A):
                tps.append(p)
        mu = 2
        while len(tps) < 3:
            p = e - risk * mu
            if not tps or p < tps[-1] - 0.3 * A:
                tps.append(p)
            mu += 1
        s = {"j": j, "m": m, "sweepP": h["p"], "ext": sh, "z": z, "entry": e, "sl": sl, "tps": tps}
        s["st"] = status(c, s)
        out.append(s)
    return out

def unmirror(s):
    return {"j": s["j"], "m": s["m"], "sweepP": -s["sweepP"], "ext": -s["ext"],
            "z": {"top": -s["z"]["bot"], "bot": -s["z"]["top"], "i": s["z"]["i"], "t": s["z"]["t"]},
            "entry": -s["entry"], "sl": -s["sl"], "tps": [-x for x in s["tps"]], "st": s["st"]}

def scan(c):
    A = atr(c, 14)
    H, L = swings(c, 3)
    b = bias(H, L)
    lv = levels(c, H, L)
    res = []
    for s in detect_sell(c, A, 3):
        s["dir"] = -1
        res.append(s)
    for s in detect_sell(mirror(c), A, 3):
        u = unmirror(s)
        u["dir"] = 1
        res.append(u)
    tail = c[-100:]
    hi, lo = max(x["h"] for x in tail), min(x["l"] for x in tail)
    mid = (hi + lo) / 2
    for s in res:
        s["rr"] = abs(s["tps"][0] - s["entry"]) / abs(s["sl"] - s["entry"])
        want = "R" if s["dir"] == -1 else "S"
        near = any(l["k"] == want and abs(l["p"] - s["entry"]) <= 1.5 * A for l in lv)
        s["checks"] = [
            ("Liquidity sweep", True), ("Structure shift", True),
            ("FVG entry zone" if s["z"]["t"] == "FVG" else "Order block zone", s["z"]["t"] == "FVG"),
            ("Trend aligned", b == s["dir"]),
            ("Premium" if s["dir"] == -1 else "Discount", s["entry"] > mid if s["dir"] == -1 else s["entry"] < mid),
            ("Fresh MSNR level near", near), ("RR >= 1:2", s["rr"] >= 2)]
        n = sum(1 for x in s["checks"] if x[1])
        s["grade"] = "A" if n >= 6 else "B" if n >= 5 else "C"
    res.sort(key=lambda s: (-int(s["st"]["live"]), -s["m"]))
    return {"A": A, "bias": b, "lv": lv, "sigs": res[:4], "price": c[-1]["c"]}

# ---------------------------------------------------------------- data

TF_MT5 = {"M1": "TIMEFRAME_M1", "M5": "TIMEFRAME_M5", "M15": "TIMEFRAME_M15", "M30": "TIMEFRAME_M30",
          "H1": "TIMEFRAME_H1", "H4": "TIMEFRAME_H4", "D1": "TIMEFRAME_D1"}
TF_YF = {"M1": "1m", "M5": "5m", "M15": "15m", "M30": "30m", "H1": "60m", "H4": "60m", "D1": "1d"}

def get_mt5(symbol, tf, bars):
    import MetaTrader5 as mt5
    if not mt5.initialize():
        sys.exit("MT5 init failed: %s" % (mt5.last_error(),))
    rates = mt5.copy_rates_from_pos(symbol, getattr(mt5, TF_MT5[tf]), 0, bars + 1)
    if rates is None or len(rates) < 50:
        sys.exit("No MT5 data for %s. Is the symbol in Market Watch (some brokers use XAUUSDm or GOLD)?" % symbol)
    return [{"t": int(r["time"]), "o": float(r["open"]), "h": float(r["high"]),
             "l": float(r["low"]), "c": float(r["close"])} for r in rates[:-1]]  # drop forming candle

def get_yahoo(symbol, tf, bars):
    if tf == "H4":
        sys.exit("Yahoo has no 4h candles. Use M15, M30, H1 or D1, or --source twelvedata.")
    rng = {"M1": "5d", "M5": "30d", "M15": "30d", "M30": "30d", "H1": "60d", "D1": "2y"}[tf]
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%s?interval=%s&range=%s" % (
        urllib.parse.quote(symbol), TF_YF[tf], rng)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    d = json.load(urllib.request.urlopen(req, timeout=20))["chart"]["result"][0]
    q = d["indicators"]["quote"][0]
    rows = []
    for t, o, h, l, cl in zip(d["timestamp"], q["open"], q["high"], q["low"], q["close"]):
        if None in (o, h, l, cl):
            continue
        rows.append({"t": int(t), "o": float(o), "h": float(h), "l": float(l), "c": float(cl)})
    if len(rows) < 50:
        sys.exit("Not enough Yahoo data returned.")
    return rows[-bars - 1:-1]  # drop forming candle

TF_TD = {"M1": "1min", "M5": "5min", "M15": "15min", "M30": "30min", "H1": "1h", "H4": "4h", "D1": "1day"}

def get_td(symbol, tf, bars):
    key = os.environ.get("TWELVEDATA_KEY")
    if not key:
        sys.exit("Set TWELVEDATA_KEY (free key from twelvedata.com).")
    q = urllib.parse.urlencode({"symbol": symbol, "interval": TF_TD[tf], "outputsize": bars + 1,
                                "order": "ASC", "apikey": key})
    d = json.load(urllib.request.urlopen("https://api.twelvedata.com/time_series?" + q, timeout=20))
    if "values" not in d:
        sys.exit("Twelve Data: %s" % d.get("message", d))
    rows = [{"t": v["datetime"], "o": float(v["open"]), "h": float(v["high"]),
             "l": float(v["low"]), "c": float(v["close"])} for v in d["values"]]
    if len(rows) < 50:
        sys.exit("Not enough Twelve Data candles returned.")
    return rows[:-1]  # drop forming candle

def get_demo():
    seed = [7]
    def r():
        seed[0] = seed[0] * 16807 % 2147483647
        return seed[0] / 2147483647
    pts = [(0, 3900), (30, 3790), (40, 3840), (65, 3690), (79, 3815), (84, 3715), (86, 3752)]
    ex = {80: (3815, 3848, 3805, 3812), 81: (3812, 3814, 3796, 3798),
          82: (3798, 3800, 3782, 3784), 83: (3784, 3790, 3655, 3676)}
    def base(i):
        for k in range(1, len(pts)):
            if i <= pts[k][0]:
                a, b = pts[k - 1], pts[k]
                return a[1] + (b[1] - a[1]) * (i - a[0]) / (b[0] - a[0])
        return pts[-1][1]
    c, prev = [], 3900.0
    for i in range(87):
        if i in ex:
            o, h, l, cl = ex[i]
        else:
            o = prev
            cl = base(i) + (r() - .5) * 5
            h = max(o, cl) + r() * 4
            l = min(o, cl) - r() * 4
        c.append({"t": 1_700_000_000 + i * 900, "o": o, "h": h, "l": l, "c": cl})
        prev = cl
    return c

# ---------------------------------------------------------------- output

def fmt(s, symbol, tf):
    side = "SELL" if s["dir"] == -1 else "BUY"
    z = s["z"]
    return ("%s %s %s  grade %s\nStatus: %s\nEntry %.2f (%s %.2f-%.2f)\nSL %.2f\nTP1 %.2f | TP2 %.2f | TP3 %.2f\n"
            "RR to TP1 1:%.1f\nChecks: %s") % (
        symbol, tf, side, s["grade"], s["st"]["t"], s["entry"], z["t"], z["bot"], z["top"], s["sl"],
        s["tps"][0], s["tps"][1], s["tps"][2], s["rr"],
        ", ".join(("+" if ok else "-") + name for name, ok in s["checks"]))

def telegram(text):
    tok, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        return
    try:
        data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
        urllib.request.urlopen("https://api.telegram.org/bot%s/sendMessage" % tok, data, timeout=15)
    except Exception as e:
        print("Telegram error:", e)

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=["mt5", "yahoo", "twelvedata", "demo"], default="mt5")
    ap.add_argument("--symbol", default=None, help="default XAUUSD (mt5), XAU/USD (twelvedata), GC=F (yahoo)")
    ap.add_argument("--tf", default="M15", choices=list(TF_MT5))
    ap.add_argument("--bars", type=int, default=300)
    ap.add_argument("--interval", type=int, default=60, help="seconds between scans")
    ap.add_argument("--min-grade", default="B", choices=["A", "B", "C"])
    ap.add_argument("--once", action="store_true", help="scan once and exit")
    ap.add_argument("--state", default=None, help="JSON file that remembers sent alerts between runs")
    a = ap.parse_args()
    symbol = a.symbol or {"yahoo": "GC=F", "twelvedata": "XAU/USD"}.get(a.source, "XAUUSD")
    order = {"A": 3, "B": 2, "C": 1}
    seen = {}  # setup key -> last status code
    if a.state and os.path.exists(a.state):
        seen = json.load(open(a.state))
    while True:
        if a.source == "demo":
            c = get_demo()
        elif a.source == "mt5":
            c = get_mt5(symbol, a.tf, a.bars)
        elif a.source == "yahoo":
            c = get_yahoo(symbol, a.tf, a.bars)
        else:
            c = get_td(symbol, a.tf, a.bars)
        R = scan(c)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print("[%s] %s %s  last close %.2f  trend %s  ATR %.2f  setups %d" % (
            now, symbol, a.tf, R["price"], {1: "bullish", -1: "bearish", 0: "ranging"}[R["bias"]], R["A"], len(R["sigs"])))
        for s in R["sigs"]:
            key = "%s|%s|%.1f" % (s["dir"], c[s["m"]]["t"], s["sweepP"])
            code = s["st"]["code"]
            if order[s["grade"]] < order[a.min_grade]:
                continue
            if key not in seen:
                seen[key] = code
                if s["st"]["live"]:
                    msg = "NEW SETUP\n" + fmt(s, symbol, a.tf)
                    print(msg + "\n")
                    telegram(msg)
            elif seen[key] != code:
                seen[key] = code
                msg = "UPDATE\n" + fmt(s, symbol, a.tf)
                print(msg + "\n")
                telegram(msg)
        if a.state:
            json.dump(seen, open(a.state, "w"))
        if a.once:
            break
        time.sleep(a.interval)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
