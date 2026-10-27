#!/usr/bin/env python3
"""
ncaam_edge_data_pull.py  (NCAAM Edge Model companion) v1.3

Writes ncaam_model_data.json for the NCAAM Edge Model HTML:
  teams     BartTorvik ratings (adjoe, adjde, adjt, barthag, ...)
  schedule  ESPN scoreboard games (neutral-site flag, venue, tournament notes, scores)
  odds_raw  The Odds API FanDuel + DraftKings lines, unmodified (the HTML parses them)
  api       what each source answered: Odds API credits used / remaining / last request cost, ESPN game counts, ratings status

Keys: keys.txt next to this script (ODDS_API_KEY=...) wins over environment variables.
Name matching is NOT done here – the HTML does it, visibly, so nothing is silently zeroed.

Ratings come from the first source in this chain that works (each step is logged, and the result is saved in the file):
  1) RATINGS_URL  (keys.txt / environment / GitHub secret): a link to a CSV or JSON of team ratings
  2) a file next to this script for the CURRENT season: ratings_manual*.csv|json or torvik_manual*.csv (year taken from the file name).
     This is also where the file pushed from your own computer lands.
  3) BartTorvik live download: browser-style headers, a cookie warm-up, three address variants, two tries each (often blocked from GitHub: HTTP 403)
  4) ratings built from ESPN scores (adjusted points for and against, no tempo data, needs ~5 games per team, so it only works from about December)
  5) an OLDER-season file next to this script (real Torvik numbers, flagged as prior season)
  6) the ratings already in the last ncaam_model_data.json (carried forward, with their original date)
Whatever happens to ratings, the schedule and lines are still saved - the HTML keeps the last good ratings.
Files need a header row with team, AdjOE, AdjDE, AdjT (Torvik: adjoe/adjde/adjt; KenPom: AdjO/AdjD/AdjT or AdjOE/AdjDE/AdjTempo).
"""
import concurrent.futures, csv, datetime as dt, glob, io, json, math, os, re, sys, time

try:
    import requests
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests"])
    import requests

VERSION = "1.3"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "ncaam_model_data.json")
UA = {"User-Agent": "Mozilla/5.0 (compatible; EdgeReportPull/1.0)"}
ODDS_URL = ("https://api.the-odds-api.com/v4/sports/basketball_ncaab/odds"
            "?apiKey={key}&regions=us&markets=h2h,spreads,totals&oddsFormat=american"
            "&bookmakers=fanduel,draftkings")
ESPN_URL = ("https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/"
            "scoreboard?dates={d}&groups=50&limit=400")
TORVIK_URLS = ["https://barttorvik.com/{year}_team_results.csv",
               "https://www.barttorvik.com/{year}_team_results.csv",
               "https://barttorvik.com/trank.php?year={year}&csv=1"]
BROWSER = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
           "Accept": "text/csv,text/plain,application/json;q=0.9,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.9", "Referer": "https://barttorvik.com/"}
AVG_POSS = 67.5          # used only by the ESPN-built ratings (no tempo data there)
ESPN_MIN_GAMES = 5


def today():
    return dt.date.today()


def log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- keys
def clean_key(v):
    v = (v or "").strip().strip("'\"").strip()
    if v.lower().startswith("bearer "):
        v = v[7:].strip()
    return v.lstrip("+").strip()


def load_keys():
    keys = {}
    for name in ("ODDS_API_KEY", "RATINGS_URL"):
        keys[name] = clean_key(os.environ.get(name, ""))
    p = os.path.join(HERE, "keys.txt")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                if k.strip() in keys and clean_key(v):
                    keys[k.strip()] = clean_key(v)
    for k, v in keys.items():
        if k == "RATINGS_URL":
            log("  RATINGS_URL: " + (v[:60] + ("…" if len(v) > 60 else "") if v else "not set (optional)"))
        else:
            log(f"  key {k}: " + (f"len {len(v)} ({v[:2]}…{v[-2:]})" if v else "MISSING"))
    return keys


# ---------------------------------------------------------------- season
def season_year(today_=None):
    t = today_ or today()
    return t.year + 1 if t.month >= 8 else t.year   # 2026-27 season -> 2027


# ---------------------------------------------------------------- torvik
def to_f(x):
    try:
        return float(str(x).replace("%", "").replace(",", "").replace("+", ""))
    except (TypeError, ValueError):
        return None


def hkey(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


RATING_KEYS = dict(
    team=["team", "teamname", "name", "school"], conf=["conf", "confshort", "conference"], record=["record", "wl", "overall"],
    adjoe=["adjoe", "adjo", "adjoff", "adjoffense", "adjoffeff", "adjustedoffense"],
    adjde=["adjde", "adjd", "adjdef", "adjdefense", "adjdefeff", "adjusteddefense"],
    adjt=["adjt", "adjtempo", "adjtemp", "tempo"], barthag=["barthag", "pythag"], sos=["sos"], wab=["wab"], rank=["rank", "rk"], luck=["luck"])


def normalize_row(o):
    m = {}
    for k, v in o.items():
        m.setdefault(hkey(k), v)

    def pick(names):
        for n in names:
            if n in m and m[n] not in (None, ""):
                return m[n]
        return None
    team = pick(RATING_KEYS["team"])
    if team is None:
        return None
    team = re.sub(r"\s+\d{1,2}$", "", str(team).strip())          # KenPom tournament view appends the seed
    rec = pick(RATING_KEYS["record"])
    if rec is None and m.get("wins") is not None and m.get("losses") is not None:
        rec = f"{m['wins']}-{m['losses']}"
    return dict(team=team, conf=pick(RATING_KEYS["conf"]) or "", record=rec or "", adjoe=to_f(pick(RATING_KEYS["adjoe"])),
                adjde=to_f(pick(RATING_KEYS["adjde"])), adjt=to_f(pick(RATING_KEYS["adjt"])), barthag=to_f(pick(RATING_KEYS["barthag"])),
                sos=to_f(pick(RATING_KEYS["sos"])), wab=to_f(pick(RATING_KEYS["wab"])), rank=to_f(pick(RATING_KEYS["rank"])),
                luck=to_f(pick(RATING_KEYS["luck"])))


def parse_ratings_text(text):
    """CSV / TSV / JSON text -> list of normalized team dicts ([] if the format is not recognised)."""
    text = (text or "").lstrip("\ufeff")
    if text.lstrip()[:1] in "[{":
        try:
            obj = json.loads(text)
        except ValueError:
            return []
        arr = obj if isinstance(obj, list) else (obj.get("teams") or obj.get("data") or obj.get("ratings") or [])
        if arr and not isinstance(arr[0], dict):
            log("  JSON rows have no field names - need a CSV with headers or JSON rows with named fields")
            return []
        return [r for r in (normalize_row(o) for o in arr) if r]
    first = text.split("\n", 1)[0]
    delim = "\t" if first.count("\t") > first.count(",") else ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    for hi in range(min(len(rows), 6)):
        h = [hkey(c) for c in rows[hi]]
        if any(k in h for k in RATING_KEYS["team"]) and any(k in h for k in RATING_KEYS["adjoe"]) and any(k in h for k in RATING_KEYS["adjde"]):
            out = []
            for r in rows[hi + 1:]:
                o = {rows[hi][j]: v for j, v in enumerate(r) if j < len(rows[hi])}
                n = normalize_row(o)
                if n:
                    out.append(n)
            return out
    return []


def sane(teams):
    good = [t for t in teams if t["adjoe"] and t["adjde"] and t["adjt"] and 60 < t["adjoe"] < 150 and 60 < t["adjde"] < 150 and 50 < t["adjt"] < 90]
    return len(good) >= 300 and len(teams) - len(good) <= 5


def year_in(text):
    m = re.search(r"(20\d\d)", text or "")
    return int(m.group(1)) if m else None


def fetch_ratings_url(url):
    try:
        r = requests.get(url, headers=UA, timeout=40)
        log(f"  RATINGS_URL: HTTP {r.status_code}, {len(r.text)} chars")
        if r.status_code == 200:
            teams = parse_ratings_text(r.text)
            if sane(teams):
                return teams
            log(f"  RATINGS_URL parsed {len(teams)} teams but failed sanity checks (need 300+ with team, AdjOE, AdjDE, AdjT)")
    except Exception as e:
        log(f"  RATINGS_URL failed: {e}")
    return []


def local_ratings():
    files = glob.glob(os.path.join(HERE, "ratings_manual*")) + glob.glob(os.path.join(HERE, "torvik_manual*"))
    files = [f for f in files if f.lower().endswith((".csv", ".json", ".tsv", ".txt"))]
    files.sort(key=lambda f: (year_in(os.path.basename(f)) or 0, os.path.getmtime(f)), reverse=True)
    for f in files:
        teams = parse_ratings_text(open(f, encoding="utf-8", errors="replace").read())
        if sane(teams):
            return teams, os.path.basename(f), year_in(os.path.basename(f)), dt.datetime.utcfromtimestamp(os.path.getmtime(f)).replace(microsecond=0).isoformat() + "Z"
        log(f"  {os.path.basename(f)}: {len(teams)} teams, failed sanity checks")
    return [], "", None, ""


def one_line(txt, n=110):
    return re.sub(r"\s+", " ", (txt or "")).strip()[:n]


def fetch_torvik(year):
    """Tries three addresses with browser-style headers and a cookie warm-up, two attempts each. Returns [] if blocked."""
    sess = requests.Session()
    sess.headers.update(BROWSER)
    try:
        sess.get("https://barttorvik.com/", timeout=15)
    except Exception:
        pass
    for tmpl in TORVIK_URLS:
        url = tmpl.format(year=year)
        short = url.replace("https://", "")
        for attempt in (1, 2):
            try:
                r = sess.get(url, timeout=30)
                log(f"  Torvik {year} [{short}] try {attempt}: HTTP {r.status_code}, {len(r.text)} chars")
                if r.status_code == 200:
                    teams = parse_ratings_text(r.text)
                    if sane(teams):
                        return teams
                    log(f"    answered 200 but not usable ratings ({len(teams)} rows parsed): {one_line(r.text)}")
                    break
                log(f"    answer began: {one_line(r.text)}")
                if r.status_code in (403, 429) or r.status_code >= 500:
                    time.sleep(2 * attempt)
                    continue
                break
            except Exception as e:
                log(f"  Torvik {year} [{short}] try {attempt} failed: {e}")
                time.sleep(2)
    log(f"  Torvik {year}: every address was blocked or empty (often blocked for automated requests from GitHub, or not published yet)")
    return []


# ---------------------------------------------------------------- ratings built from ESPN scores
def espn_results(start, end, workers=6):
    """Completed games from start..end (dates), one ESPN call per day."""
    days = [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]

    def one(d):
        for attempt in (1, 2):
            try:
                r = requests.get(ESPN_URL.format(d=d.strftime("%Y%m%d")), headers=UA, timeout=20)
                if r.status_code == 200:
                    return r.json().get("events", [])
            except Exception:
                time.sleep(1)
        return []
    out, seen = [], set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for evs in ex.map(one, days):
            for ev in evs:
                comp = (ev.get("competitions") or [{}])[0]
                t = (ev.get("status") or {}).get("type") or {}
                cs = comp.get("competitors") or []
                home = next((c for c in cs if c.get("homeAway") == "home"), None)
                away = next((c for c in cs if c.get("homeAway") == "away"), None)
                if not t.get("completed") or not home or not away or ev.get("id") in seen:
                    continue
                hs, as_ = to_f(home.get("score")), to_f(away.get("score"))
                if hs is None or as_ is None:
                    continue
                seen.add(ev.get("id"))
                out.append(dict(home=home["team"]["displayName"], away=away["team"]["displayName"], hs=hs, as_=as_, neutral=bool(comp.get("neutralSite"))))
    return out


def build_espn_ratings(games, min_games=ESPN_MIN_GAMES, iters=60, ridge=3.0):
    """Adjusted points for/against from scores alone: score = mu + off(team) + def(opponent) + home edge, ridge-shrunk and solved by iteration.
    No tempo data, so every team gets the same tempo; spreads are sound, totals are rougher than Torvik's."""
    games = list(games)
    for _ in range(3):                                        # drop teams (exhibition opponents etc.) with too few games
        cnt = {}
        for g in games:
            cnt[g["home"]] = cnt.get(g["home"], 0) + 1
            cnt[g["away"]] = cnt.get(g["away"], 0) + 1
        keep = {t for t, c in cnt.items() if c >= min_games}
        games = [g for g in games if g["home"] in keep and g["away"] in keep]
    if not games:
        return []
    teams = sorted({g["home"] for g in games} | {g["away"] for g in games})
    mu = sum(g["hs"] + g["as_"] for g in games) / (2 * len(games))
    off = {t: 0.0 for t in teams}
    dfn = {t: 0.0 for t in teams}
    hca = 3.0
    n = {t: 0 for t in teams}
    for g in games:
        n[g["home"]] += 1
        n[g["away"]] += 1
    for _ in range(iters):
        so = {t: 0.0 for t in teams}
        for g in games:
            h, a = g["home"], g["away"]
            adj = 0.0 if g["neutral"] else hca
            so[h] += g["hs"] - mu - dfn[a] - adj / 2
            so[a] += g["as_"] - mu - dfn[h] + adj / 2
        off = {t: so[t] / (n[t] + ridge) for t in teams}
        sd = {t: 0.0 for t in teams}
        for g in games:
            h, a = g["home"], g["away"]
            adj = 0.0 if g["neutral"] else hca
            sd[a] += g["hs"] - mu - off[h] - adj / 2
            sd[h] += g["as_"] - mu - off[a] + adj / 2
        dfn = {t: sd[t] / (n[t] + ridge) for t in teams}
        res = [(g["hs"] - g["as_"]) - ((off[g["home"]] + dfn[g["away"]]) - (off[g["away"]] + dfn[g["home"]])) for g in games if not g["neutral"]]
        if res:
            hca = max(0.0, min(8.0, sum(res) / len(res)))
    wl = {t: [0, 0] for t in teams}
    for g in games:
        w, l = (g["home"], g["away"]) if g["hs"] > g["as_"] else (g["away"], g["home"])
        wl[w][0] += 1
        wl[l][1] += 1
    rows = []
    for t in teams:
        oe, de = 100 * (mu + off[t]) / AVG_POSS, 100 * (mu + dfn[t]) / AVG_POSS
        bar = oe ** 11.5 / (oe ** 11.5 + de ** 11.5)
        rows.append(dict(team=t, conf="", record=f"{wl[t][0]}-{wl[t][1]}", adjoe=round(oe, 2), adjde=round(de, 2), adjt=AVG_POSS, barthag=round(bar, 4), sos=None, wab=None, rank=None, luck=None))
    rows.sort(key=lambda r: -r["barthag"])
    for i, r in enumerate(rows):
        r["rank"] = i + 1
    return rows


# ---------------------------------------------------------------- the ratings chain
def get_ratings(prev, keys):
    """-> dict(teams, season, stale, note, source, as_of, chain). Walks the chain and logs every step."""
    yr = season_year()
    now = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    chain = []

    def step(name, status):
        chain.append(dict(step=name, status=status))
        log(f"  [{len(chain)}] {name}: {status}")

    def pack(teams, src, season, as_of, note=None):
        stale = bool(season and season < yr)
        n = note if note is not None else (f"ratings are for the {season} season; {yr} not loaded yet" if stale else "")
        return dict(teams=teams, season=season or yr, stale=stale, note=n, source=src, as_of=as_of, chain=chain)
    # 1) RATINGS_URL
    if keys.get("RATINGS_URL"):
        t = fetch_ratings_url(keys["RATINGS_URL"])
        if t:
            step("RATINGS_URL", f"used ({len(t)} teams)")
            return pack(t, "RATINGS_URL", year_in(keys["RATINGS_URL"]) or yr, now)
        step("RATINGS_URL", "failed")
    else:
        step("RATINGS_URL", "not set")
    # 2) manual / pushed file for the current season
    lt, lname, lfy, lmt = local_ratings()
    if lt and (lfy is None or lfy >= yr):
        step("ratings file in the repo", f"used {lname} ({len(lt)} teams)")
        return pack(lt, lname, lfy or yr, lmt)
    step("ratings file in the repo", f"none for {yr}" + (f" (found older: {lname}, kept as a later fallback)" if lt else ""))
    # 3) Torvik live
    for y in (yr, yr - 1):
        t = fetch_torvik(y)
        if t:
            step("Torvik live", f"used the {y} file ({len(t)} teams)")
            return pack(t, "Torvik live", y, now)
    step("Torvik live", "blocked or not published")
    # 4) ratings built from ESPN scores
    start = dt.date(yr - 1, 11, 1)
    if today() <= start + dt.timedelta(days=14):
        step("ESPN-built ratings", "skipped (the season has barely started, not enough games)")
    else:
        try:
            games = espn_results(start, today())
            t = build_espn_ratings(games)
            if sane(t):
                step("ESPN-built ratings", f"used ({len(t)} teams from {len(games)} games)")
                return pack(t, "ESPN-built (no tempo data)", yr, now,
                            note="ratings built from ESPN scores because Torvik could not be reached: spreads are sound, totals are rougher (no tempo data)")
            step("ESPN-built ratings", f"not enough games yet ({len(t)} teams with {ESPN_MIN_GAMES}+ games from {len(games)} games)")
        except Exception as e:
            step("ESPN-built ratings", f"failed: {e}")
    # 5) older-season file kept in the repo
    if lt:
        step("older-season file", f"used {lname} ({len(lt)} teams, season {lfy})")
        return pack(lt, lname, lfy, lmt)
    step("older-season file", "none")
    # 6) previous ratings in the last output
    if prev and prev.get("teams"):
        step("carried forward", f"used the ratings from the last good pull ({len(prev['teams'])} teams)")
        return dict(teams=prev["teams"], season=prev.get("season"), stale=True, note="ratings could not be refreshed; carried forward from the last good pull",
                    source=(prev.get("ratings_source") or "previous pull") + " (carried forward)", as_of=prev.get("ratings_as_of") or prev.get("generated_at"), chain=chain)
    step("carried forward", "nothing saved from an earlier pull")
    return dict(teams=[], season=yr, stale=True, note="NO RATINGS: every source in the chain failed. Add torvik_manual_<year>.csv to the repo, or set RATINGS_URL; schedule and lines were still saved",
                source="none", as_of=None, chain=chain)


# ---------------------------------------------------------------- espn
def fetch_espn(days_back=1, days_fwd=7):
    out, seen = [], set()
    base = today()
    for i in range(-days_back, days_fwd + 1):
        d = base + dt.timedelta(days=i)
        try:
            r = requests.get(ESPN_URL.format(d=d.strftime("%Y%m%d")), headers=UA, timeout=20)
            if r.status_code != 200:
                continue
            for ev in r.json().get("events", []):
                comp = (ev.get("competitions") or [{}])[0]
                cs = comp.get("competitors") or []
                home = next((c for c in cs if c.get("homeAway") == "home"), None)
                away = next((c for c in cs if c.get("homeAway") == "away"), None)
                if not home or not away or ev.get("id") in seen:
                    continue
                seen.add(ev.get("id"))
                t = (ev.get("status") or {}).get("type") or {}

                def sc(c):
                    try:
                        return float(c.get("score"))
                    except (TypeError, ValueError):
                        return None
                out.append(dict(id=ev.get("id"), date=ev.get("date"), away=away["team"]["displayName"], home=home["team"]["displayName"],
                                neutral=bool(comp.get("neutralSite")), venue=(comp.get("venue") or {}).get("fullName", ""),
                                notes=" | ".join(n.get("headline", "") for n in comp.get("notes", [])),
                                status=t.get("shortDetail", ""), completed=bool(t.get("completed")),
                                awayScore=sc(away), homeScore=sc(home)))
        except Exception as e:
            log(f"  ESPN {d}: {e}")
        time.sleep(0.2)
    return out


# ---------------------------------------------------------------- odds
def fetch_odds(key):
    """Returns (events, info). info is saved in the JSON so the HTML can show credits left without calling the API."""
    info = dict(status="skipped", fetched_at=dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z", used=None, remaining=None, last=None, events=0, books=[], error="")
    if not key:
        log("  ODDS_API_KEY missing – skipping lines")
        info["error"] = "ODDS_API_KEY missing"
        return [], info
    try:
        r = requests.get(ODDS_URL.format(key=key), timeout=30)
        h = r.headers
        info.update(status=r.status_code, used=to_f(h.get("x-requests-used")), remaining=to_f(h.get("x-requests-remaining")), last=to_f(h.get("x-requests-last")))
        log(f"  Odds API: HTTP {r.status_code} · used {h.get('x-requests-used', '?')} · left {h.get('x-requests-remaining', '?')} · last call cost {h.get('x-requests-last', '?')}")
        if r.status_code == 200:
            ev = r.json()
            info["events"] = len(ev)
            info["books"] = sorted({b.get("key") for e in ev for b in e.get("bookmakers", []) if b.get("key")})
            return ev, info
        info["error"] = (r.text or "")[:160]
    except Exception as e:
        info["error"] = str(e)[:160]
        log(f"  Odds API failed: {e}")
    return [], info


# ---------------------------------------------------------------- main
def main():
    log(f"NCAAM Edge Model pull v{VERSION} · {dt.datetime.utcnow():%Y-%m-%d %H:%M} UTC")
    keys = load_keys()
    prev = None
    if os.path.exists(OUT):
        try:
            prev = json.load(open(OUT, encoding="utf-8"))
        except Exception:
            prev = None
    R = get_ratings(prev, keys)
    sched = fetch_espn()
    odds, odds_info = fetch_odds(keys["ODDS_API_KEY"])
    if not odds and prev and prev.get("odds_raw"):
        log("  keeping previous odds_raw (fresh pull returned nothing)")
        odds = prev["odds_raw"]
        odds_info["kept_previous"] = True
    data = dict(version=VERSION, generated_at=dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
                season=R["season"], stale_prior_season=R["stale"], note=R["note"], ratings_source=R["source"], ratings_as_of=R["as_of"],
                teams=R["teams"], schedule=sched, odds_raw=odds,
                api=dict(odds=odds_info, espn=dict(games=len(sched), first=(min((g.get("date") or "") for g in sched)[:10] if sched else None), last=(max((g.get("date") or "") for g in sched)[:10] if sched else None)),
                         ratings=dict(source=R["source"], as_of=R["as_of"], teams=len(R["teams"]), note=R["note"], chain=R.get("chain", []))),
                _counts=dict(teams=len(R["teams"]), schedule=len(sched), odds=len(odds)))
    tmp = OUT + ".tmp"
    json.dump(data, open(tmp, "w", encoding="utf-8"), separators=(",", ":"))
    os.replace(tmp, OUT)
    log(f"Wrote {OUT}: {len(R['teams'])} teams ({R['source']}) · {len(sched)} schedule games · {len(odds)} odds events" + (f" · NOTE: {R['note']}" if R["note"] else ""))
    if not R["teams"]:
        log("WARNING: no ratings. The file still has lines and schedule; the model will use ratings cached in the browser.")


if __name__ == "__main__":
    main()
