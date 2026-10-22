#!/usr/bin/env python3
"""
NCAAM Edge Model - historical game results pull   VERSION 1.2

Walks ESPN's public men's college basketball scoreboard day by day and writes one CSV per
season into  history/ncaam_games_<season>.csv  (season 2025 = the 2024-25 season, Nov through early Apr).
The CSVs use the model's games-CSV columns, so the Backtest tab can load them directly:

  date, season, team_a, team_b, score_a, score_b, site, stage, event, espn_id

  team_a = AWAY team, team_b = HOME team (site "B Home"), or both neutral (site "Neutral").
  stage is one of the model's stages (Regular Season, Conference Tournament, First Four, Round of 64/32,
  Sweet 16/Elite 8, Final Four/Championship). NIT / CBI / other postseason games stay "Regular Season"
  with the real name in the event column so they can be filtered out.

Scores only: ESPN has no historical betting lines. A summary is written to history/ncaam_games_summary.json.

Usage (GitHub Actions runs this; you can also run it by hand):
  python ncaam_history_pull.py --seasons 2022-2026
  python ncaam_history_pull.py --seasons 2024,2025 --only-missing
  python ncaam_history_pull.py --seasons current          # this season only (the weekly schedule uses this)
"""
import argparse, csv, json, os, sys, time
from datetime import date, datetime, timedelta, timezone

import requests

VERSION = "1.2"
URL = ("https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard"
       "?dates={d}&groups=50&limit=400")
OUT_DIR = "history"
COLS = ["date", "season", "team_a", "team_b", "score_a", "score_b", "site", "stage", "event", "espn_id"]
try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    ET = timezone(timedelta(hours=-5))


def current_season():
    """Season label for today (2025 = 2024-25). Aug-Dec belongs to the NEXT calendar year's season."""
    d = datetime.now(ET).date()
    return d.year + 1 if d.month >= 8 else d.year


def parse_seasons(s):
    out = []
    for part in str(s).split(","):
        part = part.strip()
        if not part:
            continue
        if part.lower() == "current":
            out.append(current_season())
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out += list(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return sorted(set(out))


def stage_of(notes, d):
    """NCAA tournament games are labelled "Men's Basketball Championship - <Region> - <round>".
    Conference tournaments are "<Conference> Championship - <round>" (or contain "Tournament").
    NIT / CBI / College Basketball Crown stay Regular Season (the event column keeps the real name)."""
    import re
    n = (notes or "").lower()
    other_post = re.search(r"\b(nit|cbi|cit)\b|invitational|basketball crown|vegas 16|the basketball classic", n)
    ncaa = ("ncaa" in n) or ("men's basketball championship" in n) or ("mens basketball championship" in n)
    if ncaa and not other_post:
        if "first four" in n: return "First Four"
        if re.search(r"(final four|national championship|national semi)", n) or re.search(r"basketball championship\s*-\s*(final four|national)", n): return "Final Four/Championship"
        if re.search(r"(sweet 16|elite (8|eight)|regional (semi|final))", n): return "Sweet 16/Elite 8"
        if re.search(r"(1st|first|2nd|second) round", n): return "Round of 64/32"
        return "Regular Season"
    if other_post: return "Regular Season"
    if re.search(r"(championship|tournament|tourney)", n): return "Conference Tournament"
    return "Regular Season"


def et_date(iso):
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone(ET).date()
    except Exception:
        return None


def team_name(t):
    return (t.get("location") or t.get("shortDisplayName") or t.get("displayName") or "").strip()


def parse_events(js, season):
    rows = []
    for ev in js.get("events") or []:
        comp = (ev.get("competitions") or [{}])[0]
        st = (ev.get("status") or {}).get("type") or {}
        if not st.get("completed"):
            continue
        cs = comp.get("competitors") or []
        home = next((c for c in cs if c.get("homeAway") == "home"), None)
        away = next((c for c in cs if c.get("homeAway") == "away"), None)
        if not home or not away:
            continue
        try:
            sh, sa = int(float(home.get("score"))), int(float(away.get("score")))
        except (TypeError, ValueError):
            continue
        if sh == 0 and sa == 0:
            continue
        d = et_date(ev.get("date") or "")
        if d is None:
            continue
        notes = " | ".join(n.get("headline", "") for n in (comp.get("notes") or []) if n.get("headline"))
        neutral = bool(comp.get("neutralSite"))
        rows.append({"date": d.isoformat(), "season": season, "team_a": team_name(away["team"]), "team_b": team_name(home["team"]),
                     "score_a": sa, "score_b": sh, "site": "Neutral" if neutral else "B Home", "stage": stage_of(notes, d),
                     "event": notes, "espn_id": ev.get("id")})
    return rows


def get(session, d, retries=4):
    last = None
    for k in range(retries):
        try:
            r = session.get(URL.format(d=d.strftime("%Y%m%d")), timeout=30)
            if r.status_code == 200:
                return r.json(), None
            last = "HTTP %s" % r.status_code
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 * (k + 1)); continue
            break
        except Exception as e:  # network blips
            last = str(e)[:120]; time.sleep(2 * (k + 1))
    return None, last


def pull_season(session, season, pause):
    start, end = date(season - 1, 11, 1), date(season, 4, 12)
    today = datetime.now(ET).date()
    d, rows, seen, errors, days = start, [], set(), [], 0
    while d <= min(end, today):
        js, err = get(session, d)
        days += 1
        if js is None:
            errors.append("%s %s" % (d.isoformat(), err))
        else:
            for r in parse_events(js, season):
                if r["espn_id"] in seen:
                    continue
                seen.add(r["espn_id"]); rows.append(r)
        d += timedelta(days=1); time.sleep(pause)
    rows.sort(key=lambda r: (r["date"], r["team_b"]))
    return rows, errors, days


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", default="2025")
    ap.add_argument("--only-missing", action="store_true", help="skip seasons whose CSV already exists")
    ap.add_argument("--pause", type=float, default=0.25)
    a = ap.parse_args()
    os.makedirs(OUT_DIR, exist_ok=True)
    print("ncaam_history_pull VERSION", VERSION)
    sess = requests.Session(); sess.headers["User-Agent"] = "Mozilla/5.0 (EdgeReport history pull)"
    sp = os.path.join(OUT_DIR, "ncaam_games_summary.json")
    summary = json.load(open(sp)) if os.path.exists(sp) else {}
    summary.setdefault("seasons", {})
    bad = 0
    for s in parse_seasons(a.seasons):
        path = os.path.join(OUT_DIR, "ncaam_games_%d.csv" % s)
        if a.only_missing and os.path.exists(path):
            print("season", s, "exists - skipped"); continue
        rows, errors, days = pull_season(sess, s, a.pause)
        if not rows:
            print("season", s, "NO GAMES returned (%d days asked, %d errors). Not writing a file." % (days, len(errors)), errors[:3]); bad += 1
            summary["seasons"][str(s)] = dict(games=0, errors=len(errors), error_sample=errors[:5], pulled_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
            continue
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=COLS); w.writeheader(); w.writerows(rows)
        stages = {}
        for r in rows: stages[r["stage"]] = stages.get(r["stage"], 0) + 1
        summary["seasons"][str(s)] = dict(games=len(rows), first=rows[0]["date"], last=rows[-1]["date"], days_asked=days, errors=len(errors),
                                         error_sample=errors[:5], neutral=sum(1 for r in rows if r["site"] == "Neutral"), stages=stages,
                                         pulled_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        print("season %d: %d games %s..%s · %d day-errors · stages %s" % (s, len(rows), rows[0]["date"], rows[-1]["date"], len(errors), stages))
        if errors: print("   first errors:", errors[:3])
    summary["version"] = VERSION
    json.dump(summary, open(sp, "w"), indent=1)
    return 1 if bad and not any(v.get("games") for v in summary["seasons"].values()) else 0


if __name__ == "__main__":
    sys.exit(main())
