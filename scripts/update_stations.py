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
import hashlib
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
PER_COUNTRY_MAX = 50     # working stations kept per country (same for all; 50 keeps the list ~1.4 MB)
# Per-country files (SHARDS=1): the app loads a country's full file only for
# the countries the listener uses; stations.json stays the small base list
# (BASE_MAX per country) for everything else.
SHARD_FETCH = 400
SHARD_MAX = 200
BASE_MAX = 50
SHARD_DIR = DATA / "c"
INDEX = DATA / "index.json"
RETRY_PER_COUNTRY = 20   # failed streams tried again, slower, per country
TEST_WORKERS = 128
# ALL_COUNTRIES=1: besides COUNTRIES, every country the directory knows with at
# least MIN_DIRECTORY_STATIONS stations (all countries equal), plus every
# country we listed before (never dropped because the directory was busy).
MIN_DIRECTORY_STATIONS = 10
# TAIL=1 (plan "unlimited stations", 20 Oct 2026): every station of a country,
# not only the top SHARD_MAX. The top part (page 1, data/c/<cc>.json) is
# tested every night as before; the rest ("tail") is tested once a week
# (1/7 of it each night) plus up to TAIL_MAX_TESTS stations never tested
# before, and kept while its last good test is at most TAIL_DAYS old. Tail
# pages (data/c/<cc>/<n>.json, TAIL_PAGE stations each), a search file per
# country (data/c/<cc>/names.json) and the test memory (data/tail_state.json)
# are published to Cloudflare only - never committed to git (size).
TAIL_PAGE = 200
TAIL_MAX_TESTS = 25000
TAIL_DAYS = 8
DIRECTORY_ALL = 100000   # "every station" per country from the directory
CDN = "https://tunely-data.pages.dev/data/"
# Not real listening countries in the directory (Antarctica; "US minor
# islands" = mislabelled US stations) and North Korea (state media under
# sanctions; Asim decides, 19 Oct). Never listed.
NEVER_COUNTRIES = {"AQ", "UM", "KP"}
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


def fetch_json(path, timeout=30):
    last = None
    for base in MIRRORS:
        try:
            req = urllib.request.Request(base + path, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as r:
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
    results = []
    failed = []
    for q in queries:
        time.sleep(0.3)  # be gentle with the free directory
        try:
            results.append(fetch_json(q))
            QUERY_COUNTS[q.split("?")[0]] = len(results[-1])
        except RuntimeError as e:
            print(f"  failed {q.split('?')[0]}: {e}", file=sys.stderr)
            failed.append(q)
    # The directory is sometimes busy: wait, then try the failed ones again,
    # slower and one mirror at a time.
    for attempt in (1, 2, 3):
        if not failed:
            break
        time.sleep(30 * attempt)
        again = []
        for q in failed:
            try:
                results.append(fetch_json(q, timeout=90))
                QUERY_COUNTS[q.split("?")[0]] = len(results[-1])
            except RuntimeError:
                again.append(q)
        failed = again
    for q in failed:
        QUERY_COUNTS[q.split("?")[0]] = "failed"
        print(f"  skipped {q.split('?')[0]} (directory not reachable)", file=sys.stderr)
    for rows in results:
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


def all_countries():
    countries = list(COUNTRIES)
    try:
        for c in fetch_json("/json/countries", timeout=60):
            cc = (c.get("iso_3166_1") or "").upper()
            if (len(cc) == 2 and cc.isalpha() and cc not in countries
                    and cc not in NEVER_COUNTRIES
                    and int(c.get("stationcount") or 0) >= MIN_DIRECTORY_STATIONS):
                countries.append(cc)
    except Exception as e:
        print(f"  country list unavailable tonight: {e}", file=sys.stderr)
    if SHARD_DIR.exists():
        for p in sorted(SHARD_DIR.glob("*.json")):
            cc = p.stem.upper()
            if len(cc) == 2 and cc not in countries and cc not in NEVER_COUNTRIES:
                countries.append(cc)
    print(f"{len(countries)} countries ({len(countries) - len(COUNTRIES)} beyond the core list)")
    return countries


def test_streams(candidates, timeout=10, workers=TEST_WORKERS):
    """True/False per candidate: does the stream really play?"""
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda st: stream_works(st, timeout=timeout), candidates))


def _bucket(sid):
    return int(hashlib.md5(sid.encode("utf-8")).hexdigest(), 16) % 7


def _fetch_cdn(path, timeout=60):
    req = urllib.request.Request(CDN + path, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _public(st):
    return {k: v for k, v in st.items() if not k.startswith("_")}


def build_tail(per, tail_pool, shard_dir, today, failed_countries, report_file):
    """Pages 2, 3 ... of every country (the stations beyond the nightly top
    list) and a search file per country. Returns (totals, pages)."""
    started = time.time()
    try:
        state = _fetch_cdn("tail_state.json").get("ok", {})
    except Exception as e:
        print(f"  no tail memory yet ({e}) - starting fresh", file=sys.stderr)
        state = {}
    head_ids = {st["id"] for lst in per.values() for st in lst}
    pool = {cc: [st for st in lst if st["id"] not in head_ids]
            for cc, lst in tail_pool.items()}
    day = int(time.strftime("%j", time.gmtime())) % 7
    due, unseen_by_cc = [], {}
    for cc, lst in pool.items():
        for st in lst:
            if st["id"] in state:
                if _bucket(st["id"]) == day:
                    due.append(st)
            else:
                unseen_by_cc.setdefault(cc, []).append(st)
    # Never-tested stations: fair between countries (round robin by rank).
    unseen, rank = [], 0
    while len(unseen) < TAIL_MAX_TESTS and any(
            rank < len(v) for v in unseen_by_cc.values()):
        for v in unseen_by_cc.values():
            if rank < len(v):
                unseen.append(v[rank])
        rank += 1
    tests = due + unseen[:max(0, TAIL_MAX_TESTS - len(due))]
    print(f"Tail: {sum(len(v) for v in pool.values())} stations beyond the top lists, "
          f"testing {len(due)} due this week + {len(tests) - len(due)} new ...")
    for st, good in zip(tests, test_streams(tests, timeout=8, workers=192)):
        if good:
            state[st["id"]] = today
        else:
            state.pop(st["id"], None)
    cutoff = time.strftime("%Y-%m-%d", time.gmtime(time.time() - TAIL_DAYS * 86400))
    state = {k: v for k, v in state.items() if v >= cutoff}

    totals, pages, kept_all = {}, {}, 0
    for cc in sorted(set(per) | set(pool)):
        head = per.get(cc, [])
        if not head:
            continue
        folder = shard_dir / cc.lower()
        if cc in failed_countries:
            # Directory didn't answer for this country tonight: keep last
            # night's pages as they were.
            try:
                names = _fetch_cdn(f"c/{cc.lower()}/names.json")
                n_pages = int(names.get("pages", 1))
                folder.mkdir(parents=True, exist_ok=True)
                for n in range(2, n_pages + 1):
                    (folder / f"{n}.json").write_text(json.dumps(
                        _fetch_cdn(f"c/{cc.lower()}/{n}.json"),
                        ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
                (folder / "names.json").write_text(json.dumps(
                    names, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
                totals[cc] = int(names.get("total", len(head)))
                pages[cc] = n_pages
                continue
            except Exception:
                pass
        tail = [_public(st) for st in pool.get(cc, []) if st["id"] in state]
        kept_all += len(tail)
        chunks = [tail[i:i + TAIL_PAGE] for i in range(0, len(tail), TAIL_PAGE)]
        n_pages = 1 + len(chunks)
        total = len(head) + len(tail)
        totals[cc], pages[cc] = total, n_pages
        folder.mkdir(parents=True, exist_ok=True)
        for p in folder.glob("*.json"):
            p.unlink()
        for i, chunk in enumerate(chunks):
            n = i + 2
            (folder / f"{n}.json").write_text(json.dumps(
                {"version": 1, "country": cc, "page": n, "pages": n_pages,
                 "total": total, "stations": chunk},
                ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        rows = [[st["id"], st["name"], st.get("city", ""), " ".join(st.get("tags", [])), 1]
                for st in head]
        for i, chunk in enumerate(chunks):
            rows += [[st["id"], st["name"], st.get("city", ""),
                      " ".join(st.get("tags", [])), i + 2] for st in chunk]
        (folder / "names.json").write_text(json.dumps(
            {"version": 1, "country": cc, "total": total, "pages": n_pages,
             "rows": rows}, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8")
    for d in shard_dir.iterdir():  # countries with nothing left
        if d.is_dir() and d.name.upper() not in totals:
            for p in d.glob("*.json"):
                p.unlink()
            d.rmdir()
    (shard_dir.parent / "tail_state.json").write_text(json.dumps(
        {"date": today, "ok": state}, separators=(",", ":")), encoding="utf-8")
    print(f"Tail: {kept_all} more stations kept, {time.time() - started:.0f} s")
    try:  # add to tonight's report
        rep = json.loads(report_file.read_text(encoding="utf-8"))
        rep.update({"tail_tested": len(tests), "tail_kept": kept_all,
                    "tail_seconds": round(time.time() - started),
                    "total_all_pages": sum(totals.values())})
        report_file.write_text(json.dumps(rep, ensure_ascii=False, indent=1),
                               encoding="utf-8")
    except Exception:
        pass
    return totals, pages


def main():
    blocked = set()
    if BLOCKED.exists():
        for line in BLOCKED.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                blocked.add(line.lower())

    trial = bool(os.environ.get("TRIAL"))
    shards = bool(os.environ.get("SHARDS"))
    cap = SHARD_MAX if shards else PER_COUNTRY_MAX
    fetch = SHARD_FETCH if shards else PER_COUNTRY_FETCH
    shard_dir = TRIAL_DIR / "c" if trial else SHARD_DIR
    index_file = TRIAL_DIR / "index.json" if trial else INDEX
    out_file = TRIAL_DIR / "stations.json" if trial else OUT
    report_file = TRIAL_DIR / "report.json" if trial else REPORT
    missing_file = TRIAL_DIR / "missing.json" if trial else MISSING

    started = time.time()
    countries = all_countries() if os.environ.get("ALL_COUNTRIES") else list(COUNTRIES)
    today = time.strftime("%Y-%m-%d", time.gmtime())

    # Stations listed last night + the watch list: all are tested again.
    previous_list = []
    if OUT.exists():
        try:
            previous_list = json.loads(OUT.read_text(encoding="utf-8")).get("stations", [])
        except ValueError:
            pass
    # With per-country files, last night's full lists are the reference.
    if SHARD_DIR.exists():
        full = []
        for p in sorted(SHARD_DIR.glob("*.json")):
            try:
                full += json.loads(p.read_text(encoding="utf-8")).get("stations", [])
            except ValueError:
                pass
        if full:
            seen_ids = {st["id"] for st in full}
            previous_list = full + [st for st in previous_list if st["id"] not in seen_ids]
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
    tail_on = shards and bool(os.environ.get("TAIL"))
    limit = DIRECTORY_ALL if tail_on else fetch
    queries = [f"/json/stations/bycountrycodeexact/{cc}?order=clickcount"
               f"&reverse=true&limit={limit}" for cc in countries]
    directory = merge_extras(collect(queries, blocked), blocked)
    by_cc = {}
    for st in directory:
        by_cc.setdefault(st["country"], []).append(st)
    # A country the directory didn't answer for tonight: test last night's
    # stations of that country again instead (never drop a whole country
    # because of a busy directory).
    fallback = [cc for cc in countries
                if QUERY_COUNTS.get(f"/json/stations/bycountrycodeexact/{cc}") == "failed"]
    known = {st["id"] for st in directory}
    for i, st in enumerate(previous_list):
        if st.get("country") in fallback and st["id"] not in known and not is_blocked(st):
            by_cc.setdefault(st["country"], []).append(
                dict(st, _score=-i, _codec=""))  # keeps last night's order
            known.add(st["id"])
    for cc, lst in by_cc.items():
        lst.sort(key=lambda st: (0 if st.get("_extra") else 1, -st["_score"]))
    # How many usable stations the directory has per country (all of them
    # when TAIL is on). The nightly head test takes the first `fetch`; the
    # rest is the tail, tested on a weekly rotation.
    directory_sizes = {cc: len(lst) for cc, lst in by_cc.items()}
    tail_pool = {}
    if tail_on:
        for cc in list(by_cc):
            tail_pool[cc] = by_cc[cc][fetch:]
            by_cc[cc] = by_cc[cc][:fetch]
    ids = {st["id"] for st in directory}
    for m in missing.values():
        st = m["station"]
        if st["id"] in ids or is_blocked(st) or st.get("country") not in countries:
            continue
        lst = by_cc.setdefault(st["country"], [])
        n_extra = sum(1 for x in lst if x.get("_extra"))
        lst.insert(n_extra, dict(st, _score=-1, _codec=""))
        ids.add(st["id"])

    candidates = [st for cc in countries for st in by_cc.get(cc, [])]
    print(f"{len(candidates)} candidates from {len(countries)} countries, testing ...")
    ok = dict(zip((st["id"] for st in candidates), test_streams(candidates)))

    # 2. Second, slower try for countries that are not full yet.
    retry = []
    for cc in countries:
        lst = by_cc.get(cc, [])
        if sum(ok[st["id"]] for st in lst) >= cap:
            continue
        retry += [st for st in lst if not ok[st["id"]]][:RETRY_PER_COUNTRY]
    print(f"Second try for {len(retry)} slow streams ...")
    for st, good in zip(retry, test_streams(retry, timeout=25, workers=64)):
        ok[st["id"]] = good
    recovered = sum(1 for st in retry if ok[st["id"]])

    # 3. Keep up to PER_COUNTRY_MAX working stations per country. Stability
    #    first: a station we already listed that still plays is never dropped
    #    just for the cap (listeners may have it saved); new stations fill
    #    the free places. Hand-picked extras always stay.
    listed = {st["id"] for st in previous_list} | set(missing)
    working, failed_sample = [], {}
    for cc in countries:
        lst = by_cc.get(cc, [])
        keep = {st["id"] for st in lst
                if ok[st["id"]] and (st.get("_extra") or st["id"] in listed)}
        for st in lst:
            if len(keep) >= cap:
                break
            if ok[st["id"]]:
                keep.add(st["id"])
        for st in lst:
            if st["id"] in keep:
                working.append(st)
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
        "directory_failed_used_last_night": fallback,
        "per_country": dict(sorted(counts.items(), key=lambda x: -x[1])),
        "shards": shards,
        "off_air_watch": len(new_missing),
        "came_back_tonight": back,
        "new_tonight": len(new_tonight) if previous_list else 0,
        "new_tonight_sample": new_tonight[:100] if previous_list else [],
        "failed_sample": {k: v[:15] for k, v in failed_sample.items()},
        "queries": QUERY_COUNTS,
        "directory_sizes": dict(sorted(directory_sizes.items(), key=lambda x: -x[1])),
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
    if shards:
        # Per-country files (full lists) + a tiny index; the base list keeps
        # the first BASE_MAX stations of every country.
        shard_dir.mkdir(parents=True, exist_ok=True)
        per = {}
        for st in working:
            per.setdefault(st["country"], []).append(st)
        totals = {cc: len(lst) for cc, lst in per.items()}
        pages = {cc: 1 for cc in per}
        if tail_on:
            totals, pages = build_tail(per, tail_pool, shard_dir, today, fallback,
                                       report_file)
        for cc, lst in per.items():
            (shard_dir / f"{cc.lower()}.json").write_text(json.dumps(
                {"version": 2, "country": cc, "count": len(lst),
                 "total": totals.get(cc, len(lst)), "pages": pages.get(cc, 1),
                 "stations": lst},
                ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        for p in shard_dir.glob("*.json"):  # countries with nothing left
            if p.stem.upper() not in per:
                p.unlink()
        # "countries" = every working station of the country (all pages);
        # "pages" = how many page files it has (1 = only data/c/<cc>.json).
        index_file.write_text(json.dumps(
            {"version": 3, "date": today,
             "countries": {cc: totals.get(cc, len(per[cc])) for cc in sorted(per)},
             "pages": {cc: pages[cc] for cc in sorted(per) if pages.get(cc, 1) > 1}},
            separators=(",", ":")), encoding="utf-8")
        # The base list never loses a station it had (app versions without
        # per-country files may have it saved); new ones up to BASE_MAX.
        prev_base = set()
        if OUT.exists():
            try:
                prev_base = {st["id"] for st in json.loads(
                    OUT.read_text(encoding="utf-8")).get("stations", [])}
            except ValueError:
                pass
        base = [st for cc in countries
                for i, st in enumerate(per.get(cc, []))
                if i < BASE_MAX or st["id"] in prev_base]
        print(f"Wrote {len(per)} country files ({len(working)} stations), "
              f"base list {len(base)}")
    else:
        base = working
    body = json.dumps({"version": 2, "count": len(base), "stations": base,
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
