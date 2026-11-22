#!/usr/bin/env python3
"""
MOON SIGNAL BOT  (XAUUSD, MSNR + ICT)

Upgraded engine: higher-timeframe bias, sweep quality, displacement strength,
OTE + FVG/OB entry refinement, liquidity-pool sniper targets, killzones and a
0-100 Moon Score. Interactive Telegram bot with chart images.

Modes
  --once   scan, send alerts, answer waiting commands, exit (GitHub Actions)
  --poll   keep running: instant command replies + scan every --interval secs
  --cmd "/scan"   run one command and print the result (testing, no Telegram)

Sources: mt5 | yahoo | twelvedata | demo      (see --help)
Env: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, TWELVEDATA_KEY (twelvedata only)
Optional: pip install matplotlib   (chart images)

General information only, not financial advice. Test on a demo account first.
"""
import argparse, html, io, json, os, sys, time, urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta

# ================================================================ engine

def atr(c, n=14):
    k = min(n, len(c) - 1)
    return sum(max(c[i]["h"] - c[i]["l"], abs(c[i]["h"] - c[i - 1]["c"]), abs(c[i]["l"] - c[i - 1]["c"]))
               for i in range(len(c) - k, len(c))) / k

def ema(vals, n):
    k, e, out = 2.0 / (n + 1), vals[0], []
    for v in vals:
        e = v * k + e * (1 - k)
        out.append(e)
    return out

def swings(c, n):
    H, L = [], []
    for i in range(n, len(c) - n):
        hi = lo = True
        for j in range(1, n + 1):
            if c[i]["h"] <= c[i - j]["h"] or c[i]["h"] <= c[i + j]["h"]: hi = False
            if c[i]["l"] >= c[i - j]["l"] or c[i]["l"] >= c[i + j]["l"]: lo = False
        if hi: H.append({"i": i, "p": c[i]["h"]})
        if lo: L.append({"i": i, "p": c[i]["l"]})
    return H, L

def bias(H, L):
    if len(H) < 2 or len(L) < 2: return 0
    a, b = H[-1]["p"] > H[-2]["p"], L[-1]["p"] > L[-2]["p"]
    return 1 if a and b else (-1 if not a and not b else 0)

def levels(c, H, L):
    out = []
    for h in H:
        if max([x["h"] for x in c[h["i"] + 1:]] or [-1e18]) < h["p"]: out.append({"p": h["p"], "k": "R", "i": h["i"]})
    for l in L:
        if min([x["l"] for x in c[l["i"] + 1:]] or [1e18]) > l["p"]: out.append({"p": l["p"], "k": "S", "i": l["i"]})
    return [x for x in out if x["i"] >= len(c) - 200]

def fvgs(c, A):
    out = []
    for i in range(2, len(c)):
        b = None
        if c[i]["l"] > c[i - 2]["h"]: b = {"dir": 1, "top": c[i]["l"], "bot": c[i - 2]["h"], "i": i}
        elif c[i]["h"] < c[i - 2]["l"]: b = {"dir": -1, "top": c[i - 2]["l"], "bot": c[i]["h"], "i": i}
        if not b or b["top"] - b["bot"] < 0.15 * A: continue
        if all(not ((b["dir"] == 1 and c[k]["c"] < b["bot"]) or (b["dir"] == -1 and c[k]["c"] > b["top"]))
               for k in range(i + 1, len(c))):
            out.append(b)
    return out

def mirror(c):
    return [{"t": x.get("t"), "o": -x["o"], "h": -x["l"], "l": -x["h"], "c": -x["c"]} for x in c]

def status(c, s):
    for k in range(max(s["m"], s["z"]["i"]) + 1, len(c)):
        if c[k]["h"] >= s["entry"]: break
        if c[k]["l"] <= s["tps"][0]: return {"t": "Missed (TP1 reached before retest)", "live": False, "code": "missed"}
    else:
        if len(c) - 1 - s["m"] > 60: return {"t": "Expired (no retest in time)", "live": False, "code": "expired"}
        return {"t": "Waiting for retest", "live": True, "code": "wait"}
    hit = 0
    for k in range(k, len(c)):
        if c[k]["h"] >= s["sl"]:
            return {"t": ("TP%d hit, then stopped" % hit) if hit else "Stopped out", "live": False, "code": "stop"}
        while hit < 3 and c[k]["l"] <= s["tps"][hit]: hit += 1
        if hit == 3: return {"t": "All targets hit", "live": False, "code": "tp3"}
    return {"t": ("TP%d hit, trade open" % hit) if hit else "Entry triggered", "live": True,
            "code": "tp%d" % hit if hit else "open"}

def detect_sell(c, A, n):
    H, L = swings(c, n)
    out = []
    for h in H:
        j = next((k for k in range(h["i"] + 1, len(c)) if c[k]["h"] > h["p"]), -1)
        if j < 0 or c[j]["c"] >= h["p"] or j < len(c) - 90: continue
        sh = max(c[k]["h"] for k in range(j, min(j + 2, len(c) - 1) + 1))
        wick = sh - h["p"]
        if wick < 0.1 * A: continue
        ref = None
        for l in L:
            if h["i"] < l["i"] < j: ref = l
        if not ref: continue
        m = -1
        for k in range(j + 1, min(len(c), j + 26)):
            if c[k]["c"] > sh: break
            if c[k]["c"] < ref["p"]: m = k; break
        if m < 0: continue
        disp = max(c[k]["o"] - c[k]["c"] for k in range(j + 1, m + 1)) / A
        if disp < 0.6: continue                                  # weak shift: skip
        leg_lo = min(c[k]["l"] for k in range(j, m + 1))
        z = None
        for f in fvgs(c[:min(len(c), m + 1)], A):
            if f["dir"] == -1 and f["i"] > j and (not z or f["top"] - f["bot"] > z["top"] - z["bot"]):
                z = {"top": f["top"], "bot": f["bot"], "i": f["i"], "t": "FVG"}
        if not z:
            for k in range(m, j - 1, -1):
                if c[k]["c"] > c[k]["o"]: z = {"top": c[k]["h"], "bot": c[k]["l"], "i": m, "t": "OB"}; break
        if not z: continue
        rng = sh - leg_lo
        olo, ohi = leg_lo + 0.62 * rng, leg_lo + 0.79 * rng      # OTE band
        lo2, hi2 = max(olo, z["bot"]), min(ohi, z["top"])
        ote = lo2 <= hi2
        e = (lo2 + hi2) / 2 if ote else (z["top"] + z["bot"]) / 2
        sl = sh + 0.25 * A
        risk = sl - e
        if risk < 0.4 * A or risk > 3.5 * A: continue            # absurd stop distance: skip
        pools = sorted([l["p"] for l in L if l["p"] < e - 0.5 * A
                        and all(c[q]["l"] > l["p"] for q in range(l["i"] + 1, len(c)))], reverse=True)
        tps = []
        for p in pools:                                          # sniper: just before the liquidity
            tp = p + 0.1 * A
            if e - tp >= 1.2 * risk and (not tps or tp < tps[-1] - 0.5 * A) and len(tps) < 3: tps.append(tp)
        for mu in (1.5, 2.5, 4.0, 5.5):
            tp = e - risk * mu
            if len(tps) < 3 and (not tps or tp < tps[-1] - 0.8 * A): tps.append(tp)
        eq = sum(1 for x in H if x["i"] < j and x is not h and abs(x["p"] - h["p"]) <= 0.3 * A)
        s = {"j": j, "m": m, "sweepP": h["p"], "ext": sh, "wick": wick, "disp": disp, "eq": eq,
             "leg": (leg_lo, sh), "ote_band": (olo, ohi), "ote": ote, "z": z, "entry": e, "sl": sl, "tps": tps}
        s["st"] = status(c, s)
        out.append(s)
    return out

def unmirror(s):
    u = dict(s)
    for k in ("sweepP", "ext", "entry", "sl"): u[k] = -s[k]
    u["tps"] = [-x for x in s["tps"]]
    u["leg"] = (-s["leg"][1], -s["leg"][0])
    u["ote_band"] = (-s["ote_band"][1], -s["ote_band"][0])
    u["z"] = {"top": -s["z"]["bot"], "bot": -s["z"]["top"], "i": s["z"]["i"], "t": s["z"]["t"]}
    return u

def session(t):
    h = datetime.fromtimestamp(t, tz=timezone.utc).hour
    if 7 <= h < 10: return "London killzone", True
    if 12 <= h < 15: return "New York killzone", True
    if 15 <= h < 17: return "New York afternoon", False
    return "Off-session", False

def scan(c, hc):
    A = atr(c)
    H, L = swings(c, 3)
    b = bias(H, L)
    hb = bias(*swings(hc, 2)) if hc and len(hc) > 12 else 0
    lv = levels(c, H, L)
    e50 = ema([x["c"] for x in c], 50)[-1]
    price = c[-1]["c"]
    res = []
    for s in detect_sell(c, A, 3): s["dir"] = -1; res.append(s)
    for s in detect_sell(mirror(c), A, 3): u = unmirror(s); u["dir"] = 1; res.append(u)
    tail = c[-100:]
    mid = (max(x["h"] for x in tail) + min(x["l"] for x in tail)) / 2
    for s in res:
        d, e = s["dir"], s["entry"]
        s["rr"] = [abs(t - e) / abs(s["sl"] - e) for t in s["tps"]]
        near = any(l["k"] == ("R" if d == -1 else "S") and abs(l["p"] - e) <= 1.5 * A for l in lv)
        s["ses"] = session(c[s["m"]]["t"])
        s["checks"] = [
            ("HTF bias aligned", hb == d, 20), ("LTF structure aligned", b == d, 6),
            ("EMA50 side", price < e50 if d == -1 else price > e50, 4),
            ("Sweep wick quality", s["wick"] >= 0.25 * A, 6), ("Equal-level liquidity", s["eq"] >= 1, 4),
            ("Strong displacement", s["disp"] >= 1.0, 14), ("FVG entry zone", s["z"]["t"] == "FVG", 8),
            ("OTE overlap", s["ote"], 10), ("Killzone timing", s["ses"][1], 10),
            ("RR to TP1 at least 1.5", s["rr"][0] >= 1.5, 6),
            ("Premium / discount", e > mid if d == -1 else e < mid, 6), ("Fresh MSNR level near", near, 6)]
        s["score"] = sum(w for _, ok, w in s["checks"] if ok)
        s["grade"] = "A" if s["score"] >= 75 else "B" if s["score"] >= 60 else "C"
    res.sort(key=lambda s: (-int(s["st"]["live"]), -s["score"], -s["m"]))
    return {"A": A, "bias": b, "hbias": hb, "ema50": e50, "mid": mid, "lv": lv, "fg": fvgs(c, A),
            "sigs": res[:4], "price": price, "c": c, "hc": hc}

# ================================================================ data

TF_MT5 = {"M5": "TIMEFRAME_M5", "M15": "TIMEFRAME_M15", "M30": "TIMEFRAME_M30", "H1": "TIMEFRAME_H1",
          "H4": "TIMEFRAME_H4", "D1": "TIMEFRAME_D1"}
TF_YF = {"M5": "5m", "M15": "15m", "M30": "30m", "H1": "60m", "D1": "1d"}
TF_TD = {"M5": "5min", "M15": "15min", "M30": "30min", "H1": "1h", "H4": "4h", "D1": "1day"}
HTF = {"M5": "H1", "M15": "H1", "M30": "H4", "H1": "H4", "H4": "D1", "D1": "D1"}

def resample(c, k):
    return [{"t": g[0]["t"], "o": g[0]["o"], "h": max(x["h"] for x in g), "l": min(x["l"] for x in g), "c": g[-1]["c"]}
            for g in (c[i:i + k] for i in range(0, len(c) - k + 1, k))]

def load(source, symbol, tf, bars):
    if source == "demo": return get_demo()
    if source == "mt5":
        import MetaTrader5 as mt5
        if not mt5.initialize(): raise RuntimeError("MT5 init failed: %s" % (mt5.last_error(),))
        r = mt5.copy_rates_from_pos(symbol, getattr(mt5, TF_MT5[tf]), 0, bars + 1)
        if r is None or len(r) < 30: raise RuntimeError("No MT5 data for %s" % symbol)
        return [{"t": int(x["time"]), "o": float(x["open"]), "h": float(x["high"]), "l": float(x["low"]),
                 "c": float(x["close"])} for x in r[:-1]]
    if source == "yahoo":
        if tf not in TF_YF: raise RuntimeError("Yahoo has no %s candles" % tf)
        rng = {"M5": "30d", "M15": "30d", "M30": "30d", "H1": "60d", "D1": "2y"}[tf]
        url = "https://query1.finance.yahoo.com/v8/finance/chart/%s?interval=%s&range=%s" % (
            urllib.parse.quote(symbol), TF_YF[tf], rng)
        d = json.load(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=20))
        d = d["chart"]["result"][0]
        q = d["indicators"]["quote"][0]
        rows = [{"t": int(t), "o": float(o), "h": float(h), "l": float(l), "c": float(cl)}
                for t, o, h, l, cl in zip(d["timestamp"], q["open"], q["high"], q["low"], q["close"]) if None not in (o, h, l, cl)]
        return rows[-bars - 1:-1]
    key = os.environ.get("TWELVEDATA_KEY")
    if not key: raise RuntimeError("Set TWELVEDATA_KEY")
    q = urllib.parse.urlencode({"symbol": symbol, "interval": TF_TD[tf], "outputsize": bars + 1, "order": "ASC",
                                "timezone": "UTC", "apikey": key})
    d = json.load(urllib.request.urlopen("https://api.twelvedata.com/time_series?" + q, timeout=20))
    if "values" not in d: raise RuntimeError("Twelve Data: %s" % d.get("message", d))
    def ts(s):
        s = s[:19] if len(s) > 10 else s + " 00:00:00"
        return int(datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp())
    return [{"t": ts(v["datetime"]), "o": float(v["open"]), "h": float(v["high"]), "l": float(v["low"]),
             "c": float(v["close"])} for v in d["values"]][:-1]

def get_demo():
    seed = [7]
    def r():
        seed[0] = seed[0] * 16807 % 2147483647
        return seed[0] / 2147483647
    pts = [(0, 3900), (30, 3790), (40, 3840), (65, 3690), (79, 3815), (84, 3715), (86, 3752)]
    ex = {80: (3815, 3848, 3805, 3812), 81: (3812, 3814, 3796, 3798), 82: (3798, 3800, 3782, 3784),
          83: (3784, 3790, 3655, 3676)}
    def base(i):
        for k in range(1, len(pts)):
            if i <= pts[k][0]:
                a, b = pts[k - 1], pts[k]
                return a[1] + (b[1] - a[1]) * (i - a[0]) / (b[0] - a[0])
        return pts[-1][1]
    c, prev = [], 3900.0
    t0 = int(datetime(2026, 11, 20, 4, 0, tzinfo=timezone.utc).timestamp())
    for i in range(87):
        if i in ex: o, h, l, cl = ex[i]
        else:
            o, cl = prev, base(i) + (r() - .5) * 5
            h, l = max(o, cl) + r() * 4, min(o, cl) - r() * 4
        c.append({"t": t0 + i * 900, "o": o, "h": h, "l": l, "c": cl})
        prev = cl
    return c

# ================================================================ formatting

E = html.escape
f2 = lambda x: "%.2f" % x
SIDE = {-1: ("SELL", "🔻"), 1: ("BUY", "🔺")}
STATE_ICON = {"wait": "⏳", "open": "⚡", "tp1": "🎯", "tp2": "🎯", "tp3": "🏆", "stop": "🛑", "missed": "💨", "expired": "🌫"}
BARS = "━━━━━━━━━━━━━━━━━━"
BIAS_TXT = {1: "Bullish 🌕", -1: "Bearish 🌑", 0: "Ranging 🌗"}

def lots(cfg, entry, sl):
    if not cfg.get("acc"): return None
    d = abs(entry - sl)
    return max(0.0, int(cfg["acc"] * cfg["risk"] / 100.0 / (d * 100) * 100) / 100.0) if d else None

def gmt7(t): return (datetime.fromtimestamp(t, tz=timezone.utc) + timedelta(hours=7)).strftime("%H:%M")

def fmt_signal(s, R, cfg, sym, head="LUNAR SIGNAL"):
    name, ic = SIDE[s["dir"]]
    c, z, e, sl = R["c"], s["z"], s["entry"], s["sl"]
    mt = c[s["m"]]["t"]
    L = ["🌙 <b>%s</b> ✨  %s · %s" % (head, E(sym), cfg["tf"]), BARS,
         "%s <b>%s</b>  ·  Moon Score <b>%d/100</b>  (Grade %s)" % (ic, name, s["score"], s["grade"]),
         "%s %s" % (STATE_ICON.get(s["st"]["code"], "•"), E(s["st"]["t"])), "",
         "🎯 Entry  <code>%s</code>  (%s %s–%s%s)" % (f2(e), z["t"], f2(z["bot"]), f2(z["top"]), " + OTE" if s["ote"] else ""),
         "🛡 Stop   <code>%s</code>  (%s pts)" % (f2(sl), f2(abs(sl - e)))]
    for i, (tp, pct, moon) in enumerate(zip(s["tps"], (50, 30, 20), "🌒🌓🌕")):
        L.append("%s TP%d    <code>%s</code>  (1:%.1f) · close %d%%" % (moon, i + 1, f2(tp), s["rr"][i], pct))
    lt = lots(cfg, e, sl)
    if lt is not None:
        L.append("💼 Risk %g%% of $%g → %s" % (cfg["risk"], cfg["acc"], ("<b>%.2f lot</b>" % lt) if lt >= 0.01 else "<b>under 0.01 lot</b> (stop too wide for this risk)"))
    L += [BARS, "🧭 HTF %s: %s · LTF: %s" % (HTF.get(cfg["tf"], "H1"), BIAS_TXT[R["hbias"]], BIAS_TXT[R["bias"]]),
          "🔍 %s%s swept %s (wick %s) → shift %.1f× ATR → %s retest" % (
              "Equal levels " if s["eq"] else "", "highs" if s["dir"] == -1 else "lows", f2(s["sweepP"]), f2(s["wick"]), s["disp"], z["t"]),
          "⏰ %s · %s GMT+7" % (s["ses"][0], gmt7(mt))]
    ok = [n for n, k, _ in s["checks"] if k]
    miss = [n for n, k, _ in s["checks"] if not k]
    L.append("✅ " + E(", ".join(ok)))
    if miss: L.append("▫️ Missing: " + E(", ".join(miss)))
    L += [BARS, "⚠️ Invalid if price closes %s %s before entry" % ("above" if s["dir"] == -1 else "below", f2(sl)),
          "🌙 Plan: close 50% at TP1 and move SL to breakeven. Risk small, one setup at a time.",
          "<i>Rule-based scan, not advice.</i>"]
    return "\n".join(L)

def fmt_summary(R, cfg, sym):
    c, pr = R["c"], R["price"]
    rng_pos = (pr - min(x["l"] for x in c[-100:])) / max(1e-9, max(x["h"] for x in c[-100:]) - min(x["l"] for x in c[-100:])) * 100
    live = [s for s in R["sigs"] if s["st"]["live"]]
    return "\n".join(["🌙 <b>MOON SCAN</b> ✨  %s · %s" % (E(sym), cfg["tf"]), BARS,
        "💰 Price <code>%s</code>   ATR <code>%s</code>" % (f2(pr), f2(R["A"])),
        "🧭 HTF %s: %s" % (HTF.get(cfg["tf"], "H1"), BIAS_TXT[R["hbias"]]), "🧭 LTF: %s" % BIAS_TXT[R["bias"]],
        "📐 EMA50 %s · range position %d%% (%s)" % (f2(R["ema50"]), rng_pos, "premium" if rng_pos > 50 else "discount"),
        "🌌 Live setups: <b>%d</b>" % len(live), BARS])

def fmt_levels(R):
    pr = R["price"]
    up = sorted([l for l in R["lv"] if l["k"] == "R" and l["p"] > pr], key=lambda l: l["p"])[:4]
    dn = sorted([l for l in R["lv"] if l["k"] == "S" and l["p"] < pr], key=lambda l: -l["p"])[:4]
    L = ["🌙 <b>FRESH MSNR LEVELS</b>", BARS]
    L += ["🌑 Resistance <code>%s</code>  (+%s)" % (f2(l["p"]), f2(l["p"] - pr)) for l in reversed(up)] or ["🌑 none above"]
    L.append("💰 Price <code>%s</code>" % f2(pr))
    L += ["🌕 Support <code>%s</code>  (-%s)" % (f2(l["p"]), f2(pr - l["p"])) for l in dn] or ["🌕 none below"]
    L.append("<i>Fresh = not touched since formed. Liquidity usually rests there.</i>")
    return "\n".join(L)

# ================================================================ chart

def chart_png(R, sig, sym, tf):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import Rectangle, Circle
    except Exception:
        return None
    c = R["c"]
    N = min(110, len(c)); s0 = len(c) - N; v = c[s0:]
    BG, PANEL, SIL, GOLD, UP, DN = "#0b1026", "#10173a", "#cfd6ea", "#f2d27a", "#5fd3b5", "#ff7a90"
    fig, ax = plt.subplots(figsize=(9, 5.6), dpi=110)
    fig.patch.set_facecolor(BG); ax.set_facecolor(PANEL)
    hi, lo = max(x["h"] for x in v), min(x["l"] for x in v)
    if sig: hi, lo = max(hi, sig["sl"]), min(lo, sig["tps"][-1] if sig["dir"] == -1 else sig["sl"])
    if sig and sig["dir"] == 1: hi = max(hi, sig["tps"][-1])
    pad = (hi - lo) * 0.05; hi += pad; lo -= pad
    R_END = N + 12
    sp_ = ([sig["entry"], sig["sl"]] + list(sig["tps"])) if sig else []
    for b in R["fg"]:
        if b["i"] >= s0 and lo < b["top"] and b["bot"] < hi:
            x0 = b["i"] - 2 - s0
            ax.add_patch(Rectangle((x0, b["bot"]), R_END - x0, b["top"] - b["bot"], color=UP if b["dir"] == 1 else DN, alpha=0.16, lw=0))
    for l in R["lv"]:
        if lo < l["p"] < hi:
            ax.hlines(l["p"], max(0, l["i"] - s0), R_END, colors=SIL, linestyles=":", lw=0.8, alpha=0.6)
            if all(abs(l["p"] - q) > (hi - lo) * 0.035 for q in sp_):
                ax.text(R_END + .3, l["p"], f2(l["p"]), color=SIL, fontsize=7, va="center", alpha=.8)
    for i, k in enumerate(v):
        col = UP if k["c"] >= k["o"] else DN
        ax.vlines(i, k["l"], k["h"], color=col, lw=1)
        ax.add_patch(Rectangle((i - .32, min(k["o"], k["c"])), .64, max(abs(k["o"] - k["c"]), (hi - lo) / 900), color=col, lw=0))
    if sig:
        x0 = max(0, sig["m"] - s0)
        def line(p, col, txt, ls="-"):
            ax.hlines(p, x0, R_END, colors=col, linestyles=ls, lw=1.4)
            ax.text(R_END + .3, p, "%s %s" % (txt, f2(p)), color=col, fontsize=7.5, va="center", fontweight="bold")
        line(sig["entry"], GOLD, "ENTRY"); line(sig["sl"], DN, "SL", "--")
        for i, tp in enumerate(sig["tps"]): line(tp, UP, "TP%d" % (i + 1), "--")
        if sig["j"] >= s0:
            ax.annotate("SWEEP", (sig["j"] - s0, sig["ext"]), color=GOLD, fontsize=8, ha="center",
                        xytext=(0, 9 if sig["dir"] == -1 else -14), textcoords="offset points")
        if sig["m"] >= s0:
            ax.annotate("MSS", (sig["m"] - s0, c[sig["m"]]["c"]), color=GOLD, fontsize=8, ha="center",
                        xytext=(0, -14 if sig["dir"] == -1 else 9), textcoords="offset points")
    ax.set_xlim(-1, R_END + 9); ax.set_ylim(lo, hi)
    ax.tick_params(colors=SIL, labelsize=7); ax.set_xticks([])
    for sp in ax.spines.values(): sp.set_color("#2a3466")
    ax.yaxis.tick_left()
    t = "%s  %s   %s" % (sym, tf, ("%s Moon Score %d (%s)" % (SIDE[sig["dir"]][0], sig["score"], sig["grade"])) if sig else "no live setup")
    fig.text(0.115, 0.935, t, color=SIL, fontsize=12, fontweight="bold", va="center")
    fig.subplots_adjust(left=0.07, right=0.97, top=0.88, bottom=0.04)
    m = fig.add_axes([0.012, 0.88, 0.085, 0.11]); m.axis("off"); m.set_xlim(-1, 1); m.set_ylim(-1, 1); m.set_aspect("equal")
    m.add_patch(Circle((0, 0), .78, color=GOLD)); m.add_patch(Circle((.38, .14), .72, color=BG))
    m.scatter([.62, .85, .5], [.7, .15, -.6], s=[6, 3, 4], color=SIL)
    buf = io.BytesIO(); fig.savefig(buf, format="png", facecolor=BG); plt.close(fig)
    return buf.getvalue()

# ================================================================ telegram

KB = {"inline_keyboard": [[{"text": "🌙 Scan", "callback_data": "/scan"}, {"text": "📈 Chart", "callback_data": "/chart"}],
                          [{"text": "🌕 Levels", "callback_data": "/levels"}, {"text": "🧭 Bias", "callback_data": "/bias"}],
                          [{"text": "📡 Signals", "callback_data": "/signals"}, {"text": "⚙️ Settings", "callback_data": "/status"}]]}

def tg(method, params=None, timeout=35):
    tok = os.environ.get("TELEGRAM_TOKEN")
    if not tok: return None
    try:
        return json.load(urllib.request.urlopen("https://api.telegram.org/bot%s/%s" % (tok, method),
                                                urllib.parse.urlencode(params or {}).encode(), timeout=timeout))
    except Exception as e:
        print("Telegram error (%s):" % method, type(e).__name__)
        return None

def tg_photo(chat, png, caption):
    tok = os.environ.get("TELEGRAM_TOKEN")
    b = "moon%d" % int(time.time() * 1000)
    body = b"".join([("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (b, k, v)).encode()
                     for k, v in (("chat_id", chat), ("caption", caption), ("parse_mode", "HTML"))] +
                    [("--%s\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"moon.png\"\r\nContent-Type: image/png\r\n\r\n" % b).encode(),
                     png, ("\r\n--%s--\r\n" % b).encode()])
    try:
        urllib.request.urlopen(urllib.request.Request("https://api.telegram.org/bot%s/sendPhoto" % tok, body,
                               {"Content-Type": "multipart/form-data; boundary=" + b}), timeout=40)
    except Exception as e:
        print("Telegram error (sendPhoto):", type(e).__name__)

def deliver(items, chat):
    for it in items:
        if not os.environ.get("TELEGRAM_TOKEN"):
            print(it[1] if it[0] == "msg" else "[chart image, %d bytes] %s" % (len(it[1]), it[2])); print()
        elif it[0] == "msg":
            p = {"chat_id": chat, "text": it[1], "parse_mode": "HTML", "disable_web_page_preview": "true"}
            if it[2]: p["reply_markup"] = json.dumps(KB)
            tg("sendMessage", p)
        else:
            tg_photo(chat, it[1], it[2])

# ================================================================ state + commands

DEFAULT = {"offset": 0, "cfg": {"tf": "M15", "grade": "B", "acc": 0, "risk": 1.0, "alerts": True},
           "seen": {}, "hist": [], "last": {}}
CACHE = {}
ORDER = {"A": 3, "B": 2, "C": 1}

def load_state(path):
    try:
        st = json.load(open(path))
    except Exception:
        st = {}
    for k, v in DEFAULT.items():
        st.setdefault(k, json.loads(json.dumps(v)))
    for k, v in DEFAULT["cfg"].items(): st["cfg"].setdefault(k, v)
    return st

def save_state(st, path):
    if path: json.dump(st, open(path, "w"))

def get_R(st, args):
    cfg = st["cfg"]; key = (args.source, args.symbol, cfg["tf"])
    if CACHE.get("key") == key and time.time() - CACHE["t"] < 40: return CACHE["R"]
    c = load(args.source, args.symbol, cfg["tf"], args.bars)
    if len(c) < 60: raise RuntimeError("Not enough candles (%d)" % len(c))
    try:
        if args.source == "demo": hc = resample(c, 4)
        else:
            h = HTF.get(cfg["tf"], "H1")
            hc = load(args.source, args.symbol, "D1" if (args.source == "yahoo" and h == "H4") else h, 200)
    except Exception as e:
        print("HTF fetch failed:", e); hc = None
    R = scan(c, hc)
    CACHE.update(key=key, t=time.time(), R=R)
    st["last"] = {"t": int(time.time()), "price": R["price"]}
    return R

def cmd_help(st, args, a):
    return [("msg", "\n".join(["🌙 <b>MOON SIGNAL BOT</b> ✨", BARS, "Gold scanner · MSNR + ICT", "",
        "/scan  full scan + chart", "/signals  live setups", "/chart  chart with levels and gaps", "/levels  fresh MSNR levels",
        "/bias  trend and range read", "/price  last price", "/tf M15  change timeframe (M5 M15 M30 H1 H4)",
        "/grade A|B|C  minimum alert grade", "/risk 1000 1  account and risk %", "/lot 3793 3855  lot size for entry and stop",
        "/alerts on|off  pause alerts", "/history  recent signals", "/status  settings", "", "Tap a button below or type a command."]), True)]

def cmd_scan(st, args, a):
    R = get_R(st, args); out = [("msg", fmt_summary(R, st["cfg"], args.symbol), False)]
    top = next((s for s in R["sigs"] if s["st"]["live"]), None)
    png = chart_png(R, top, args.symbol, st["cfg"]["tf"])
    if png: out.append(("photo", png, "🌙 %s %s" % (E(args.symbol), st["cfg"]["tf"])))
    out.append(("msg", fmt_signal(top, R, st["cfg"], args.symbol, "TOP SETUP") if top else
                "🌑 No live setup right now. The scanner waits for a liquidity sweep, a structure shift and a clean retest zone.", True))
    return out

def cmd_signals(st, args, a):
    R = get_R(st, args); live = [s for s in R["sigs"] if s["st"]["live"]]
    if not live: return [("msg", "🌑 No live setups right now.", True)]
    return [("msg", fmt_signal(s, R, st["cfg"], args.symbol, "SETUP %d" % (i + 1)), i == len(live) - 1) for i, s in enumerate(live)]

def cmd_chart(st, args, a):
    R = get_R(st, args); top = next((s for s in R["sigs"] if s["st"]["live"]), None)
    png = chart_png(R, top, args.symbol, st["cfg"]["tf"])
    return [("photo", png, "🌙 %s %s%s" % (E(args.symbol), st["cfg"]["tf"], " · setup drawn" if top else " · levels and gaps"))] if png else \
           [("msg", "Charts need matplotlib: pip install matplotlib", True)]

def cmd_levels(st, args, a): return [("msg", fmt_levels(get_R(st, args)), True)]

def cmd_bias(st, args, a):
    R = get_R(st, args)
    return [("msg", fmt_summary(R, st["cfg"], args.symbol) + "\n" + "⏰ Session now: %s" % session(int(time.time()))[0], True)]

def cmd_price(st, args, a):
    R = get_R(st, args); return [("msg", "💰 <b>%s</b> <code>%s</code>  (last closed %s candle)" % (E(args.symbol), f2(R["price"]), st["cfg"]["tf"]), True)]

def cmd_tf(st, args, a):
    tf = (a[0] if a else "").upper()
    if tf not in HTF or (args.source == "yahoo" and tf not in TF_YF):
        return [("msg", "Use one of: M5 M15 M30 H1 H4 (Yahoo: no H4).", False)]
    st["cfg"]["tf"] = tf; return [("msg", "🌙 Timeframe set to <b>%s</b>. Higher timeframe: %s." % (tf, HTF[tf]), True)]

def cmd_grade(st, args, a):
    g = (a[0] if a else "").upper()
    if g not in ORDER: return [("msg", "Use /grade A, B or C.", False)]
    st["cfg"]["grade"] = g; return [("msg", "✨ Minimum alert grade: <b>%s</b>" % g, True)]

def cmd_risk(st, args, a):
    try: acc, rk = float(a[0]), float(a[1]) if len(a) > 1 else 1.0
    except Exception: return [("msg", "Use /risk 1000 1  (account in $, risk in %).", False)]
    if not (0 < rk <= 5): return [("msg", "Risk must be between 0 and 5 percent.", False)]
    st["cfg"].update(acc=acc, risk=rk); return [("msg", "💼 Account $%g · risk %g%% per trade. Lot sizes now show in signals." % (acc, rk), True)]

def cmd_lot(st, args, a):
    try: e, sl = float(a[0]), float(a[1])
    except Exception: return [("msg", "Use /lot 3793 3855 (entry then stop). Set /risk first.", False)]
    lt = lots(st["cfg"], e, sl)
    return [("msg", "Set your account first: /risk 1000 1" if lt is None else
             "💼 Entry %s · stop %s → %s (risk %g%% of $%g)" % (f2(e), f2(sl), ("<b>%.2f lot</b>" % lt) if lt >= 0.01 else "<b>under 0.01 lot</b>, too wide for this risk", st["cfg"]["risk"], st["cfg"]["acc"]), True)]

def cmd_alerts(st, args, a):
    on = (a[0].lower() if a else "") in ("on", "1", "yes")
    if not a or a[0].lower() not in ("on", "off"): return [("msg", "Use /alerts on or /alerts off.", False)]
    st["cfg"]["alerts"] = on; return [("msg", "🔔 Alerts %s." % ("on" if on else "paused"), True)]

def cmd_history(st, args, a):
    if not st["hist"]: return [("msg", "🌑 No signals logged yet.", True)]
    L = ["🌙 <b>RECENT SIGNALS</b>", BARS]
    for h in st["hist"][-8:][::-1]:
        L.append("%s %s %s · %s · entry %s · %s · %s" % (STATE_ICON.get(h["code"], "•"), SIDE[h["dir"]][0], h["grade"],
                 datetime.fromtimestamp(h["t"], tz=timezone.utc).strftime("%d %b %H:%M UTC"), f2(h["entry"]), "score %d" % h["score"], E(h["status"])))
    return [("msg", "\n".join(L), True)]

def cmd_status(st, args, a):
    c = st["cfg"]
    return [("msg", "\n".join(["⚙️ <b>SETTINGS</b>", BARS, "Source: %s · %s" % (args.source, E(args.symbol)), "Timeframe: %s (HTF %s)" % (c["tf"], HTF[c["tf"]]),
        "Min grade: %s" % c["grade"], "Alerts: %s" % ("on" if c["alerts"] else "paused"),
        "Account: %s" % (("$%g · risk %g%%" % (c["acc"], c["risk"])) if c["acc"] else "not set (/risk 1000 1)"),
        "Last price: %s" % (f2(st["last"]["price"]) if st["last"] else "-")]), True)]

CMDS = {"/start": cmd_help, "/help": cmd_help, "/scan": cmd_scan, "/signals": cmd_signals, "/chart": cmd_chart,
        "/levels": cmd_levels, "/bias": cmd_bias, "/price": cmd_price, "/tf": cmd_tf, "/grade": cmd_grade,
        "/risk": cmd_risk, "/lot": cmd_lot, "/alerts": cmd_alerts, "/history": cmd_history, "/status": cmd_status}

def run_command(st, args, text):
    p = text.strip().split()
    if not p: return []
    name = p[0].split("@")[0].lower()
    if name not in CMDS: return [("msg", "Unknown command. Try /help.", True)]
    try:
        return CMDS[name](st, args, p[1:])
    except Exception as e:
        return [("msg", "⚠️ %s" % E(str(e)[:200]), False)]

def handle_updates(st, args, chat, timeout):
    r = tg("getUpdates", {"offset": st["offset"], "timeout": timeout}, timeout + 10)
    for u in (r or {}).get("result", []):
        st["offset"] = u["update_id"] + 1
        cb = u.get("callback_query")
        msg = cb["message"] if cb else u.get("message")
        if not msg or str(msg["chat"]["id"]) != str(chat): continue
        if cb: tg("answerCallbackQuery", {"callback_query_id": cb["id"]})
        if not cb and time.time() - msg.get("date", time.time()) > 3600: continue
        text = cb["data"] if cb else msg.get("text", "")
        if text.startswith("/"): deliver(run_command(st, args, text), chat)

def scan_and_alert(st, args, chat):
    cfg = st["cfg"]
    if not cfg["alerts"]: return
    try: R = get_R(st, args)
    except Exception as e:
        print("Scan error:", e); return
    for s in R["sigs"]:
        if ORDER[s["grade"]] < ORDER[cfg["grade"]]: continue
        key = "%s|%s|%.1f" % (s["dir"], R["c"][s["m"]]["t"], s["sweepP"]); code = s["st"]["code"]
        if key not in st["seen"]:
            st["seen"][key] = code
            if s["st"]["live"]:
                st["hist"].append({"key": key, "t": R["c"][s["m"]]["t"], "dir": s["dir"], "grade": s["grade"], "score": s["score"],
                                   "entry": s["entry"], "status": s["st"]["t"], "code": code})
                png = chart_png(R, s, args.symbol, cfg["tf"]); out = []
                if png: out.append(("photo", png, "🌙 New lunar signal · %s %s" % (E(args.symbol), cfg["tf"])))
                out.append(("msg", fmt_signal(s, R, cfg, args.symbol, "NEW LUNAR SIGNAL"), True)); deliver(out, chat)
        elif st["seen"][key] != code:
            st["seen"][key] = code
            for h in st["hist"]:
                if h["key"] == key: h["status"], h["code"] = s["st"]["t"], code
            name, ic = SIDE[s["dir"]]
            deliver([("msg", "🌙 <b>UPDATE</b> · %s %s %s %s\n%s %s\nEntry %s · SL %s · TP1 %s" % (
                ic, name, E(args.symbol), cfg["tf"], STATE_ICON.get(code, "•"), E(s["st"]["t"]), f2(s["entry"]), f2(s["sl"]), f2(s["tps"][0])), True)], chat)
    st["hist"] = st["hist"][-40:]

# ================================================================ main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=["mt5", "yahoo", "twelvedata", "demo"], default="twelvedata")
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--bars", type=int, default=300)
    ap.add_argument("--interval", type=int, default=300, help="seconds between alert scans in --poll mode")
    ap.add_argument("--state", default="moon_state.json")
    ap.add_argument("--once", action="store_true"); ap.add_argument("--poll", action="store_true")
    ap.add_argument("--cmd", default=None, help='run one command, e.g. --cmd "/scan"')
    a = ap.parse_args()
    a.symbol = a.symbol or {"yahoo": "GC=F", "twelvedata": "XAU/USD"}.get(a.source, "XAUUSD")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    st = load_state(a.state)
    if a.cmd:
        deliver(run_command(st, a, a.cmd), chat); save_state(st, a.state); return
    if a.once:
        if chat: handle_updates(st, a, chat, 0)
        scan_and_alert(st, a, chat); save_state(st, a.state); return
    if not (os.environ.get("TELEGRAM_TOKEN") and chat): sys.exit("Set TELEGRAM_TOKEN and TELEGRAM_CHAT_ID for --poll.")
    deliver([("msg", "🌙 Moon Signal Bot is awake. Send /help.", True)], chat)
    nxt = 0
    while True:
        try:
            handle_updates(st, a, chat, 25)
            if time.time() >= nxt: scan_and_alert(st, a, chat); nxt = time.time() + a.interval
            save_state(st, a.state)
        except KeyboardInterrupt: break
        except Exception as e:
            print("Loop error:", type(e).__name__); time.sleep(5)

if __name__ == "__main__":
    main()
