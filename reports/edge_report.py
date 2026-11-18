#!/usr/bin/env python3
"""Edge Lab daily bot report: credit accounts, stake sizing, standings and the PDF.

Usage:
  python3 edge_report.py --picks botpicks.json --bots bots.json --games nba.json nfl.json \
      [--archive botarchive-2026-11.json ...] [--lessons botlessons.json] [--now 2026-11-18T16:45:00Z] --out out/

Inputs are the app's database documents saved as JSON (the doc's data object):
  feed/botpicks {picks:[...]}, feed/bots {bots, standings, reports}, feed/nba + feed/nfl {games:[...]},
  feed/botarchive-YYYY-MM {picks:[...]}, optional feed/botlessons {lessons:{bot: text}}.
Outputs in --out:
  botpicks.json  feed/botpicks data with stake fields added to new open picks (write it back)
  bots.json      feed/bots data with standings + accounts recomputed (write it back)
  report.json    what the PDF shows
  report.pdf     the Report A ("Night") PDF
  summary.txt    one line per bot

STAKING (credits; every bot starts with 10,000)
  * Picks made before STAKING_START were flat 1-unit bets: they count as 100 credits each.
  * New picks: fair = no-vig consensus for that side/line (from the feed odds; Quant: its own fair prob).
    Trust w per bot x market comes from the bot's track record in that market (graded picks, avg CLV,
    Brier vs. the market's Brier): w = 0.5 until 50 graded picks; then 0.25 / 0.5 / 0.75 / 1.0.
    p_used = fair + w * (prob - fair)   (shrinks the bot's opinion toward the market where it has not earned trust)
    stake% = quarter Kelly on p_used at the pick's price, x confidence multiplier (conf 4: x1.25, conf 5: x1.5),
    floor 0.5%, cap 3% -- or 5% for conf 4-5 picks once the bot's past conf 4-5 picks (30+) show ROI > 0 and CLV > 0.
    All of one run's new stakes together are capped at 30% of the balance (scaled down if over).
  * Balance = 10,000 + profit of all graded picks. Stakes are a % of the balance when the pick is staked.
"""
import argparse, json, math, os, statistics, sys, html
from datetime import datetime, timedelta, timezone

START_BALANCE = 10000
LEGACY_STAKE = 100
STAKING_START = "2026-11-18T12:00:00Z"
FLOOR, CAP, CAP_EARNED, RUN_CAP = 0.5, 3.0, 5.0, 30.0
CONF_MULT = {4: 1.25, 5: 1.5}
MARKETS = ["h2h", "spreads", "totals"]
MKT_LABEL = {"h2h": "Moneyline", "spreads": "Spread", "totals": "Total"}
MKT_SHORT = {"h2h": "ML", "spreads": "SPR", "totals": "TOT"}
SD = {("NBA", "spreads"): 12.0, ("NBA", "totals"): 17.5, ("NFL", "spreads"): 13.5, ("NFL", "totals"): 13.0}
BOT_ORDER = ["quant", "scout", "consensus", "analyst"]
ET = timezone(timedelta(hours=-5))  # Eastern (standard time; report times only)

# ---------- odds math ----------
def dec(a): a = float(a); return 1 + a / 100 if a > 0 else 1 + 100 / abs(a)
def imp(a): return 1 / dec(a)
def parse_t(s): return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
def r4(x): return None if x is None else round(x, 4)

def novig(a, b):
    ia, ib = imp(a), imp(b); return ia / (ia + ib)

def fair_for(pick, games_by_id):
    """No-vig consensus probability for the pick's side at the pick's line, from the feed odds."""
    g = games_by_id.get(pick.get("event"))
    if not g or not g.get("odds"): return None
    o, m, side = g["odds"], pick["market"], pick["side"]
    if m == "h2h":
        rows = [r for r in o.get("h2h", []) if r.get("a") is not None and r.get("h") is not None]
        if not rows: return None
        ph = statistics.mean(novig(r["h"], r["a"]) for r in rows)
        return ph if side == g["home"] else 1 - ph
    if m == "spreads":
        rows = [r for r in o.get("spreads", []) if r.get("a") is not None and r.get("h") is not None and r.get("line") is not None]
        if not rows or pick.get("point") is None: return None
        home_line = float(pick["point"]) if side == g["home"] else -float(pick["point"])
        def p_home_cover(L, rs): return statistics.mean(novig(r["h"], r["a"]) for r in rs)
        exact = [r for r in rows if float(r["line"]) == home_line]
        if exact: ph = p_home_cover(home_line, exact)
        else:  # shift from the most common line with a normal margin model
            common = statistics.mode([float(r["line"]) for r in rows]); rs = [r for r in rows if float(r["line"]) == common]
            sd = SD.get((g.get("league", pick.get("league")), "spreads"), 12.0); nd = statistics.NormalDist()
            p0 = min(max(p_home_cover(common, rs), 0.01), 0.99)
            mu = -common - sd * nd.inv_cdf(1 - p0)  # home margin mean, from P(margin > -common) = p0
            ph = 1 - nd.cdf((-home_line - mu) / sd)
        return ph if side == g["home"] else 1 - ph
    if m == "totals":
        rows = [r for r in o.get("totals", []) if r.get("o") is not None and r.get("u") is not None and r.get("line") is not None]
        if not rows or pick.get("point") is None: return None
        L = float(pick["point"])
        exact = [r for r in rows if float(r["line"]) == L]
        if exact: po = statistics.mean(novig(r["o"], r["u"]) for r in exact)
        else:
            common = statistics.mode([float(r["line"]) for r in rows]); rs = [r for r in rows if float(r["line"]) == common]
            sd = SD.get((g.get("league", pick.get("league")), "totals"), 17.5); nd = statistics.NormalDist()
            p0 = min(max(statistics.mean(novig(r["o"], r["u"]) for r in rs), 0.01), 0.99)
            mu = common + sd * nd.inv_cdf(p0)
            po = 1 - nd.cdf((L - mu) / sd)
        return po if side.lower() == "over" else 1 - po
    return None

# ---------- records ----------
def stake_of(p):
    if p.get("stake") is not None: return float(p["stake"])
    return LEGACY_STAKE

def credits_profit(p):
    st = p.get("status")
    if st == "won": return stake_of(p) * (dec(p["price"]) - 1)
    if st == "lost": return -stake_of(p)
    return 0.0

def graded(p): return p.get("status") in ("won", "lost", "push")

def stats(ps):
    g = [p for p in ps if graded(p)]
    w = sum(p["status"] == "won" for p in g); l = sum(p["status"] == "lost" for p in g); pu = sum(p["status"] == "push" for p in g)
    staked = sum(stake_of(p) for p in g); prof = sum(credits_profit(p) for p in g)
    clvs = [p["clv"] for p in g if p.get("clv") is not None]
    wl = [p for p in g if p["status"] in ("won", "lost") and p.get("prob") is not None]
    brier = statistics.mean((p["prob"] - (p["status"] == "won")) ** 2 for p in wl) if wl else None
    wlf = [p for p in wl if p.get("fair") is not None]
    mbrier = statistics.mean((p["fair"] - (p["status"] == "won")) ** 2 for p in wlf) if wlf else None
    bbrier = statistics.mean((p["prob"] - (p["status"] == "won")) ** 2 for p in wlf) if wlf else None
    evs = [p["ev"] for p in g if p.get("ev") is not None]
    return {"n": len(g), "w": w, "l": l, "p": pu,
            "units": r4(sum(float(p.get("profit") or 0) for p in g)),
            "staked": round(staked), "profitCredits": round(prof),
            "roi": r4(prof / staked) if staked else None,
            "avgEV": r4(statistics.mean(evs)) if evs else None,
            "avgCLV": r4(statistics.mean(clvs)) if clvs else None,
            "brier": r4(brier), "brierVsMarket": r4(bbrier - mbrier) if (bbrier is not None and mbrier is not None) else None}

def is_best(p):
    """A bot's 'best bets': research picks rated confidence 3+, Quant picks priced at or above fair (EV >= 0)."""
    if p.get("bot") == "quant": return (p.get("ev") or -1) >= 0
    return (p.get("conf") or 0) >= 3

def trust(s):
    """Weight on the bot's own opinion vs. the market, earned per market."""
    if s["n"] < 50 or s["avgCLV"] is None: return 0.5
    clv, bvm = s["avgCLV"], s["brierVsMarket"]
    if clv <= -0.02: return 0.25
    if clv >= 0.025 and bvm is not None and bvm <= 0: return 1.0
    if clv >= 0.01: return 0.75
    return 0.5

def earned_high_conf(ps):
    hc = [p for p in ps if graded(p) and (p.get("conf") or 0) >= 4]
    s = stats(hc)
    return s["n"] >= 30 and (s["roi"] or 0) > 0 and (s["avgCLV"] or 0) > 0

# ---------- main computation ----------
def run(a):
    now = parse_t(a.now) if a.now else datetime.now(timezone.utc)
    load = lambda f: json.load(open(f))
    cur = load(a.picks).get("picks", [])
    arch = []
    for f in a.archive or []: arch += load(f).get("picks", [])
    bots_doc = load(a.bots)
    games = []
    for f in a.games or []: games += load(f).get("games", [])
    gid = {g["id"]: g for g in games}
    lessons = (load(a.lessons).get("lessons", {}) if a.lessons and os.path.exists(a.lessons) else {})
    allp = arch + cur
    bot_ids = [b["id"] for b in bots_doc.get("bots", [])] or BOT_ORDER

    # accounts from graded picks
    acct = {}
    for b in bot_ids:
        mine = [p for p in allp if p.get("bot") == b]
        bal = START_BALANCE + sum(credits_profit(p) for p in mine if graded(p))
        acct[b] = {"start": START_BALANCE, "balance": round(bal)}

    # stake new open picks
    new_by_bot = {b: [] for b in bot_ids}
    for p in cur:
        if p.get("status") == "open" and p.get("stake") is None and str(p.get("pickedAt", "")) >= STAKING_START:
            new_by_bot.setdefault(p["bot"], []).append(p)
    for b, news in new_by_bot.items():
        if not news: continue
        hist = [p for p in allp if p.get("bot") == b]
        mstats = {m: stats([p for p in hist if p.get("market") == m]) for m in MARKETS}
        hc_ok = earned_high_conf(hist)
        bal = acct.get(b, {"balance": START_BALANCE})["balance"]
        for p in news:
            fair = p["prob"] if b == "quant" else fair_for(p, gid)
            if fair is None: fair = imp(p["price"]) / 1.045
            w = 1.0 if b == "quant" else trust(mstats[p["market"]])
            pu = fair + w * (float(p["prob"]) - fair)
            d = dec(p["price"]); k = (pu * d - 1) / (d - 1)
            pct = max(0.0, 25 * k)  # quarter Kelly, in percent
            conf = p.get("conf") or 3
            pct *= CONF_MULT.get(conf, 1.0)
            cap = CAP_EARNED if (conf >= 4 and hc_ok) else CAP
            p["fair"], p["pUsed"], p["trust"] = r4(fair), r4(pu), w
            p["stakePct"] = round(min(max(pct, FLOOR), cap), 2)
        tot = sum(p["stakePct"] for p in news)
        if tot > RUN_CAP:
            f = RUN_CAP / tot
            for p in news: p["stakePct"] = round(max(FLOOR, p["stakePct"] * f), 2)
        for p in news:
            p["stake"] = int(round(bal * p["stakePct"] / 100))
            p["stakedAt"] = now.strftime("%Y-%m-%dT%H:%M:%SZ"); p["balanceAtStake"] = bal

    # graded picks: credit profit for display
    for p in cur:
        if graded(p): p["profitCredits"] = round(credits_profit(p), 2)

    # standings
    standings = []
    for b in bot_ids:
        mine = [p for p in allp if p.get("bot") == b]
        s = stats(mine)
        s["bot"] = b
        s["byMarket"] = {m: stats([p for p in mine if p.get("market") == m]) for m in MARKETS}
        s["trust"] = {m: (1.0 if b == "quant" else trust(s["byMarket"][m])) for m in MARKETS}
        best = [p for p in mine if is_best(p)]
        s["best"] = {k: v for k, v in stats(best).items() if k in ("n", "w", "l", "p", "roi", "avgCLV", "staked", "profitCredits")}
        s["balance"] = acct[b]["balance"]
        s["openStake"] = int(sum(stake_of(p) for p in mine if p.get("status") == "open"))
        standings.append(s)
    standings.sort(key=lambda s: (s["roi"] if s["roi"] is not None else -9), reverse=True)
    bots_doc["standings"] = standings
    bots_doc["accounts"] = {b: acct[b] for b in bot_ids}
    bots_doc["updatedAt"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    # report content
    meta = {b["id"]: b for b in bots_doc.get("bots", [])}
    win_lo, win_hi = now - timedelta(hours=6), now + timedelta(hours=30)
    upcoming = [p for p in cur if p.get("status") == "open" and win_lo <= parse_t(p["start"]) <= win_hi]
    last_lo, last_hi = now - timedelta(hours=40), now - timedelta(hours=2)
    rep_bots = []
    for rank, s in enumerate(standings, 1):
        b = s["bot"]; mine = [p for p in upcoming if p.get("bot") == b]
        mine.sort(key=lambda p: (p["start"], p.get("event", ""), MARKETS.index(p["market"]) if p["market"] in MARKETS else 9))
        last = [p for p in cur if p.get("bot") == b and graded(p) and last_lo <= parse_t(p["start"]) <= last_hi]
        ls = stats(last) if last else None
        rep_bots.append({"rank": rank, "id": b, "name": meta.get(b, {}).get("name", b.title()),
                         "desc": meta.get(b, {}).get("desc", ""), "s": s,
                         "lesson": lessons.get(b) or auto_lesson(b, s),
                         "last": ls, "picks": mine})
    agree, near = agreements(upcoming, bot_ids)
    hist_agree = agree_record([p for p in allp if graded(p)], bot_ids)
    games_n = len({p.get("event") for p in upcoming})
    leagues = sorted({p.get("league") for p in upcoming})
    report = {"generatedAt": now.isoformat(), "date": now.astimezone(ET).strftime("%a %b %d, %Y").replace(" 0", " "),
              "games": games_n, "leagues": leagues, "picks": len(upcoming), "bots": rep_bots,
              "agree": agree, "near": near, "agreeRecord": hist_agree, "nbots": len(bot_ids)}

    os.makedirs(a.out, exist_ok=True)
    json.dump({"picks": cur, "updatedAt": bots_doc["updatedAt"]}, open(os.path.join(a.out, "botpicks.json"), "w"))
    json.dump(bots_doc, open(os.path.join(a.out, "bots.json"), "w"))
    json.dump(report, open(os.path.join(a.out, "report.json"), "w"), default=str)
    with open(os.path.join(a.out, "summary.txt"), "w") as f:
        for rb in rep_bots:
            s = rb["s"]
            f.write(f"#{rb['rank']} {rb['name']}: balance {s['balance']:,} | ROI {pct_s(s['roi'])} | {s['w']}-{s['l']}-{s['p']} | "
                    f"{len(rb['picks'])} picks, {sum(p.get('stake') or 0 for p in rb['picks']):,} staked\n")
    html_s = render_html(report)
    open(os.path.join(a.out, "report.html"), "w").write(html_s)
    if not a.no_pdf: to_pdf(os.path.join(a.out, "report.html"), os.path.join(a.out, "report.pdf"), report)
    print(open(os.path.join(a.out, "summary.txt")).read())

def agreements(ps, bot_ids):
    """Group picks by game x market; all bots on the same side = agree; one dissenter = near."""
    groups = {}
    for p in ps: groups.setdefault((p.get("event"), p["market"]), []).append(p)
    agree, near = [], []
    for (ev, m), g in groups.items():
        by = {p["bot"]: p for p in g}
        if len(by) < len(bot_ids): continue
        sides = {}
        for p in by.values(): sides.setdefault(p["side"], []).append(p)
        top = max(sides.values(), key=len)
        best = max(top, key=lambda p: dec(p["price"]))
        row = {"event": ev, "market": m, "league": best.get("league"), "away": best["away"], "home": best["home"],
               "start": best["start"], "side": best["side"], "point": best.get("point"), "price": best["price"],
               "book": best.get("book"), "avgProb": statistics.mean(float(p["prob"]) for p in top),
               "stakes": {p["bot"]: p.get("stakePct") for p in top},
               "dissent": [{"bot": p["bot"], "side": fmt_side(p)} for side, lst in sides.items() if lst is not top for p in lst]}
        if len(top) == len(bot_ids): agree.append(row)
        elif len(top) == len(bot_ids) - 1: near.append(row)
    k = lambda r: (r["start"], r["event"], MARKETS.index(r["market"]))
    return sorted(agree, key=k), sorted(near, key=k)

def agree_record(ps, bot_ids):
    """Season record of the side every bot agreed on (graded once, at the best price taken)."""
    groups = {}
    for p in ps: groups.setdefault((p.get("event"), p["market"]), []).append(p)
    w = l = pu = 0; prof = 0.0
    for g in groups.values():
        by = {p["bot"]: p for p in g}
        if len(by) < len(bot_ids) or len({p["side"] for p in by.values()}) != 1: continue
        best = max(by.values(), key=lambda p: dec(p["price"]))
        st = best["status"]
        if st == "won": w += 1; prof += dec(best["price"]) - 1
        elif st == "lost": l += 1; prof -= 1
        else: pu += 1
    n = w + l + pu
    return {"w": w, "l": l, "p": pu, "units": round(prof, 2), "roi": r4(prof / n) if n else None}

def auto_lesson(b, s):
    bm = s["byMarket"]; ok = {m: v for m, v in bm.items() if v["n"]}
    if not ok: return "No graded picks yet."
    best = max(ok, key=lambda m: ok[m]["roi"] or -9); worst = min(ok, key=lambda m: ok[m]["roi"] or 9)
    f = lambda m: f"{MKT_LABEL[m].lower()} {ok[m]['w']}-{ok[m]['l']} ({pct_s(ok[m]['roi'])} ROI)"
    tr = s.get("trust", {})
    trust_s = " · ".join(f"{MKT_SHORT[m]} {int(tr.get(m, .5) * 100)}%" for m in MARKETS)
    if b == "quant":
        return f"Best: {f(best)}. Weakest: {f(worst)}. Quant bets the market's own fair line, so its stakes only grow when a price beats fair."
    return f"Best: {f(best)}. Weakest: {f(worst)}. Trust in its own reads: {trust_s} (earned after 50 graded picks per market)."

def pct_s(x, d=1):
    if x is None: return "–"
    v = x * 100; s = f"{abs(v):.{d}f}%"
    return ("+" if v > 0 else ("−" if v < 0 else "")) + s

# ---------- rendering (Report A: Night) ----------
TEAM_COLORS = {
 "NBA": {"ATL":"#E03A3E","BOS":"#1E9E5A","BKN":"#B8B8B8","CHA":"#1CA5B8","CHI":"#CE1141","CLE":"#A3224D","DAL":"#2D7DD2","DEN":"#FEC524","DET":"#C8102E","GSW":"#FFC72C",
  "HOU":"#CE1141","IND":"#FDBB30","LAC":"#E0354B","LAL":"#8A5CD1","MEM":"#7D98CF","MIA":"#C8234A","MIL":"#3AA26A","MIN":"#3A86C8","NOP":"#C8A25E","NYK":"#F58426",
  "OKC":"#2E93D6","ORL":"#2E8FD8","PHI":"#2E7FD0","PHX":"#E56020","POR":"#E03A3E","SAC":"#8A5CC2","SAS":"#C4CED4","TOR":"#CE1141","UTA":"#7B5CC7","WAS":"#E31837"},
 "NFL": {"ARI":"#C8344F","ATL":"#C8293D","BAL":"#7B5CC7","BUF":"#2D6CDF","CAR":"#0085CA","CHI":"#E8642C","CIN":"#FB4F14","CLE":"#FF5A1F","DAL":"#4A7FD4","DEN":"#FB4F14",
  "DET":"#1C8FD6","GB":"#FFB612","HOU":"#C8293D","IND":"#3B78E0","JAC":"#00A3AD","JAX":"#00A3AD","KC":"#E31837","LV":"#A5ACAF","LAC":"#2EA0E6","LA":"#FFD100","LAR":"#FFD100",
  "MIA":"#00A6B0","MIN":"#7A52C7","NE":"#D21F3C","NO":"#D3BC8D","NYG":"#3B6FE0","NYJ":"#2E9E6A","PHI":"#2E9C9C","PIT":"#FFB612","SF":"#D42A2A","SEA":"#69BE28",
  "TB":"#E0262B","TEN":"#4B92DB","WAS":"#B3364A"}}
TEAM_COLORS2 = {
 "NBA": {"ATL":"#C1D32F","BOS":"#BA9653","BKN":"#7A7A7A","CHA":"#8A5CC2","CHI":"#D9D9D9","CLE":"#FDBB30","DAL":"#B8C4CA","DEN":"#4A7FD4","DET":"#3B6FE0","GSW":"#2E6FD0",
  "HOU":"#C4CED4","IND":"#4A6FD0","LAC":"#2E7FD0","LAL":"#FDB927","MEM":"#F5B112","MIA":"#F9A01B","MIL":"#EEE1C6","MIN":"#78BE20","NOP":"#D33A4F","NYK":"#2E6FD0",
  "OKC":"#EF3B24","ORL":"#C4CED4","PHI":"#ED174C","PHX":"#8A5CC2","POR":"#B8B8B8","SAC":"#C4CED4","SAS":"#7A7A7A","TOR":"#C0C0C0","UTA":"#DDDDDD","WAS":"#4A7FD4"},
 "NFL": {"ARI":"#FFB612","ATL":"#A5ACAF","BAL":"#C9A227","BUF":"#E31837","CAR":"#A5ACAF","CHI":"#4A6FD0","CIN":"#D0D0D0","CLE":"#B5794A","DAL":"#B0B7BC","DEN":"#4A6FD0",
  "DET":"#B0B7BC","GB":"#2E9E5A","HOU":"#4A6FD0","IND":"#D0D0D0","JAC":"#D7A22A","JAX":"#D7A22A","KC":"#FFB81C","LV":"#6E7276","LAC":"#FFC20E","LA":"#3B6FE0","LAR":"#3B6FE0",
  "MIA":"#FC4C02","MIN":"#FFC62F","NE":"#4A6FD0","NO":"#E0E0E0","NYG":"#C8293D","NYJ":"#E0E0E0","PHI":"#A5ACAF","PIT":"#D0D0D0","SF":"#B3995D","SEA":"#4A6FD0",
  "TB":"#FF8200","TEN":"#C8293D","WAS":"#FFB612"}}

def _rgb(c): n = int(c[1:], 16); return (n >> 16 & 255, n >> 8 & 255, n & 255)
def _dist(c1, c2):
    a, b = _rgb(c1), _rgb(c2); r = (a[0] + b[0]) / 2; dr, dg, db = a[0] - b[0], a[1] - b[1], a[2] - b[2]
    return math.sqrt((2 + r / 256) * dr * dr + 4 * dg * dg + (2 + (255 - r) / 256) * db * db)
def game_colors(lg, away, home):
    t1, t2 = TEAM_COLORS.get(lg, {}), TEAM_COLORS2.get(lg, {})
    a1, h1, a2, h2 = t1.get(away, "#8a99ad"), t1.get(home, "#8a99ad"), t2.get(away), t2.get(home)
    opts = [o for o in [(a1, h1), (a1, h2), (a2, h1), (a2, h2)] if o[0] and o[1]]
    for o in opts:
        if _dist(*o) >= 150: return o
    return max(opts, key=lambda o: _dist(*o))

E = html.escape
ACC = "#f5b400"; GREEN = "#22c55e"; RED = "#ef4444"; MUTED = "#8a99ad"
def fmt_price(p): p = int(p); return f"+{p}" if p > 0 else f"−{abs(p)}"
def fmt_side(p):
    s = p["side"]
    if p["market"] == "spreads" and p.get("point") is not None:
        v = float(p["point"]); v = int(v) if v == int(v) else v
        return f"{s} {'+' if v > 0 else ('−' if v < 0 else '')}{abs(v)}"
    if p["market"] == "totals" and p.get("point") is not None:
        v = float(p["point"]); v = int(v) if v == int(v) else v
        return f"{s} {v}"
    return s
def bar(label, ms):
    n = ms["w"] + ms["l"]
    p = 100 * ms["w"] / n if n else 0
    col = GREEN if p >= 52.4 else RED
    return (f'<div class="bar"><span class="lab">{label}</span><div class="track"><div class="fill" style="width:{p:.0f}%;background:{col}"></div>'
            f'<div class="tick"></div></div><span class="val">{p:.0f}%</span></div>')
def best_line(rb):
    b = rb["s"].get("best") or {}; n = (b.get("w") or 0) + (b.get("l") or 0)
    lab = "EV+ PICKS" if rb["id"] == "quant" else "CONF 3+"
    if not n: return f'<div class="best mono tiny muted">{lab}: no graded picks yet</div>'
    pc = 100 * b["w"] / n; col = GREEN if pc >= 52.4 else RED
    return (f'<div class="best mono tiny"><span class="muted">{lab}</span> <b style="color:{col}">{pc:.0f}%</b> '
            f'<span class="muted">{b["w"]}-{b["l"]} · ROI {pct_s(b.get("roi"))}</span></div>')

def tstart(p): return parse_t(p["start"]).astimezone(ET).strftime("%a %-I:%M %p")

def render_html(r):
    bots = r["bots"]
    cards = ""
    for rb in bots:
        s = rb["s"]; roi = s["roi"]
        cards += f'''<div class="card{' lead' if rb['rank'] == 1 else ''}">
<div class="row"><b>#{rb['rank']} {E(rb['name'])}</b><span class="mono muted">{s['w']}-{s['l']}{'-' + str(s['p']) if s['p'] else ''}</span></div>
<div class="desc">{E(rb['desc'])}</div>
<div class="row base"><span class="roi" style="color:{GREEN if (roi or 0) > 0 else RED}">{pct_s(roi)}</span><span class="mono muted small">ROI</span></div>
<div class="mono bal">{s['balance']:,} ◎ <span class="muted small">of {START_BALANCE:,}</span></div>
{bar('ML', s['byMarket']['h2h'])}{bar('SPR', s['byMarket']['spreads'])}{bar('TOT', s['byMarket']['totals'])}
{best_line(rb)}
</div>'''
    sections = ""
    for rb in bots:
        s = rb["s"]; picks = rb["picks"]
        risk = sum(p.get("stake") or 0 for p in picks)
        byg = {}
        for p in picks: byg.setdefault(p.get("event"), []).append(p)
        rows = ""
        for ev, ps in byg.items():
            p0 = ps[0]; lg = p0.get("league", "NBA"); ca, ch = game_colors(lg, p0["away"], p0["home"])
            cells = ""
            for m in MARKETS:
                q = next((x for x in ps if x["market"] == m), None)
                if not q: cells += '<div class="cell empty"></div>'; continue
                evc = GREEN if (q.get("ev") or 0) > 0.0005 else (RED if (q.get("ev") or 0) < -0.0005 else MUTED)
                sp = q.get("stakePct"); stake = q.get("stake")
                boosted = (q.get("conf") or 0) >= 4
                conf = f'<span class="conf">conf {q["conf"]}/5</span>' if q.get("conf") else ""
                stake_s = (f'<span class="stk{" boost" if boosted else ""}">{sp:g}%</span> · {stake:,} ◎' if sp is not None else f"{stake or LEGACY_STAKE:,} ◎ flat")
                why = E(q.get("why") or "")
                cells += f'''<div class="cell">
<div class="row mono tiny muted"><span>{MKT_LABEL[m].upper()}</span><span>{E(q.get('book', ''))}</span></div>
<div class="row base"><b class="side">{E(fmt_side(q))}</b><b class="mono price">{fmt_price(q['price'])}</b></div>
<div class="mono tiny line2">Win {100 * float(q['prob']):.1f}% · EV <span style="color:{evc}">{pct_s(q.get('ev'))}</span></div>
<div class="mono tiny line2">Stake {stake_s} {conf}</div>
{f'<div class="why">{why}</div>' if why and rb['id'] != 'quant' else ''}
</div>'''
            rows += f'''<div class="game">
<div class="teams"><div><i style="background:{ca}"></i>{E(p0['away'])}</div><div><i style="background:{ch}"></i>{E(p0['home'])}</div>
<div class="mono tiny muted">{tstart(p0)} ET · {E(lg)}</div></div>{cells}</div>'''
        if not rows: rows = '<div class="none">No picks in this window.</div>'
        last = rb["last"]
        last_s = (f" · LAST SLATE {last['w']}-{last['l']}{'-' + str(last['p']) if last['p'] else ''}, "
                  f"{'+' if last['profitCredits'] >= 0 else '−'}{abs(last['profitCredits']):,} ◎") if last else ""
        quant_note = ('<div class="note"><b>Why these picks:</b> Quant takes the side whose best available price is closest to (or above) '
                      'the market\'s no-vig fair line. Thin or negative edges get the 0.5% floor.</div>') if rb["id"] == "quant" else ""
        sections += f'''<section class="bot">
<div class="bhead"><div><span class="bname">#{rb['rank']} {E(rb['name'])}</span><span class="bdesc">{E(rb['desc'])}</span></div>
<span class="mono tiny muted">BALANCE {s['balance']:,} ◎ · {len(picks)} PICKS · {risk:,} ◎ AT RISK ({100 * risk / max(s['balance'], 1):.1f}%){last_s}</span></div>
{rows}
<div class="notes">{quant_note}<div class="note"><b>Learning:</b> {E(rb['lesson'])}</div></div>
</section>'''
    nb = r.get("nbots", 4); names = {rb["id"]: rb["name"] for rb in bots}
    def agree_rows(lst, near=False):
        out = ""
        for a in lst:
            ca, ch = game_colors(a.get("league") or "NBA", a["away"], a["home"])
            chips = " ".join(f'<span class="chip">{E(names.get(b, b))} {("" if v is None else f"{v:g}%")}</span>' for b, v in a["stakes"].items())
            dis = ""
            if near and a["dissent"]:
                d = a["dissent"][0]; dis = f'<span class="dis">{E(names.get(d["bot"], d["bot"]))} took {E(d["side"])}</span>'
            out += f'''<div class="arow"><div class="ateams"><i style="background:{ca}"></i>{E(a['away'])} @ <i style="background:{ch}"></i>{E(a['home'])}
<div class="mono tiny muted" style="font-weight:400">{tstart(a)} ET</div></div><div class="mono tiny muted">{MKT_LABEL[a['market']].upper()}</div>
<div><b class="side">{E(fmt_side(a))}</b> <b class="mono price">{fmt_price(a['price'])}</b> <span class="mono tiny muted">{E(a.get('book') or '')}</span></div>
<div class="mono tiny">avg win {100 * a['avgProb']:.1f}%</div><div class="chips">{chips}{dis}</div></div>'''
        return out
    ar = r.get("agreeRecord") or {}
    rec_s = (f"Season record when all {nb} bots agree: {ar.get('w', 0)}-{ar.get('l', 0)}{'-' + str(ar['p']) if ar.get('p') else ''}"
             f" ({pct_s(ar.get('roi'))} ROI flat)") if (ar.get('w', 0) + ar.get('l', 0)) else "No graded all-agree picks yet."
    agree_html = f'''<section class="agree"><div class="bhead"><div><span class="bname">All Bots Agree</span>
<span class="bdesc">every bot took the same side · {len(r.get('agree', []))} picks</span></div><span class="mono tiny muted">{E(rec_s.upper())}</span></div>
{agree_rows(r.get('agree', [])) or '<div class="none">No market where all four bots agree today.</div>'}
{f'<div class="subhead">3 OF {nb} AGREE · one dissenter</div>' + agree_rows(r.get('near', []), True) if r.get('near') else ''}
</section>'''
    lg = " + ".join(r["leagues"]) or "NBA/NFL"
    return f'''<!doctype html><html><head><meta charset="utf-8"><title>Edge Lab Daily Bot Report</title><style>
@page {{ size: Letter; margin: 0; }}
html {{ background:#0b0f14; -webkit-print-color-adjust:exact; print-color-adjust:exact; }}
body {{ margin:0; background:#0b0f14; color:#e8eef6; font-family:Inter, sans-serif; font-size:11px; }}
main {{ padding:0.42in 0.45in 0.45in; box-decoration-break:clone; -webkit-box-decoration-break:clone; }}
.mono {{ font-variant-numeric: tabular-nums; letter-spacing:.2px; }}
.muted {{ color:{MUTED}; }} .small {{ font-size:10px; }} .tiny {{ font-size:9px; }}
.row {{ display:flex; justify-content:space-between; align-items:center; gap:6px; }} .base {{ align-items:baseline; }}
header {{ display:flex; justify-content:space-between; align-items:flex-end; border-bottom:1px solid #1d2733; padding-bottom:12px; margin-bottom:14px; }}
.logo {{ width:36px; height:36px; border-radius:9px; background:{ACC}; color:#111; display:flex; align-items:center; justify-content:center; font-weight:900; font-size:18px; }}
h1 {{ margin:0; font-size:22px; font-weight:900; letter-spacing:-.3px; }}
.cards {{ display:flex; gap:9px; margin-bottom:16px; break-inside:avoid; }}
.card {{ flex:1; background:#121820; border:1px solid #243041; border-radius:10px; padding:10px; display:flex; flex-direction:column; gap:6px; }}
.card.lead {{ border-color:{ACC}; }} .card b {{ font-size:13px; }}
.desc {{ font-size:9px; color:{MUTED}; margin-top:-4px; min-height:22px; }}
.roi {{ font-size:21px; font-weight:800; }} .bal {{ font-size:11px; margin-top:-3px; }}
.bar {{ display:grid; grid-template-columns:24px 1fr 30px; align-items:center; gap:5px; font-size:9px; }}
.bar .lab {{ color:{MUTED}; }} .bar .val {{ text-align:right; font-weight:700; }}
.track {{ position:relative; height:6px; background:#1d2733; border-radius:3px; }} .fill {{ height:6px; border-radius:3px; }}
.tick {{ position:absolute; left:52.4%; top:-3px; width:1px; height:12px; background:{MUTED}; }}
.best {{ border-top:1px solid #1d2733; padding-top:5px; margin-top:1px; }}
.legend {{ font-size:9px; color:{MUTED}; margin:-8px 0 14px; }}
section.bot {{ margin-bottom:16px; }}
.bhead {{ display:flex; justify-content:space-between; align-items:baseline; border-left:3px solid {ACC}; padding-left:9px; margin-bottom:8px; break-after:avoid; }}
.bname {{ font-weight:900; font-size:16px; }} .bdesc {{ font-size:10px; color:{MUTED}; margin-left:8px; }}
.game {{ display:grid; grid-template-columns:104px 1fr 1fr 1fr; gap:7px; margin-bottom:7px; break-inside:avoid; }}
.teams {{ display:flex; flex-direction:column; justify-content:center; gap:3px; font-weight:800; font-size:13px; }}
.teams i {{ display:inline-block; width:4px; height:12px; border-radius:2px; margin-right:6px; vertical-align:-1px; }}
.cell {{ background:#0e141b; border:1px solid #1d2733; border-radius:8px; padding:7px 9px; display:flex; flex-direction:column; gap:3px; }}
.cell.empty {{ background:transparent; border-style:dashed; }}
.side {{ font-size:12px; }} .price {{ font-size:12px; color:{ACC}; }} .line2 {{ color:#b6c2d1; }}
.stk {{ font-weight:700; color:#e8eef6; }} .stk.boost {{ color:{ACC}; }}
.conf {{ margin-left:4px; padding:0 4px; border:1px solid #2a3646; border-radius:4px; color:#b6c2d1; }}
.why {{ font-size:9.5px; line-height:1.35; color:#c9d3df; border-top:1px solid #1d2733; padding-top:4px; margin-top:1px; }}
.notes {{ display:flex; flex-direction:column; gap:6px; break-inside:avoid; }}
.note {{ font-size:10px; color:#b6c2d1; background:#121820; border-radius:8px; padding:8px 11px; line-height:1.45; }}
.note b {{ color:{ACC}; }} .none {{ color:{MUTED}; font-size:11px; padding:6px 0 10px; }}
.agree {{ margin-bottom:18px; }}
.arow {{ display:grid; grid-template-columns:175px 58px 175px 72px 1fr; gap:8px; align-items:center; background:#0e141b; border:1px solid #1d2733;
  border-radius:8px; padding:7px 10px; margin-bottom:5px; break-inside:avoid; }}
.ateams {{ font-weight:800; font-size:12px; }} .ateams i {{ display:inline-block; width:4px; height:11px; border-radius:2px; margin:0 5px 0 0; vertical-align:-1px; }}
.chips {{ display:flex; flex-wrap:wrap; gap:4px; justify-content:flex-end; }}
.chip {{ font-size:9px; border:1px solid #2a3646; border-radius:999px; padding:1px 7px; color:#c9d3df; }}
.dis {{ font-size:9px; color:{RED}; padding:1px 4px; }}
.subhead {{ font-size:9px; color:{MUTED}; letter-spacing:.6px; margin:10px 0 5px; }}
.foot {{ font-size:9px; color:{MUTED}; border-top:1px solid #1d2733; padding-top:8px; line-height:1.5; }}
</style></head><body><main>
<header><div class="row" style="gap:11px"><div class="logo">E</div><div><h1>Daily Bot Report</h1>
<div class="mono small muted">EDGE LAB · {E(r['date'].upper())} · {r['games']} {lg} GAMES · {r['picks']} PICKS</div></div></div>
<div class="mono tiny muted" style="text-align:right;line-height:1.6;white-space:nowrap">SEASON TO DATE · RANKED BY ROI<br>BARS = WIN % · TICK = 52.4% BREAKEVEN AT −110</div></header>
<div class="cards">{cards}</div>
{agree_html}
<div class="legend">Every bot started with {START_BALANCE:,} ◎. Picks before Nov 18 were flat bets of {LEGACY_STAKE} ◎. New stakes: quarter-Kelly on the bot's win chance (pulled toward the market's fair line until the bot earns trust), 0.5% floor, 3% cap; gold stakes are boosted by confidence 4–5.</div>
{sections}
<div class="foot">Play money only. Win % = the bot's own estimate; EV = win % × payout − 1 at the listed price; ◎ = Edge Lab credits.</div>
</main></body></html>'''

def to_pdf(html_path, pdf_path, r):
    from playwright.sync_api import sync_playwright
    foot = ('<div style="width:100%;font-family:Inter,sans-serif;font-size:8px;color:#8a99ad;background:#0b0f14;'
            '-webkit-print-color-adjust:exact;padding:0 0.45in;display:flex;justify-content:space-between;margin:0 0 -1px">'
            f'<span>Edge Lab · Daily Bot Report · {E(r["date"])}</span><span>Page <span class="pageNumber"></span> of <span class="totalPages"></span></span></div>')
    with sync_playwright() as p:
        exe = None
        for c in ["/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser"]:
            if os.path.exists(c): exe = c; break
        b = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
        pg = b.new_page()
        pg.goto("file://" + os.path.abspath(html_path)); pg.wait_for_timeout(300)
        pg.pdf(path=pdf_path, format="Letter", print_background=True, prefer_css_page_size=True)
        b.close()

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--picks", required=True); ap.add_argument("--bots", required=True)
    ap.add_argument("--games", nargs="*"); ap.add_argument("--archive", nargs="*")
    ap.add_argument("--lessons"); ap.add_argument("--now"); ap.add_argument("--out", default="out")
    ap.add_argument("--no-pdf", action="store_true")
    run(ap.parse_args())
