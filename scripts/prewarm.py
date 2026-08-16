#!/usr/bin/env python3
# Homelander PREWARM — cold segments ko edge pe warm karo (koi secret nahi)
# Manifest public hai (fallback_auth token + worker URL + cdn URL sab usi me).
# Flow per segment: CDN probe -> cold to /prepare (signed) -> probe again.
# ⚠️ Edge cache per-PoP hota hai: GHA (US) se chalane par US edge warm hota hai.
#    India edge warm karne ke liye isi script ko Colab (India network) se chalao.
# ENV: MOVIE_ID (single) ya MOVIE_IDS (comma) ya khaali = saari movies
#      VERBOSE_LOGS=1 full detail
import os, re, json, time, sys, base64
import requests

REF = "https://v-player.pages.dev/"
UA = "Mozilla/5.0 (Homelander-Prewarm)"
VERBOSE = os.environ.get("VERBOSE_LOGS", "").lower() in ("1", "true", "yes")

def _cfg():
    raw = os.environ.get("HL_CFG", "")
    if not raw:
        sys.exit("HL_CFG secret missing")
    return json.loads(base64.b64decode(raw).decode())
_CFG = _cfg()
CDN_BASE = _CFG["cdn_base"]

def fetch(url, headers=None, timeout=90):
    h = {"Referer": REF, "User-Agent": UA}
    if headers: h.update(headers)
    return requests.get(url, headers=h, timeout=timeout)

def get_movie_ids():
    single = os.environ.get("MOVIE_ID", "").strip()
    if single:
        return [single]
    multi = os.environ.get("MOVIE_IDS", "").strip()
    if multi:
        return [x.strip() for x in multi.split(",") if x.strip()]
    # saari movies: known list (daily republish se). Mongo nahi padhenge (secret nahi)
    return ["19124", "20038", "28114", "60842", "83716", "85344"]

def warm_segment(cdn_url, fa, worker):
    # 1. probe (edge warm?)
    try:
        r = fetch(cdn_url, headers={"Range": "bytes=0-0"}, timeout=30)
        if r.status_code in (200, 206):
            return "warm", r.status_code
    except Exception:
        pass
    # 2. cold -> /prepare (signed playback token)
    mid = fa.get("movie_id"); exp = fa.get("exp"); sig = fa.get("sig")
    obj = cdn_url.split("/segments/")[-1]
    prep = f"{worker}/prepare/{obj}?mid={mid}&exp={exp}&sig={sig}"
    try:
        r2 = fetch(prep, timeout=120)
        if r2.status_code == 200:
            # 3. probe again (edge warm hua?)
            r3 = fetch(cdn_url, headers={"Range": "bytes=0-1023"}, timeout=30)
            if r3.status_code in (200, 206):
                return "warmed", r3.status_code
            return "no206", r3.status_code
        return f"fail{r2.status_code}", 0
    except Exception as e:
        return "err", str(e)[:30]

def main():
    ids = get_movie_ids()
    print(f"PREWARM START | movies={len(ids)}" + (f" -> {ids}" if VERBOSE else ""))
    total_warm = total_cold = total_fail = 0
    for mid in ids:
        try:
            m = fetch(f"{CDN_BASE}/manifests/{mid}.json?_pw={int(time.time())}", timeout=30)
            m.raise_for_status()
            mf = m.json()
            cdn = mf.get("cdn_base_url") or CDN_BASE
            worker = (mf.get("workers") or [_CFG.get("worker_base")])[0]
            pl_url = mf.get("playlist_url")
            fa = mf.get("fallback_auth") or {}
            rp = fetch(pl_url, timeout=30); rp.raise_for_status()
            segs = [l.strip() for l in rp.text.splitlines() if l.startswith("http") and "/segments/" in l]
            w = c = f = 0
            for s in segs:
                st, code = warm_segment(s, fa, worker)
                if st in ("warm", "warmed"): w += 1
                elif st == "no206": c += 1
                else: f += 1
            total_warm += w; total_cold += c; total_fail += f
            print(f"{mid[:4]}…: warm={w} cold={c} fail={f} (total {len(segs)})")
        except Exception as e:
            print(f"{mid[:4]}…: ERROR {type(e).__name__}" + (f" {e}" if VERBOSE else ""))
    print(f"PREWARM DONE | warm={total_warm} cold={total_cold} fail={total_fail}")

if __name__ == "__main__":
    main()
