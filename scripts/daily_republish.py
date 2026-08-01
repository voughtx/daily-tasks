#!/usr/bin/env python3
# Homelander — DAILY AUTO-REPUBLISH v2 (GitHub Actions, roz 03:30 IST)
# v2 fixes:
#   1. Movie ke MULTIPLE docs ho to naya-se-naya doc jisme init+segments HO wo chuno
#      (purane v1 me latest doc me data na ho to FAIL aata tha — ab SKIP)
#   2. Verify ab cache-bust query se karta hai (US/global edge pe purana manifest
#      dikhne wali race khatam) + retry loop
#   3. SKIP (Mongo me data hi nahi) ko run FAIL nahi karta — sirf real errors pe red
#
# ENV (repo secrets se): MONGO_URI, PUBLISH_SECRET, CF_ZONE_ID, CF_PURGE_TOKEN

import os, re, json, time, math, sys
import requests
import pymongo

CDN_BASE    = "https://homeleni.dpdns.org"
WORKER_BASE = "https://v2.7homelander.workers.dev"
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

RUN_VER = str(int(time.time()))
WAF_BYPASS_HEADERS = {"Referer": "https://v-player.pages.dev/",
                      "Origin": "https://v-player.pages.dev"}

print(f"=== DAILY REPUBLISH v2 START | RUN_VER={RUN_VER} ===", flush=True)
coll = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=15000)[DB_NAME][COLL]

raw_ids = coll.distinct("movie_id")
movie_ids = sorted({str(x) for x in raw_ids})
print(f"Mongo me movies mili: {len(movie_ids)} -> {movie_ids}\n", flush=True)


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
        if (doc.get("init") or {}).get("worker_url") and doc.get("segments"):
            return doc
    return None


def build_playlist(doc):
    segs = doc.get("segments") or []
    init = doc.get("init") or {}
    cdn_init = to_cdn(init["worker_url"])
    cdn_segs = [to_cdn(s["worker_url"]) for s in segs]
    durs = []
    for s in segs:
        d = s.get("d")
        durs.append(float(d) if isinstance(d, (int, float)) and 0.05 < d < 600
                    else float(doc.get("segment_time") or SEG_TIME_FALLBACK))
    target = max(1, math.ceil(max(durs)))
    L = ["#EXTM3U", "#EXT-X-VERSION:7", f"#EXT-X-TARGETDURATION:{target}",
         "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-PLAYLIST-TYPE:VOD",
         "#EXT-X-INDEPENDENT-SEGMENTS", f'#EXT-X-MAP:URI="{cdn_init}"', ""]
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
    r = requests.post(
        f"https://api.cloudflare.com/client/v4/zones/{CF_ZONE_ID}/purge_cache",
        headers={"Authorization": f"Bearer {CF_PURGE_TOKEN}",
                 "Content-Type": "application/json"},
        json={"files": [f"{CDN_BASE}/manifests/{mid}.json"]}, timeout=30)
    ok = r.status_code == 200 and (r.json() or {}).get("success")
    return ok, f"{r.status_code} {r.text[:150]}"


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
            print(f"verify try {a}: playlist_new={new_ok} | token ttl ~{ttl}s", flush=True)
            if new_ok and ttl > 23 * 3600:
                return True, ttl
        except Exception as e:
            print(f"verify try {a}: err {e!r}", flush=True)
        time.sleep(gap)
    return False, 0


ok_list, skip_list, fail_list = [], [], []

for mid in movie_ids:
    print(f"\n----- movie {mid} -----", flush=True)
    try:
        doc = find_good_doc(mid)
        if not doc:
            title = ""
            try:
                anydoc = coll.find_one({"$or": [{"movie_id": mid},
                                                {"movie_id": int(mid) if mid.isdigit() else mid}]})
                title = (anydoc or {}).get("title") or ""
            except Exception:
                pass
            print(f"SKIP: Mongo me init/segments wala doc nahi (title: {title})", flush=True)
            skip_list.append((mid, title))
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
            "playlist_cache_seconds": 86400,
            "manifest_cache_seconds": 86400,
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
        print(f"publish OK | {nseg} segs | {playlist_path}", flush=True)

        pok, pmsg = purge_manifest(mid)
        print("purge:", "OK" if pok else f"FAIL ({pmsg})", flush=True)
        if not pok:
            raise RuntimeError("purge fail")

        vok, ttl = verify_fresh(mid)
        if not vok:
            raise RuntimeError("verify weak (retries ke baad bhi)")

        ok_list.append((mid, nseg, ttl))
    except Exception as e:
        print("❌ FAIL:", repr(e), flush=True)
        fail_list.append((mid, repr(e)[:120]))

print("\n================ SUMMARY ================", flush=True)
for mid, nseg, ttl in ok_list:
    print(f"{mid}: OK | {nseg} segs | ttl ~{ttl}s", flush=True)
for mid, title in skip_list:
    print(f"{mid}: SKIP (Mongo me segments data nahi) | {title}", flush=True)
for mid, err in fail_list:
    print(f"{mid}: FAIL | {err}", flush=True)
print(f"TOTAL: OK={len(ok_list)} | SKIP={len(skip_list)} | FAIL={len(fail_list)} "
      f"(Mongo total {len(movie_ids)})", flush=True)
print("==========================================", flush=True)

# SKIP se run RED nahi hota (un movies me dekhne ko hai hi nahi).
# RED sirf tab jab data wali movie publish/purge/verify me fail ho.
if fail_list:
    sys.exit(1)
print(f"✅ DONE — {len(ok_list)} movies fresh for ~25h | {len(skip_list)} skipped (incomplete data)", flush=True)
