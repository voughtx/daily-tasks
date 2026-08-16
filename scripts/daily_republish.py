#!/usr/bin/env python3
# masked build — config secrets se aata hai (VERBOSE_LOGS=1 debug)

import os, re, json, time, math, sys
import requests
import pymongo

import base64 as _b64
def _cfg():
    raw = os.environ.get("HL_CFG", "")
    if not raw:
        sys.exit("❌ HL_CFG secret missing — repo settings me daalo")
    return json.loads(_b64.b64decode(raw).decode())
_CFG = _cfg()
CDN_BASE    = _CFG["cdn_base"]
WORKER_BASE = _CFG["worker_base"]
SEG_TIME_FALLBACK = 12.0
DB_NAME, COLL = "video_database", "segments"

MONGO_URI      = os.environ.get("MONGO_URI", "")
PUBLISH_SECRET = os.environ.get("PUBLISH_SECRET", "")
CF_ZONE_ID     = os.environ.get("CF_ZONE_ID", "")
CF_PURGE_TOKEN = os.environ.get("CF_PURGE_TOKEN", "")

_missing = [k for k, v in {
    "MONGO_URI": MONGO_URI, "PUBLISH_SECRET": PUBLISH_SECRET,
    "CF_ZONE_ID": CF_ZONE_ID, "CF_PURGE_TOKEN": CF_PURGE_TOKEN,
}.items() if not v]
if _missing:
    sys.exit("❌ Missing env secrets: " + ", ".join(_missing))

# --- QUIET LOGS (public repo privacy): VERBOSE_LOGS=1 se full detail; default masked ---
VERBOSE = os.environ.get("VERBOSE_LOGS", "").strip().lower() in ("1", "true", "yes")
def mm(mid):
    s = str(mid)
    return (s[:4] + "…") if len(s) > 4 else s

# --- Zone sanity check: galat ZONE_ID pe purge API "OK" dikhake silent no-op hoti hai ---
try:
    zr = requests.get(f"https://api.cloudflare.com/client/v4/zones/{CF_ZONE_ID}",
                      headers={"Authorization": f"Bearer {CF_PURGE_TOKEN}"}, timeout=20)
    zname = (((zr.json() or {}).get("result")) or {}).get("name")
    print("CF zone check: " + (f"{zr.status_code} name={zname}" if VERBOSE else "OK"), flush=True)
    if zname != _CFG["zone_name"]:
        sys.exit(f"❌ CF_ZONE_ID dusre zone ka hai (mila: {zname}) — GitHub secret theek karo!")
except SystemExit:
    raise
except Exception as e:
    print("⚠️ zone check err (continue anyway):", repr(e), flush=True)

RUN_VER = str(int(time.time()))
TARGET_MOVIE = os.environ.get("TARGET_MOVIE_ID", "").strip()  # single-movie manual run
WAF_BYPASS_HEADERS = {"Referer": _CFG["pages"] + "/",
                      "Origin": _CFG["pages"]}

print(("=== DAILY REPUBLISH START | RUN_VER=" + RUN_VER + " ===") if VERBOSE else "=== DAILY REPUBLISH START ===", flush=True)
coll = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=15000)[DB_NAME][COLL]

raw_ids = coll.distinct("movie_id")
movie_ids = sorted({str(x) for x in raw_ids})
print(f"Mongo movies: {len(movie_ids)}" + (f" -> {movie_ids}" if VERBOSE else "") + "\n", flush=True)

if TARGET_MOVIE:
    print("🎯 TARGET MODE: single movie" + (f" {TARGET_MOVIE}" if VERBOSE else "") + "\n", flush=True)
    movie_ids = [TARGET_MOVIE]


def to_cdn(worker_url):
    u = re.sub(r"^https?://[^/]+/tg/", CDN_BASE + "/segments/", worker_url)
    u = re.sub(r"/segments/bot(\d+)/", r"/segments/\1/", u)  # botN -> N
    return u


def find_good_doc(mid):
    """Movie ke newest-se-oldest docs scan karke pehla doc do jisme init+segments ho."""
    orq = [{"movie_id": mid}]
    if mid.isdigit():
        orq.append({"movie_id": int(mid)})
    for doc in coll.find({"$or": orq}).sort("timestamp", -1).limit(10):
        if doc.get("segments"):
            # TS (mpegts) me init nahi hota; fMP4 me init zaroori
            if doc.get("container") == "mpegts" or (doc.get("init") or {}).get("worker_url"):
                return doc
    return None


def build_playlist(doc):
    segs = doc.get("segments") or []
    init = doc.get("init") or {}
    container = (doc.get("container") or "fmp4").lower()
    has_init = bool(init.get("worker_url")) and container != "mpegts"
    cdn_init = to_cdn(init["worker_url"]) if has_init else None
    cdn_segs = [to_cdn(s["worker_url"]) for s in segs]
    durs = []
    for s in segs:
        d = s.get("d")
        durs.append(float(d) if isinstance(d, (int, float)) and 0.05 < d < 600
                    else float(doc.get("segment_time") or SEG_TIME_FALLBACK))
    target = max(1, math.ceil(max(durs)))
    L = ["#EXTM3U", "#EXT-X-VERSION:7", f"#EXT-X-TARGETDURATION:{target}",
         "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-PLAYLIST-TYPE:VOD"]
    if doc.get("split_by_time") is not True:
        L.append("#EXT-X-INDEPENDENT-SEGMENTS")
    if has_init:
        L.append(f'#EXT-X-MAP:URI="{cdn_init}"')
    L.append("")
    for d, u in zip(durs, cdn_segs):
        L.append(f"#EXTINF:{d:.3f},")
        L.append(u)
    L.append("#EXT-X-ENDLIST")
    return "\n".join(L) + "\n", len(cdn_segs)


def build_manifest(mid, doc, playlist_url):
    return {
        "manifest_version": 1, "content_type": "movie",
        "movie_id": str(mid), "title": doc.get("title") or f"Movie {mid}",
        "playlist_url": playlist_url, "cdn_base_url": CDN_BASE,
        "workers": [WORKER_BASE],
        "sources": [{
            "id": f"{mid}-main", "quality": "auto", "language": "default",
            "type": "hls", "playlist_url": playlist_url,
            "segments_count": len(doc.get("segments") or []),
        }],
        "subtitles": [],
        "player": {
            "theme_color": "#9638ee", "show_debug": False, "show_ended_screen": False,
            "quality_selector": True, "language_selector": True, "subtitle_selector": True,
            "resume_playback": True, "autoplay_next": False,
            "error_screen": {
                "enabled": True, "accent": "#9638ee",
                "title": "Playback Error",
                "subtitle": "This video couldn't be loaded right now.",
                "button_text": "Send Report", "sending_text": "Sending...",
                "sent_title": "Report Sent", "sent_note": "Thank you! Our team has been notified.",
                "send_retry_text": "Retry", "note_text": "Please try again later.",
                "button_text_color": "#ffffff", "sent_button_text": "Report Sent",
                "sent_return_seconds": 5,
                "error_types": {
                    "ERR_MANIFEST_NOT_FOUND": {
                        "icon": "screen_x", "title": "Video Not Available",
                        "subtitle": "This video may have been removed, or is temporarily unavailable.",
                        "button": False, "note": False,
                    },
                    "ERR_INVALID_LINK": {
                        "icon": False, "title": "404",
                        "subtitle": "The video you're looking for is not available or has expired.",
                        "button": False, "note": False,
                    },
                    "ERR_SECURITY_BLOCKED": {
                        "icon": "alert_triangle", "title": "We're Sorry!",
                        "subtitle": "We can't find the file you are looking for. It may have been deleted by the owner or removed due to a copyright violation.",
                        "button": False, "note": False, "code_label": "410",
                    },
                },
                "bg_color": "#060608", "bg_opacity": 1, "hide_player": True,
                "text_color": "#e8e8ea", "success_color": "#22c55e",
                "icon": "cloud_off",
            },
        },
        "security": {"anti_debug": False, "anti_debug_screen": "error", "iframe_only": False,
                     "direct_open_error": False, "allowed_parent_domains": []},
    }


def purge_manifest(mid):
    # PREFIX purge (files wali is zone pe silently fail hui — Age climbing proof).
    # Purge host = CDN domain; saare manifests (query variants samet) clear.
    host = CDN_BASE.split("://", 1)[1]
    r = requests.post(
        f"https://api.cloudflare.com/client/v4/zones/{CF_ZONE_ID}/purge_cache",
        headers={"Authorization": f"Bearer {CF_PURGE_TOKEN}",
                 "Content-Type": "application/json"},
        json={"prefixes": [host + "/manifests/"]}, timeout=30)
    ok = r.status_code == 200 and (r.json() or {}).get("success")
    return ok, f"{r.status_code} {r.text[:150]}"


def bare_check(mid):
    """Bare manifest URL (NO query) pado — REAL user path. Yeh GET edge ko
    origin se fresh seed bhi karti hai aur batati hai real path fresh hai kya."""
    try:
        r = requests.get(f"{CDN_BASE}/manifests/{mid}.json",
                         headers=WAF_BYPASS_HEADERS, timeout=30)
        mf = r.json()
        ok = RUN_VER in (mf.get("playlist_url") or "")
        ray = (r.headers.get("CF-RAY") or "").split("-")[-1]
        print(("bare check: CF=" + str(r.headers.get('CF-Cache-Status')) + " Age=" + str(r.headers.get('Age')) + " pop=" + ray + " playlist_new=" + str(ok)) if VERBOSE else ("bare check: " + ("fresh" if ok else "stale")), flush=True)
        return ok
    except Exception as e:
        print("bare check err:", repr(e), flush=True)
        return False


def verify_fresh(mid, attempts=5, gap=4):
    """Cache-bust query se manifest pado — stale edge ki race nahi. Har attempt fresh."""
    for a in range(1, attempts + 1):
        try:
            r = requests.get(f"{CDN_BASE}/manifests/{mid}.json?_gh={RUN_VER}&a={a}",
                             headers=WAF_BYPASS_HEADERS, timeout=30)
            mf = r.json()
            fa = mf.get("fallback_auth") or {}
            ttl = int(fa.get("exp", 0)) - int(time.time())
            new_ok = RUN_VER in (mf.get("playlist_url") or "")
            print((f"verify try {a}: playlist_new={new_ok} | token ttl ~{ttl}s") if VERBOSE else f"verify try {a}: ok", flush=True)
            if new_ok and ttl > 23 * 3600:
                return True, ttl
        except Exception as e:
            print(f"verify try {a}: err {type(e).__name__}", flush=True)
        time.sleep(gap)
    return False, 0


ok_list, skip_list, fail_list = [], [], []

for mid in movie_ids:
    print(f"\n----- movie {mm(mid)} -----", flush=True)
    try:
        doc = find_good_doc(mid)
        if not doc:
            msg = "Mongo me init/segments data nahi"
            title = ""
            try:
                anydoc = coll.find_one({"$or": [{"movie_id": mid},
                                                {"movie_id": int(mid) if mid.isdigit() else mid}]})
                title = (anydoc or {}).get("title") or ""
            except Exception:
                pass
            print(f"SKIP: {msg}" + (f" (title: {title})" if VERBOSE else ""), flush=True)
            # TARGET (single) mode me yeh FAIL hai — user ne specifically isi movie ke liye button dabaya
            (fail_list if TARGET_MOVIE else skip_list).append((mid, msg if TARGET_MOVIE else title))
            continue

        playlist, nseg = build_playlist(doc)
        playlist_path = f"playlists/{mid}.v{RUN_VER}.m3u8"
        playlist_url = f"{CDN_BASE}/{playlist_path}"
        manifest = build_manifest(mid, doc, playlist_url)

        payload = {
            "movie_id": mid,
            "playlist_path": playlist_path,
            "manifest_path": f"manifests/{mid}.json",
            "playlist": playlist,
            "manifest": manifest,
            "playlist_cache_seconds": 31536000,
            "manifest_cache_seconds": 600,
        }
        pr = requests.post(f"{WORKER_BASE}/publish",
                           headers={"X-Publish-Secret": PUBLISH_SECRET,
                                    "Content-Type": "application/json"},
                           data=json.dumps(payload), timeout=90)
        pj = {}
        try:
            pj = pr.json()
        except Exception:
            pass
        if pr.status_code != 200 or not pj.get("ok"):
            raise RuntimeError(f"publish fail: {pr.status_code} {pr.text[:200]}")
        print(f"publish OK | {nseg} segs" + (f" | {playlist_path}" if VERBOSE else ""), flush=True)

        # purge + bare-path self-heal (tiered-cache stale re-seed race ka ilaaj):
        # purge -> 6s wait -> bare GET (real path seed+check) -> stale ho toh
        # dobara purge. Max 3 rounds.
        healed = False
        for rnd in range(1, 4):
            pok, pmsg = purge_manifest(mid)
            print(f"purge round {rnd}:", "OK" if pok else ("FAIL (" + pmsg + ")" if VERBOSE else "FAIL"), flush=True)
            if not pok:
                raise RuntimeError("purge API fail")
            time.sleep(10)
            if bare_check(mid):
                healed = True
                break
        if not healed:
            # Bada issue NAHI hai: origin pe naya manifest hai (neeche verify hoga),
            # purana playlist R2 me exist karta hai + uska token valid hai, aur Cache
            # Rule ab manifests ka Edge TTL 10-min hai => thodi der me khud fresh.
            print("⚠️ WARN: bare path abhi stale — max 2h TTL + 2x daily run = khud heal; run RED nahi karenge", flush=True)

        vok, ttl = verify_fresh(mid)
        if not vok:
            raise RuntimeError("verify weak (origin pe bhi fresh nahi)")

        ok_list.append((mid, nseg, ttl))
    except Exception as e:
        print("❌ FAIL:", repr(e) if VERBOSE else type(e).__name__, flush=True)
        fail_list.append((mid, repr(e)[:120] if VERBOSE else type(e).__name__))

print("\n================ SUMMARY ================", flush=True)
for mid, nseg, ttl in ok_list:
    print(f"{mm(mid)}: OK | {nseg} segs | ttl ~{ttl}s", flush=True)
for mid, title in skip_list:
    print(f"{mm(mid)}: SKIP (data nahi)" + (f" | {title}" if VERBOSE else ""), flush=True)
for mid, err in fail_list:
    print(f"{mm(mid)}: FAIL | {err}", flush=True)
print(f"TOTAL: OK={len(ok_list)} | SKIP={len(skip_list)} | FAIL={len(fail_list)} "
      f"(Mongo total {len(movie_ids)})", flush=True)
print("==========================================", flush=True)

# SKIP se run RED nahi hota (un movies me dekhne ko hai hi nahi).
# RED sirf tab jab data wali movie publish/purge/verify me fail ho.
if fail_list:
    sys.exit(1)
print(f"✅ DONE — {len(ok_list)} movies fresh for ~25h | {len(skip_list)} skipped (incomplete data)", flush=True)
