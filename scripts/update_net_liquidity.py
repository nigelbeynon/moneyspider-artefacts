#!/usr/bin/env python3
"""Refresh data/net-liquidity.json from primary public sources.

WALCL + TGA : Federal Reserve H.4.1, Table 5 (Wednesday level), via the release page.
RRP         : Federal Reserve Bank of New York reverse-repo operations API.
Fails loudly (non-zero exit) on missing/implausible data so the workflow alerts.
Only public-domain / US-government sources. No FRED.
"""
import json, re, sys, datetime as dt, html, urllib.request, pathlib

OUT = pathlib.Path(__file__).resolve().parent.parent / "data" / "net-liquidity.json"
DEBUG = pathlib.Path(__file__).resolve().parent.parent / "debug"
UA = {"User-Agent": "Mozilla/5.0 (compatible; moneyspider-data-bot; +https://www.moneyspider.net)"}
H41 = "https://www.federalreserve.gov/releases/h41/current/"
NYF = "https://markets.newyorkfed.org/api/rp/reverserepo/propositions/search.json?startDate={a}&endDate={b}"

def get(url, timeout=60):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")

def dump(name, text):
    DEBUG.mkdir(exist_ok=True); (DEBUG / name).write_text(text)

def die(msg):
    print("::error title=Net Liquidity refresh::" + str(msg).replace("%", "%25").replace("\r", "").replace("\n", "%0A")[:4000])
    print("ERROR:", msg, file=sys.stderr); sys.exit(1)

def num(s):
    s = re.sub(r"[^\d.\-]", "", s.replace(",", ""))
    return float(s) if s not in ("", "-", ".") else None

def h41_rows(page):
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S | re.I):
        cells = [html.unescape(re.sub(r"<[^>]+>", " ", c)).strip()
                 for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, re.S | re.I)]
        cells = [re.sub(r"\s+", " ", c) for c in cells]
        if cells: rows.append(cells)
    return rows

def fetch_fed():
    page = get(H41); dump("h41.html", page)
    text = html.unescape(re.sub(r"<[^>]+>", " ", page)); text = re.sub(r"\s+", " ", text)
    # Wednesday date: take the release date, Wednesday is the day before the Thursday release
    m = re.search(r"Release Date:?\s*([A-Z][a-z]+ \d{1,2}, \d{4})", text) or re.search(r"([A-Z][a-z]+ \d{1,2}, \d{4})", text)
    if not m: die("H.4.1: no release date found")
    rel = dt.datetime.strptime(m.group(1), "%B %d, %Y").date()
    wed = rel - dt.timedelta(days=(rel.weekday() - 2) % 7)  # most recent Wednesday on/before release
    rows = h41_rows(page)
    start = next((i for i, r in enumerate(rows) if any("Consolidated Statement of Condition of All Federal Reserve Banks" in c for c in r)), None)
    if start is None:
        # fall back: caption may sit outside rows; scan all rows
        start = 0
    def pick(label):
        for r in rows[start:]:
            if r and r[0].lower().startswith(label.lower()):
                vals = [num(c) for c in r[1:] if num(c) is not None]
                if vals: return vals[0]
        return None
    walcl = pick("Total assets")
    tga = pick("U.S. Treasury, General Account")
    if walcl is None or tga is None: die(f"H.4.1: could not parse WALCL={walcl} TGA={tga} (see debug/h41.html)")
    return wed, walcl / 1000.0, tga / 1000.0   # table is $ millions

def fetch_rrp(wed):
    a = (wed - dt.timedelta(days=10)).isoformat(); b = (wed + dt.timedelta(days=7)).isoformat()
    raw = get(NYF.format(a=a, b=b)); dump("nyfed_rrp.json", raw)
    ops = json.loads(raw).get("repo", {}).get("operations", [])
    by = {}
    for o in ops:
        if str(o.get("operationType", "")).lower().startswith("reverse") and str(o.get("term", "")).lower().startswith("overnight"):
            d = o.get("operationDate"); amt = o.get("totalAmtAccepted")
            if d is not None and amt is not None: by[d] = by.get(d, 0.0) + float(amt) / 1e9
    if not by: die("NY Fed: no overnight reverse-repo operations parsed. Raw start: " + raw[:1800])
    latest = max(by)
    return by.get(wed.isoformat()), by[latest], latest

def check(name, v, lo, hi):
    if v is None or not (lo <= v <= hi): die(f"{name}={v} outside plausible range {lo}..{hi}")

def main():
    d = json.loads(OUT.read_text())
    wed, walcl, tga = fetch_fed()
    rrp_wed, rrp_latest, rrp_date = fetch_rrp(wed)
    check("WALCL $B", walcl, 3000, 15000); check("TGA $B", tga, 0, 2500); check("RRP $B", rrp_latest, 0, 3000)
    proxy = round(walcl - tga, 3)
    net = round(walcl - tga - (rrp_wed if rrp_wed is not None else rrp_latest), 3)
    last = d["history"][-1][0]
    if wed.isoformat() < last: die(f"Fed Wednesday {wed} is older than stored {last}")
    row = [wed.isoformat(), proxy, net]
    if wed.isoformat() == last: d["history"][-1] = row
    else: d["history"].append(row)
    d["as_of"] = wed.isoformat()
    d["refreshed_at"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    d["latest"] = {"date": wed.isoformat(), "walcl": round(walcl, 1), "tga": round(tga, 1),
                   "rrp": round(rrp_latest, 1), "rrp_date": rrp_date, "proxy": proxy, "net": net}
    d["sources"] = {"walcl_tga": "Federal Reserve Board, H.4.1 (Table 5, Wednesday level)",
                    "rrp": "Federal Reserve Bank of New York, overnight reverse repo operations"}
    OUT.write_text(json.dumps(d, separators=(",", ":")))
    print(json.dumps(d["latest"]))

if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # network/parse errors -> loud, readable failure
        die(f"{type(e).__name__}: {e}")
