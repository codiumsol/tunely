#!/usr/bin/env python3
"""
Builds data/stations.json for Tunely Radio.

1. Downloads the most popular stations of ~110 countries from the free Radio
   Browser directory (https://www.radio-browser.info). Every country follows
   the same rules - no country is favoured.
2. Adds anything listed in data/extra_stations.json (your own additions).
3. Removes anything listed in data/blocked.txt (takedown requests, bad stations).
4. Actually connects to every stream and drops the ones that don't play.
   A station that was in the list but fails tonight goes on a watch list
   (data/missing.json) and is tested again every night for 30 days, even if
   the directory stops offering it; when it plays again it is back in the
   list (and back in listeners' favourites). The app removes a favourite
   that stays off air for 10 days.
5. New stations in the directory are picked up every night (by popularity).
5. Writes data/stations.json. The app downloads it from
   https://raw.githubusercontent.com/codiumsol/tunely/main/data/stations.json
   (the app's own build copies it in as the offline fallback).

Runs every night on GitHub. No server needed. Standard library only.
"""
import json
import os
import re
import ssl
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = DATA / "stations.json"
BUNDLED = ROOT / "assets" / "stations.json"
EXTRA = DATA / "extra_stations.json"
BLOCKED = DATA / "blocked.txt"

USER_AGENT = "TunelyRadio/1.0 (+https://github.com)"
MIRRORS = [
    "https://de1.api.radio-browser.info",
    "https://de2.api.radio-browser.info",
    "https://nl1.api.radio-browser.info",
    "https://at1.api.radio-browser.info",
    "https://fi1.api.radio-browser.info",
]
# EVERY country follows the same rules (decision 18 Oct 2026: global and
# equal, no country favoured):
# - the most popular stations of the country from Radio Browser, without its
#   "hidebroken" filter (it checks from Europe and wrongly marks slow servers
#   far away as broken) - our own test decides,
# - hand-picked official streams (data/extra_stations.json) come first,
# - streams that fail get a second, slower try (slow servers abroad),
# - at most PER_COUNTRY_MAX working stations per country.
COUNTRIES = [
    # South + Central Asia
    "PK", "IN", "BD", "LK", "NP", "AF", "AZ", "KZ", "UZ",
    # Middle East
    "AE", "SA", "QA", "KW", "OM", "BH", "JO", "LB", "IQ", "EG", "IR", "SY", "PS", "YE",
    "IL", "CY",
    # UK + Europe
    "GB", "IE", "DE", "FR", "NL", "BE", "LU", "IT", "ES", "PT", "CH", "AT", "SE",
    "NO", "DK", "FI", "IS", "PL", "GR", "RO", "CZ", "SK", "HU", "SI", "HR", "RS",
    "BA", "AL", "MK", "BG", "LT", "LV", "EE", "UA", "RU", "TR", "MT",
    # Americas
    "US", "CA", "MX", "BR", "AR", "CO", "CL", "PE", "VE", "EC", "BO", "UY", "PY",
    "CR", "PA", "GT", "DO", "PR", "JM", "HN", "SV",
    # East + South-East Asia, Oceania
    "CN", "HK", "TW", "JP", "KR", "ID", "MY", "SG", "PH", "TH", "VN", "MM", "KH",
    "MN", "AU", "NZ",
    # Africa
    "NG", "KE", "ZA", "GH", "ET", "MA", "DZ", "TN", "SN", "CI", "CM", "UG", "TZ",
    "ZW", "ZM", "AO", "MZ", "SD", "RW",
]
PER_COUNTRY_FETCH = 120  # candidates asked from Radio Browser per country
PER_COUNTRY_MAX = 60     # working stations kept per country (same for all)
RETRY_PER_COUNTRY = 20   # failed streams tried again, slower, per country
TEST_WORKERS = 128
# Junk entries in the open directory: a bare generic word as the name, or a
# shop / test stream. (Specific bad stations go into data/blocked.txt.)
GENERIC_NAMES = {"fm", "am", "radio", "live", "stream", "online", "test", "music",
                 "webradio", "web radio", "internet radio", "my radio", "radio fm",
                 "fm radio", "live radio", "new station", "station", "default"}
JUNK_WORDS = re.compile(r"\b(pizza|restaurant|cafe|caf\u00e9|hotel|mall|shop|store|"
                        r"test ?stream|demo|sample)\b", re.I)
REPORT = DATA / "report.json"
MISSING = DATA / "missing.json"
TRIAL_DIR = DATA / "trial"  # TRIAL=1: write here, leave the real list alone
MISSING_DAYS = 30
OK_CODECS = {"MP3", "AAC", "AAC+", "OGG", "OPUS", ""}
AUDIO_TYPES = ("audio/", "application/ogg", "application/octet-stream",
               "application/vnd.apple.mpegurl", "application/x-mpegurl",
               "video/mp2t")
# Tags that are noise in the filter chips.
DROP_TAGS = {"pakistan", "pakistani", "urdu", "fm", "radio", "online", "live",
             "internet radio", "webradio", "stereo", "music", "pk", "am", "local"}


def fetch_json(path):
    last = None
    for base in MIRRORS:
        try:
            req = urllib.request.Request(base + path, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as e:  # try the next mirror
            last = e
            print(f"  mirror {base} failed: {e}", file=sys.stderr)
    raise RuntimeError(f"All Radio Browser mirrors failed: {last}")


def clean_name(name):
    name = re.sub(r"\s+", " ", (name or "").replace("\t", " ")).strip(" -|#*_.")
    return name


def is_junk(name):
    """True for names that can't be a real station: too short, a bare generic
    word ("fm", "live"), or a shop / test stream."""
    letters = re.sub(r"[\W_]", "", name, flags=re.UNICODE)
    if len(letters) < 3:
        return True
    if name.lower() in GENERIC_NAMES:
        return True
    return bool(JUNK_WORDS.search(name))


def clean_tags(raw):
    out = []
    for t in (raw or "").split(","):
        t = t.strip().lower()
        if not t or t in DROP_TAGS or len(t) > 18 or t in out:
            continue
        out.append(t)
    return out[:5]


def from_radio_browser(s):
    return {
        "id": s.get("stationuuid", ""),
        "name": clean_name(s.get("name")),
        "url": s.get("url_resolved") or s.get("url") or "",
        "logo": (s.get("favicon") or "").strip(),
        "city": (s.get("state") or "").strip(),
        "tags": clean_tags(s.get("tags")),
        "language": (s.get("language") or "").split(",")[0].strip(),
        "country": s.get("countrycode", ""),
        "bitrate": s.get("bitrate") or 0,
        "homepage": s.get("homepage") or "",
        "_codec": (s.get("codec") or "").upper(),
        "_score": (s.get("clickcount") or 0) + 3 * (s.get("votes") or 0),
    }


def stream_works(st, timeout=10):
    """Connects to the stream and checks that audio actually comes back."""
    url = st["url"]
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT, "Icy-MetaData": "0", "Range": "bytes=0-"})
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            ctype = (r.headers.get("Content-Type") or "").lower()
            chunk = r.read(2048)
            if not chunk:
                return False
            if "text/html" in ctype:
                return False
            if url.lower().split("?")[0].endswith(".m3u8") or "mpegurl" in ctype:
                return b"#EXTM3U" in chunk[:64]
            return ctype.startswith(AUDIO_TYPES) or ctype == "" or "icy" in ctype
    except Exception:
        return False


QUERY_COUNTS = {}


def collect(queries, blocked):
    """Downloads, cleans and de-duplicates the stations of some queries."""
    stations = {}
    for q in queries:
        try:
            rows = fetch_json(q)
        except RuntimeError as e:
            print(f"  skipped {q.split('?')[0]}: {e}", file=sys.stderr)
            QUERY_COUNTS[q.split("?")[0]] = "failed"
            continue
        QUERY_COUNTS[q.split("?")[0]] = len(rows)
        for raw in rows:
            st = from_radio_browser(raw)
            if not st["id"] or not st["name"] or not st["url"]:
                continue
            if is_junk(st["name"]):
                continue
            if st["_codec"] not in OK_CODECS:
                continue
            if {st["id"].lower(), st["name"].lower(), st["url"].lower()} & blocked:
                continue
            stations[st["id"]] = st
    # Same station listed twice (same name in the same country): keep the
    # most popular one.
    best = {}
    for st in stations.values():
        key = (st["country"], re.sub(r"[^a-z0-9]", "", st["name"].lower()) or st["name"])
        if key not in best or st["_score"] > best[key]["_score"]:
            best[key] = st
    return sorted(best.values(), key=lambda s: -s["_score"])


def _norm_url(u):
    return re.sub(r"^https?://", "", u.lower()).split("?")[0].rstrip("/")


def _norm_name(n):
    return re.sub(r"[^a-z0-9]", "", n.lower())


def merge_extras(home, blocked):
    """Adds data/extra_stations.json (hand-picked official streams, e.g. all
    Radio Pakistan channels from radio.gov.pk/live-streaming). When the
    directory has the same station (same stream or same name), the official
    entry wins but keeps the directory's popularity, so popular stations
    stay on top and the extras don't push them down."""
    if not EXTRA.exists():
        return home
    extras = json.loads(EXTRA.read_text(encoding="utf-8"))
    by_url = {_norm_url(s["url"]): s for s in home}
    by_name = {(s["country"], _norm_name(s["name"])): s for s in home}
    out = list(home)
    for i, st in enumerate(extras):
        if {st.get("id", "").lower(), st["name"].lower(), st["url"].lower()} & blocked:
            continue
        st.setdefault("id", f"extra-{i}")
        st.setdefault("tags", [])
        st["_codec"] = ""
        st["_extra"] = True
        twin = by_url.get(_norm_url(st["url"])) or by_name.get(
            (st["country"], _norm_name(st["name"])))
        st["_score"] = twin["_score"] if twin else 0
        if twin in out:
            out[out.index(twin)] = st
        else:
            out.append(st)
    out.sort(key=lambda s: -s["_score"])
    return out


def test_streams(candidates, timeout=10, workers=TEST_WORKERS):
    """True/False per candidate: does the stream really play?"""
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda st: stream_works(st, timeout=timeout), candidates))


def main():
    blocked = set()
    if BLOCKED.exists():
        for line in BLOCKED.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                blocked.add(line.lower())

    trial = bool(os.environ.get("TRIAL"))
    out_file = TRIAL_DIR / "stations.json" if trial else OUT
    report_file = TRIAL_DIR / "report.json" if trial else REPORT
    missing_file = TRIAL_DIR / "missing.json" if trial else MISSING

    started = time.time()
    today = time.strftime("%Y-%m-%d", time.gmtime())

    # Stations listed last night + the watch list: all are tested again.
    previous_list = []
    if OUT.exists():
        try:
            previous_list = json.loads(OUT.read_text(encoding="utf-8")).get("stations", [])
        except ValueError:
            pass
    missing = {}
    if MISSING.exists():
        try:
            missing = json.loads(MISSING.read_text(encoding="utf-8"))
        except ValueError:
            missing = {}

    def is_blocked(st):
        return bool({st.get("id", "").lower(), st.get("name", "").lower(),
                     st.get("url", "").lower()} & blocked)

    # 1. Candidates per country: hand-picked extras first, then stations on
    #    the watch list, then the directory by popularity (new stations
    #    included, so new channels are found every night).
    queries = [f"/json/stations/bycountrycodeexact/{cc}?order=clickcount"
               f"&reverse=true&limit={PER_COUNTRY_FETCH}" for cc in COUNTRIES]
    directory = merge_extras(collect(queries, blocked), blocked)
    by_cc = {}
    for st in directory:
        by_cc.setdefault(st["country"], []).append(st)
    for cc, lst in by_cc.items():
        lst.sort(key=lambda st: (0 if st.get("_extra") else 1, -st["_score"]))
    ids = {st["id"] for st in directory}
    for m in missing.values():
        st = m["station"]
        if st["id"] in ids or is_blocked(st) or st.get("country") not in COUNTRIES:
            continue
        lst = by_cc.setdefault(st["country"], [])
        n_extra = sum(1 for x in lst if x.get("_extra"))
        lst.insert(n_extra, dict(st, _score=-1, _codec=""))
        ids.add(st["id"])

    candidates = [st for cc in COUNTRIES for st in by_cc.get(cc, [])]
    print(f"{len(candidates)} candidates from {len(COUNTRIES)} countries, testing ...")
    ok = dict(zip((st["id"] for st in candidates), test_streams(candidates)))

    # 2. Second, slower try for countries that are not full yet.
    retry = []
    for cc in COUNTRIES:
        lst = by_cc.get(cc, [])
        if sum(ok[st["id"]] for st in lst) >= PER_COUNTRY_MAX:
            continue
        retry += [st for st in lst if not ok[st["id"]]][:RETRY_PER_COUNTRY]
    print(f"Second try for {len(retry)} slow streams ...")
    for st, good in zip(retry, test_streams(retry, timeout=25, workers=64)):
        ok[st["id"]] = good
    recovered = sum(1 for st in retry if ok[st["id"]])

    # 3. Keep up to PER_COUNTRY_MAX working stations per country.
    working, failed_sample = [], {}
    for cc in COUNTRIES:
        kept = 0
        for st in by_cc.get(cc, []):
            if ok[st["id"]] and kept < PER_COUNTRY_MAX:
                working.append(st)
                kept += 1
            elif not ok[st["id"]]:
                failed_sample.setdefault(cc, []).append(st["name"])

    counts = {}
    for st in working:
        counts[st["country"]] = counts.get(st["country"], 0) + 1
    print(f"{len(working)} stations are working, {len(counts)} countries, "
          f"{time.time() - started:.0f} s (second try saved {recovered})")
    print("Per country: " + ", ".join(f"{k} {v}" for k, v in
                                      sorted(counts.items(), key=lambda x: -x[1])))

    # Watch list: listed before (or already watched) but not playing tonight.
    working_now = {st["id"] for st in working}
    new_missing = {}
    for st in previous_list:
        if st["id"] not in working_now and not is_blocked(st):
            since = missing.get(st["id"], {}).get("since", today)
            new_missing[st["id"]] = {"since": since, "station": st}
    for sid, m in missing.items():
        if sid in working_now or sid in new_missing or is_blocked(m["station"]):
            continue
        new_missing[sid] = m
    cutoff = time.strftime("%Y-%m-%d", time.gmtime(time.time() - MISSING_DAYS * 86400))
    new_missing = {k: v for k, v in new_missing.items() if v["since"] >= cutoff}
    back = len([sid for sid in missing if sid in working_now])
    listed_before = {st["id"] for st in previous_list} | set(missing)
    new_tonight = [st["name"] for st in working if st["id"] not in listed_before]
    print(f"Watch list: {len(new_missing)} off air, {back} came back, "
          f"{len(new_tonight)} new stations tonight")

    # Small report for the developers: what was offered, what worked.
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text(json.dumps({
        "date": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
        "trial": trial,
        "seconds": round(time.time() - started),
        "candidates": len(candidates),
        "working": len(working),
        "countries": len(counts),
        "second_try": len(retry),
        "second_try_saved": recovered,
        "per_country": dict(sorted(counts.items(), key=lambda x: -x[1])),
        "off_air_watch": len(new_missing),
        "came_back_tonight": back,
        "new_tonight": len(new_tonight) if previous_list else 0,
        "new_tonight_sample": new_tonight[:100] if previous_list else [],
        "failed_sample": {k: v[:15] for k, v in failed_sample.items()},
        "queries": QUERY_COUNTS,
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    previous = len(previous_list)
    if len(working) < 10 or len(working) < previous * 0.6:
        # Something is wrong (network issue, directory down). A bad night must
        # never empty the app: keep the previous list.
        print(f"Only {len(working)} working (previous list: {previous}) - "
              "keeping the previous list.", file=sys.stderr)
        return 0 if OUT.exists() else 1

    for st in working:
        for k in ("_score", "_codec", "_extra"):
            st.pop(k, None)

    # off_air: saved stations the app may still show, with the date they
    # went off air (the app removes them from favourites after 10 days, and
    # at once when a station isn't here at all = gone for 30+ days).
    off_air = {k: v["since"] for k, v in new_missing.items()}
    body = json.dumps({"version": 2, "count": len(working), "stations": working,
                       "off_air": off_air},
                      ensure_ascii=False, separators=(",", ":"))
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(body, encoding="utf-8")
    missing_file.write_text(json.dumps(new_missing, ensure_ascii=False, indent=0,
                                       sort_keys=True), encoding="utf-8")
    if not trial and BUNDLED.parent.exists():  # only inside the app repo
        BUNDLED.write_text(body, encoding="utf-8")
    print(f"Wrote {out_file} ({len(body.encode()) // 1024} KB)")
    return 0




# --- Legacy rules (Pakistan first), kept until the equal rules have been
# checked with a trial run. The nightly job uses them unless EQUAL=1. ---
# Pakistan first: everything that works.
# No "hidebroken" here: Radio Browser checks from Europe and often marks slow
# Pakistani servers broken. Our own test (twice for PK) decides instead.
_Q = "?order=clickcount&reverse=true&limit="
HOME_QUERIES = [
    "/json/stations/bycountrycodeexact/PK" + _Q + "1500",
    "/json/stations/bylanguageexact/urdu" + _Q + "800",
    # Pakistan's other languages, and diaspora stations tagged for Pakistan.
    "/json/stations/bylanguageexact/punjabi" + _Q + "300",
    "/json/stations/bylanguageexact/sindhi" + _Q + "100",
    "/json/stations/bylanguageexact/pashto" + _Q + "200",
    "/json/stations/bylanguageexact/saraiki" + _Q + "50",
    "/json/stations/bylanguageexact/balochi" + _Q + "50",
    "/json/stations/bytagexact/pakistan" + _Q + "300",
    "/json/stations/bytagexact/pakistani" + _Q + "300",
    "/json/stations/bytagexact/urdu" + _Q + "300",
]
HOME_MAX = 450

LEGACY_COUNTRIES = [
    # Gulf + Middle East
    "AE", "SA", "QA", "KW", "OM", "BH", "JO", "LB", "IQ", "EG", "IR",
    # UK, Europe
    "GB", "IE", "DE", "FR", "NL", "BE", "IT", "ES", "PT", "CH", "AT", "SE",
    "NO", "DK", "FI", "PL", "GR", "RO", "CZ", "HU", "UA", "RU", "TR",
    # Americas
    "US", "CA", "MX", "BR", "AR", "CO", "CL", "PE",
    # South + Central Asia
    "IN", "BD", "LK", "NP", "AF", "AZ", "KZ", "UZ",
    # East + South-East Asia, Oceania
    "CN", "HK", "TW", "JP", "KR", "ID", "MY", "SG", "PH", "TH", "VN", "AU", "NZ",
    # Africa
    "NG", "KE", "ZA", "GH", "ET", "MA", "DZ", "TN",
]


def test_all(candidates, second_chance=False):
    """Keeps the streams that play. With [second_chance], Pakistani ones that
    failed are tried once more, slower (their servers are often slow abroad)."""
    with ThreadPoolExecutor(max_workers=64) as pool:
        ok = list(pool.map(stream_works, candidates))
    if second_chance:
        retry = [i for i, good in enumerate(ok)
                 if not good and candidates[i]["country"] == "PK"]
        with ThreadPoolExecutor(max_workers=32) as pool:
            again = list(pool.map(lambda i: stream_works(candidates[i], timeout=25), retry))
        for i, good in zip(retry, again):
            ok[i] = good
    return [s for s, good in zip(candidates, ok) if good]


def main_legacy():
    blocked = set()
    if BLOCKED.exists():
        for line in BLOCKED.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                blocked.add(line.lower())

    started = time.time()
    today = time.strftime("%Y-%m-%d", time.gmtime())

    # Stations listed last night + the watch list: all are tested again.
    previous_list = []
    if OUT.exists():
        try:
            previous_list = json.loads(OUT.read_text(encoding="utf-8")).get("stations", [])
        except ValueError:
            pass
    missing = {}
    if MISSING.exists():
        try:
            missing = json.loads(MISSING.read_text(encoding="utf-8"))
        except ValueError:
            missing = {}
    def is_blocked(st):
        return bool({st.get("id", "").lower(), st.get("name", "").lower(),
                     st.get("url", "").lower()} & blocked)
    watch = [dict(m["station"], _score=-1, _codec="") for m in missing.values()
             if not is_blocked(m["station"])]

    # 1. Pakistan + Urdu (plus your own picks from data/extra_stations.json)
    home = merge_extras(collect(HOME_QUERIES, blocked), blocked)
    home_ids = {s["id"] for s in home}
    home += [w for w in watch if w["country"] == "PK" and w["id"] not in home_ids]
    print(f"Pakistan/Urdu: {len(home)} candidates, testing ...")
    tested = test_all(home, second_chance=True)
    # Every working Pakistani station is kept; the cap only trims the rest
    # (Urdu/Punjabi/Pashto stations from other countries).
    pk = [s for s in tested if s["country"] == "PK"]
    others = [s for s in tested if s["country"] != "PK"][:max(0, HOME_MAX - len(pk))]
    keep = {s["id"] for s in pk + others}
    working = [s for s in tested if s["id"] in keep]
    working_ids = set(keep)
    home_failed = [s["name"] for s in home if s["id"] not in working_ids]
    seen = {s["id"] for s in working}
    print(f"  {len(working)} working")

    # 2. Other countries
    queries = [f"/json/stations/bycountrycodeexact/{cc}?hidebroken=true"
               f"&order=clickcount&reverse=true&limit=90"
               for cc in LEGACY_COUNTRIES]
    world = [s for s in collect(queries, blocked) if s["id"] not in seen]
    # Off-air stations on the watch list are tested again and, if they play,
    # get their place back first. Everything else (including NEW stations
    # that appeared in the directory) competes by popularity, so new
    # channels are found every night.
    world_ids = {s["id"] for s in world}
    watched = [w for w in watch if w["country"] != "PK"
               and w["id"] not in world_ids and w["id"] not in seen]
    world = watched + world
    print(f"World: {len(world)} candidates from {len(LEGACY_COUNTRIES)} countries, testing ...")
    per_country = {}
    for s in test_all(world):
        cc = s["country"]
        if per_country.get(cc, 0) < 40:
            per_country[cc] = per_country.get(cc, 0) + 1
            working.append(s)

    counts = {}
    for s in working:
        counts[s["country"]] = counts.get(s["country"], 0) + 1
    print(f"{len(working)} stations are working, {len(counts)} countries, "
          f"{time.time() - started:.0f} s")
    print("Per country: " + ", ".join(f"{k} {v}" for k, v in
                                      sorted(counts.items(), key=lambda x: -x[1])))
    # Watch list: listed before (or already watched) but not playing tonight.
    working_now = {s["id"] for s in working}
    new_missing = {}
    for st in previous_list:
        if st["id"] not in working_now and not is_blocked(st):
            since = missing.get(st["id"], {}).get("since", today)
            new_missing[st["id"]] = {"since": since, "station": st}
    for sid, m in missing.items():
        if sid in working_now or sid in new_missing or is_blocked(m["station"]):
            continue
        new_missing[sid] = m
    cutoff = time.strftime("%Y-%m-%d", time.gmtime(time.time() - MISSING_DAYS * 86400))
    new_missing = {k: v for k, v in new_missing.items() if v["since"] >= cutoff}
    back = len([sid for sid in missing if sid in working_now])
    listed_before = {st["id"] for st in previous_list} | set(missing)
    new_tonight = [st["name"] for st in working if st["id"] not in listed_before]
    print(f"New stations tonight: {len(new_tonight)}")
    print(f"Watch list: {len(new_missing)} stations off air, {back} came back tonight")


    # Small report for the developers: what was offered, what worked.
    REPORT.write_text(json.dumps({
        "date": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
        "home_candidates": len(home),
        "home_working": len(working_ids),
        "home_pakistan": len(pk),
        "home_queries": {k: v for k, v in QUERY_COUNTS.items()
                         if "bycountrycodeexact" not in k or k.endswith("/PK")},
        "home_failed_sample": home_failed[:200],
        "per_country": dict(sorted(counts.items(), key=lambda x: -x[1])),
        "off_air_watch": len(new_missing),
        "came_back_tonight": back,
        "new_tonight": len(new_tonight) if previous_list else 0,
        "new_tonight_sample": new_tonight[:100] if previous_list else [],
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    previous = 0
    if OUT.exists():
        try:
            previous = json.loads(OUT.read_text(encoding="utf-8")).get("count", 0)
        except ValueError:
            pass
    if len(working) < 10 or len(working) < previous * 0.6:
        # Something is wrong (network issue, directory down). A bad night must
        # never empty the app: keep the previous list.
        print(f"Only {len(working)} working (previous list: {previous}) - "
              "keeping the previous list.", file=sys.stderr)
        return 0 if OUT.exists() else 1

    for s in working:
        s.pop("_score", None)
        s.pop("_codec", None)

    # off_air: saved stations the app may still show, with the date they
    # went off air (the app removes them from favourites after 10 days, and
    # at once when a station isn't here at all = gone for 30+ days).
    off_air = {k: v["since"] for k, v in new_missing.items()}
    body = json.dumps({"version": 2, "count": len(working), "stations": working,
                       "off_air": off_air},
                      ensure_ascii=False, separators=(",", ":"))
    DATA.mkdir(exist_ok=True)
    OUT.write_text(body, encoding="utf-8")
    MISSING.write_text(json.dumps(new_missing, ensure_ascii=False, indent=0,
                                  sort_keys=True), encoding="utf-8")
    if BUNDLED.parent.exists():  # only inside the app repo
        BUNDLED.write_text(body, encoding="utf-8")
    print(f"Wrote {OUT} ({len(body.encode()) // 1024} KB)")
    return 0


if __name__ == "__main__":
    if os.environ.get("EQUAL") or os.environ.get("TRIAL"):
        sys.exit(main())
    sys.exit(main_legacy())
