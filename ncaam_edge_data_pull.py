#!/usr/bin/env python3
"""
ncaam_edge_data_pull.py  (NCAAM Edge Model v1.4 companion)

Writes ncaam_model_data.json for the NCAAM Edge Model HTML:
  teams     BartTorvik ratings (adjoe, adjde, adjt, barthag, ...)
  schedule  ESPN scoreboard games (neutral-site flag, venue, tournament notes, scores)
  odds_raw  The Odds API FanDuel + DraftKings lines, unmodified (the HTML parses them)
  api       what each source answered: Odds API credits used / remaining / last request cost, ESPN game counts, ratings status

Keys: keys.txt next to this script (ODDS_API_KEY=...) wins over environment variables.
Name matching is NOT done here – the HTML does it, visibly, so nothing is silently zeroed.

Ratings come from the first source that works:
  1) RATINGS_URL  (keys.txt / environment / GitHub secret): a link to a CSV or JSON of team ratings
  2) a file next to this script: ratings_manual*.csv|json or torvik_manual*.csv (year taken from the file name)
  3) BartTorvik live download (often blocked for automated requests: HTTP 403)
  4) the ratings already in the last ncaam_model_data.json (carried forward, with their original date)
Whatever happens to ratings, the schedule and lines are still saved - the HTML keeps the last good ratings.
Files need a header row with team, AdjOE, AdjDE, AdjT (Torvik: adjoe/adjde/adjt; KenPom: AdjO/AdjD/AdjT or AdjOE/AdjDE/AdjTempo).
"""
import csv, datetime as dt, glob, io, json, os, re, sys, time

try:
    import requests
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests"])
    import requests

VERSION = "1.2"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "ncaam_model_data.json")
UA = {"User-Agent": "Mozilla/5.0 (compatible; EdgeReportPull/1.0)"}
ODDS_URL = ("https://api.the-odds-api.com/v4/sports/basketball_ncaab/odds"
            "?apiKey={key}&regions=us&markets=h2h,spreads,totals&oddsFormat=american"
            "&bookmakers=fanduel,draftkings")
ESPN_URL = ("https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/"
            "scoreboard?dates={d}&groups=50&limit=400")
TORVIK_CSV = "https://barttorvik.com/{year}_team_results.csv"


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
def season_year(today=None):
    t = today or dt.date.today()
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


def fetch_torvik(year):
    url = TORVIK_CSV.format(year=year)
    try:
        r = requests.get(url, headers=UA, timeout=30)
        log(f"  Torvik {year}: HTTP {r.status_code}, {len(r.text)} chars")
        if r.status_code == 200 and "adjoe" in r.text[:600].lower():
            teams = parse_ratings_text(r.text)
            if sane(teams):
                return teams
            log(f"  Torvik {year}: parsed {len(teams)} teams but failed sanity checks")
        else:
            log("  Torvik did not return the CSV (blocked for automated requests, or not published yet). Use RATINGS_URL or a ratings_manual file.")
    except Exception as e:
        log(f"  Torvik {year} failed: {e}")
    return []


def get_ratings(prev, keys):
    """-> dict(teams, season, stale, note, source, as_of)"""
    yr = season_year()
    now = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

    def pack(teams, src, season, as_of):
        stale = bool(season and season < yr)
        note = f"ratings are for the {season} season; {yr} not loaded yet" if stale else ""
        return dict(teams=teams, season=season or yr, stale=stale, note=note, source=src, as_of=as_of)
    if keys.get("RATINGS_URL"):
        t = fetch_ratings_url(keys["RATINGS_URL"])
        if t:
            return pack(t, "RATINGS_URL", year_in(keys["RATINGS_URL"]) or yr, now)
    t, name, fy, mt = local_ratings()
    if t:
        return pack(t, name, fy or yr, mt)
    t = fetch_torvik(yr)
    if t:
        return pack(t, "Torvik live", yr, now)
    t = fetch_torvik(yr - 1)
    if t:
        return pack(t, "Torvik live", yr - 1, now)
    if prev and prev.get("teams"):
        return dict(teams=prev["teams"], season=prev.get("season"), stale=True, note="ratings could not be refreshed; carried forward from the last good pull",
                    source=(prev.get("ratings_source") or "previous pull") + " (carried forward)", as_of=prev.get("ratings_as_of") or prev.get("generated_at"))
    return dict(teams=[], season=yr, stale=True, note="NO RATINGS: set RATINGS_URL or add a ratings_manual*.csv; schedule and lines were still saved",
                source="none", as_of=None)


# ---------------------------------------------------------------- espn
def fetch_espn(days_back=1, days_fwd=7):
    out, seen = [], set()
    today = dt.date.today()
    for i in range(-days_back, days_fwd + 1):
        d = today + dt.timedelta(days=i)
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
                         ratings=dict(source=R["source"], as_of=R["as_of"], teams=len(R["teams"]), note=R["note"])),
                _counts=dict(teams=len(R["teams"]), schedule=len(sched), odds=len(odds)))
    tmp = OUT + ".tmp"
    json.dump(data, open(tmp, "w", encoding="utf-8"), separators=(",", ":"))
    os.replace(tmp, OUT)
    log(f"Wrote {OUT}: {len(R['teams'])} teams ({R['source']}) · {len(sched)} schedule games · {len(odds)} odds events" + (f" · NOTE: {R['note']}" if R["note"] else ""))
    if not R["teams"]:
        log("WARNING: no ratings. The file still has lines and schedule; the model will use ratings cached in the browser.")


if __name__ == "__main__":
    main()
