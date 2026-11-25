#!/usr/bin/env python3
"""Refresh data/mind-the-gap.json from primary public sources (no FRED).

USD/JPY + broad dollar : Federal Reserve Board H.10 (daily rows, release page + summary page)
US 10-year             : US Treasury daily par yield curve CSV (same series H.15 republishes)
Japan 10-year          : Japan Ministry of Finance JGB interest rates (jgbcme.csv)
Fails loudly (non-zero exit) on missing/implausible data. History is seeded once from the
block's own arrays; each run only rewrites the current-year entries and the "latest" block.
"""
import csv, io, json, re, sys, datetime as dt, html, urllib.request, pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "mind-the-gap.json"
DEBUG = ROOT / "debug"
UA = {"User-Agent": "Mozilla/5.0 (compatible; moneyspider-data-bot; +https://www.moneyspider.net)"}
H10 = "https://www.federalreserve.gov/releases/h10/current/"
H10_BROAD = "https://www.federalreserve.gov/releases/h10/summary/jrxwtfb_nb.htm"
TSY = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/"
       "{y}/all?type=daily_treasury_yield_curve&field_tdr_date_value={y}&page&_format=csv")
MOF = "https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/jgbcme.csv"
MON = {m: i for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split(), 1)}

def get(url, timeout=60):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")

def dump(name, text):
    DEBUG.mkdir(exist_ok=True); (DEBUG / name).write_text(text)

def die(msg):
    print("::error title=Mind the Gap refresh::" + str(msg).replace("%", "%25").replace("\r", "").replace("\n", "%0A")[:4000])
    print("ERROR:", msg, file=sys.stderr); sys.exit(1)

def text_of(page):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", page)))

def rows_of(page):
    out = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S | re.I):
        cells = [re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", c))).strip()
                 for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, re.S | re.I)]
        if cells: out.append(cells)
    return out

def fnum(s):
    try: return float(s.replace(",", ""))
    except Exception: return None

# ---- Fed H.10 current release: daily USD/JPY and broad index for the latest week --------------
def parse_h10_current(page, year):
    """Return ({date: yen_per_usd}, {date: broad}) from the 'current' release table."""
    rows = rows_of(page)
    hdr = next((r for r in rows if r and r[0].upper().startswith("COUNTRY")), None)
    if not hdr: die("H.10: header row not found")
    dates = []
    for c in hdr[2:]:
        m = re.match(r"([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2})$", c.strip())
        dates.append(dt.date(year, MON[m.group(1).upper()], int(m.group(2))) if m else None)
    jpy, broad = {}, {}
    for r in rows:
        if len(r) > 2 and r[0].upper() == "JAPAN" and r[1].upper() == "YEN":
            for d, v in zip(dates, r[2:]):
                if d and fnum(v) is not None: jpy[d] = fnum(v)
        if len(r) > 2 and "BROAD" in r[0].upper() and "JAN06" in r[1].upper().replace(" ", ""):
            for d, v in zip(dates, r[2:]):
                if d and fnum(v) is not None: broad[d] = fnum(v)
    return jpy, broad

def release_year(text):
    m = re.search(r"Release Date:?\s*([A-Z][a-z]+ \d{1,2}, (\d{4}))", text) or re.search(r"([A-Z][a-z]+ \d{1,2}, (\d{4}))", text)
    if not m: die("H.10: release date not found")
    return int(m.group(2))

# ---- Fed H.10 summary page: full daily broad-dollar history (DD-MON-YY, value) -----------------
def parse_broad_daily(page):
    out = {}
    for r in rows_of(page):
        if len(r) >= 2:
            m = re.fullmatch(r"(\d{2})-([A-Za-z]{3})-(\d{2})", r[0].strip())
            v = fnum(r[1])
            if m and v is not None:
                out[dt.date(2000 + int(m.group(3)), MON[m.group(2).upper()], int(m.group(1)))] = v
    return out

# ---- Treasury par CSV -------------------------------------------------------------------------
def parse_treasury(raw):
    rd = csv.reader(io.StringIO(raw)); hdr = next(rd)
    if "10 Yr" not in hdr: die("Treasury CSV: '10 Yr' column missing: " + ",".join(hdr))
    i = hdr.index("10 Yr"); out = {}
    for r in rd:
        if len(r) > i and r[0] and fnum(r[i]) is not None:
            m, d, y = r[0].split("/"); out[dt.date(int(y), int(m), int(d))] = fnum(r[i])
    return out

# ---- Japan MoF CSV ----------------------------------------------------------------------------
def parse_mof(raw):
    rd = csv.reader(io.StringIO(raw)); out = {}; i = None
    for r in rd:
        if r and r[0].strip() == "Date": i = r.index("10Y"); continue
        if i is not None and r and re.fullmatch(r"\d{4}/\d{1,2}/\d{1,2}", r[0].strip()) and len(r) > i and fnum(r[i]) is not None:
            y, m, d = map(int, r[0].split("/")); out[dt.date(y, m, d)] = fnum(r[i])
    if i is None: die("MoF CSV: header with 10Y not found")
    return out

def on_or_before(series, day):
    ds = [d for d in series if d <= day]
    return (max(ds), series[max(ds)]) if ds else (None, None)

def check(name, v, lo, hi):
    if v is None or not (lo <= v <= hi): die(f"{name}={v} outside plausible range {lo}..{hi}")

def main():
    seed = json.loads(OUT.read_text())
    page = get(H10); dump("h10.html", page)
    yr = release_year(text_of(page))
    jpy, broad_wk = parse_h10_current(page, yr)
    if not jpy: die("H.10: no USD/JPY values parsed (see debug/h10.html)")
    bpage = get(H10_BROAD); dump("h10_broad.html", bpage)
    broad = parse_broad_daily(bpage); broad.update(broad_wk)
    if len(broad) < 3000: die(f"H.10 broad history too short: {len(broad)} rows")
    us = parse_treasury(get(TSY.format(y=yr))); jp = parse_mof(get(MOF))
    if not us or not jp: die("Treasury or MoF series empty")
    last = {"usdjpy": max(jpy), "us10y": max(us), "jgb10y": max(jp), "dxy": max(broad)}
    common = min(last["usdjpy"], last["us10y"], last["jgb10y"])
    vj = on_or_before(jpy, common); vu = on_or_before(us, common); vg = on_or_before(jp, common)
    check("USD/JPY", vj[1], 50, 300); check("US10", vu[1], 0, 15); check("JGB10", vg[1], -1, 10)
    if common.isoformat() < seed["latest"]["date"]: die(f"common date {common} older than stored {seed['latest']['date']}")
    # annual arrays: current-year entry = value at the common date
    ys = str(common.year)
    def setlast(years, vals, v):
        if years[-1] == ys: vals[-1] = v
        elif int(years[-1]) < common.year: years.append(ys); vals.append(v)
        else: die("array year ahead of data")
    setlast(seed["jpy_years"], seed["jpy"], round(vj[1], 2))
    setlast(seed["yield_years"], seed["us10"], round(vu[1], 2))
    if len(seed["jgb10"]) < len(seed["yield_years"]): seed["jgb10"].append(round(vg[1], 3))
    else: seed["jgb10"][-1] = round(vg[1], 3)
    ytd = [v for d, v in broad.items() if d.year == common.year]
    dxy_avg = round(sum(ytd) / len(ytd), 2)
    if seed["dxy_years"][-1] == ys: seed["dxy"][-1] = dxy_avg
    else: seed["dxy_years"].append(ys); seed["dxy"].append(dxy_avg)
    dd, dv = last["dxy"], broad[last["dxy"]]
    ya_d, ya_v = on_or_before(broad, dt.date(dd.year - 1, dd.month, min(dd.day, 28)))
    hi_d = max(broad, key=broad.get)
    check("Broad dollar", dv, 70, 200)
    seed["latest"] = {"date": common.isoformat(), "usdjpy": round(vj[1], 2), "us10y": round(vu[1], 2),
                      "jgb10y": round(vg[1], 3), "spread": round(vu[1] - vg[1], 2)}
    seed["series_dates"] = {k: v.isoformat() for k, v in last.items()}
    seed["dxy_latest"] = {"date": dd.isoformat(), "value": round(dv, 1), "year_ago_date": ya_d.isoformat(),
                          "year_ago": round(ya_v, 1), "high_date": hi_d.isoformat(), "high": round(broad[hi_d], 1),
                          "ytd_through": dd.isoformat()}
    seed["as_of"] = common.isoformat()
    seed["refreshed_at"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    OUT.write_text(json.dumps(seed, separators=(",", ":")))
    print(json.dumps({"latest": seed["latest"], "series_dates": seed["series_dates"], "dxy": seed["dxy_latest"]}))

if __name__ == "__main__":
    try: main()
    except SystemExit: raise
    except Exception as e: die(f"{type(e).__name__}: {e}")
