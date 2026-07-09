import re
import os
import json
import gzip
import httpx
from urllib.parse import urljoin, quote, unquote, urlparse
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

CDN_HOST = "https://plapi.cdnvideohub.com"
CDN_HEADERS = {
    "referer": "https://hdkino.pub/",
    "origin": "https://hdkino.pub",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
}
VK_HEADERS = {
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "referer": "https://vkvideo.ru/",
    "origin": "https://vkvideo.ru",
}
OKCDN_UPSTREAM = os.environ.get("OKCDN_UPSTREAM", "").rstrip("/")


def parse_proxy_params(request: Request):
    """
    Парсим raw query string вручную чтобы не потерять & внутри url=
    Формат: /proxy?url=ENCODED_URL  или  /proxy?url=ENCODED_URL&base=ENCODED_BASE
    """
    raw = str(request.url.query)
    url = None
    base = None

    if raw.startswith("url="):
        if "&base=" in raw:
            idx = raw.index("&base=")
            url = unquote(raw[4:idx])
            base = unquote(raw[idx + 6:])
        else:
            url = unquote(raw[4:])

    return url, base


def rewrite_m3u8(text: str, orig_url: str, hf_base: str) -> str:
    """Переписывает все URL в m3u8 через /proxy?url="""
    base = orig_url.rsplit("/", 1)[0] + "/"

    def to_proxy(seg: str) -> str:
        seg = seg.strip()
        if not seg:
            return seg
        if seg.startswith("http://") or seg.startswith("https://"):
            abs_url = seg
        elif seg.startswith("//"):
            abs_url = "https:" + seg
        else:
            abs_url = urljoin(base, seg)

        # Якщо URL вже через наш прокси — не обгортаємо знову
        if hf_base and abs_url.startswith(hf_base):
            return abs_url

        sub_base = abs_url.rsplit("/", 1)[0] + "/"
        # Всегда передаём base для правильного разрешения .ts сегментов
        return f"{hf_base}/proxy?url={quote(abs_url, safe='')}&base={quote(sub_base, safe='')}"

    lines = text.splitlines()
    out = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            out.append(line)
        elif stripped.startswith("#"):
            rewritten = re.sub(r'URI="([^"]+)"', lambda m: f'URI="{to_proxy(m.group(1))}"', line)
            out.append(rewritten)
        else:
            out.append(to_proxy(stripped))
    return "\n".join(out)


VEOVEO_API = "https://api.rstprgapipt.com"

# ── VeoVeo индекс (загружается при старте) ────────────────────────────────────
_veoveo_by_kp   = {}   # kinopoiskId (int) → content_id (int)
_veoveo_by_imdb = {}   # imdbId (str)      → content_id (int)
_veoveo_loaded  = False

def _load_veoveo_index():
    global _veoveo_by_kp, _veoveo_by_imdb, _veoveo_loaded
    if _veoveo_loaded:
        return
    try:
        import os, urllib.request, zipfile, io
        ZIP_URL     = "https://github.com/lampac-nextgen/lampac/releases/download/1.18.6/lampac-nextgen.zip"
        FILE_OFFSET = 224621992
        COMP_SIZE   = 4466882

        req = urllib.request.Request(
            ZIP_URL,
            headers={"User-Agent": "Mozilla/5.0", "Range": f"bytes={FILE_OFFSET}-{FILE_OFFSET+30+200+COMP_SIZE}"}
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            # Следуем редиректу вручную если нужно
            final_url = resp.geturl()
            raw = resp.read()

        # Если был редирект — повторяем с реальным URL
        if len(raw) < 100:
            req2 = urllib.request.Request(
                final_url,
                headers={"User-Agent": "Mozilla/5.0", "Range": f"bytes={FILE_OFFSET}-{FILE_OFFSET+30+200+COMP_SIZE}"}
            )
            with urllib.request.urlopen(req2, timeout=120) as resp2:
                raw = resp2.read()

        buf = raw
        fn_len = int.from_bytes(buf[26:28], 'little')
        ex_len = int.from_bytes(buf[28:30], 'little')
        data_start = 30 + fn_len + ex_len
        comp_data = buf[data_start:data_start + COMP_SIZE]

        import zlib
        inflated = zlib.decompress(comp_data, -15)   # deflate raw
        decompressed = gzip.decompress(inflated)      # inner gzip

        records = json.loads(decompressed.decode('utf-8'))
        for item in records:
            cid = item.get('id')
            kp  = item.get('kinopoiskId')
            imdb = item.get('imdbId')
            if cid and kp:
                _veoveo_by_kp[int(kp)] = int(cid)
            if cid and imdb:
                _veoveo_by_imdb[str(imdb)] = int(cid)

        _veoveo_loaded = True
        print(f"[VeoVeo] index loaded: {len(_veoveo_by_kp)} kp, {len(_veoveo_by_imdb)} imdb")
    except Exception as e:
        print(f"[VeoVeo] index load failed: {e}")


@app.on_event("startup")
async def startup_event():
    import asyncio
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _load_veoveo_index)
    # Авто-логін на fanserial якщо є логін/пароль але немає cookie
    if FANSERIAL_LOGIN and FANSERIAL_PASSWORD and not FANSERIAL_COOKIE:
        asyncio.create_task(fanserial_login())


@app.get("/veoveo/search")
async def veoveo_search(kp: int = 0, imdb: str = ""):
    """Ищет VeoVeo content_id по kinopoiskId или imdbId"""
    if not _veoveo_loaded:
        _load_veoveo_index()
    content_id = None
    if kp and kp in _veoveo_by_kp:
        content_id = _veoveo_by_kp[kp]
    elif imdb and imdb in _veoveo_by_imdb:
        content_id = _veoveo_by_imdb[imdb]
    return Response(
        content=json.dumps({"content_id": content_id}),
        media_type="application/json",
        headers={"Access-Control-Allow-Origin": "*"}
    )


@app.get("/veoveo/episodes")
async def veoveo_episodes(id: int):
    """Проксирует /episodes?content-id= к VeoVeo API"""
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        r = await client.get(
            f"{VEOVEO_API}/balancer-api/proxy/playlists/catalog-api/episodes?content-id={id}",
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
        )
    return Response(
        content=r.content,
        status_code=r.status_code,
        media_type="application/json",
        headers={"Access-Control-Allow-Origin": "*"}
    )


@app.get("/veoveo/content")
async def veoveo_content(id: int):
    """Проксирует /contents/{id} к VeoVeo API"""
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        r = await client.get(
            f"{VEOVEO_API}/balancer-api/proxy/playlists/catalog-api/contents/{id}",
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
        )
    return Response(
        content=r.content,
        status_code=r.status_code,
        media_type="application/json",
        headers={"Access-Control-Allow-Origin": "*"}
    )


# ── VkMovie ───────────────────────────────────────────────────────────────────
VK_CLIENT_ID = 52461373
VK_CLIENT_SECRET = "o557NLIkAErNhakXrQ7A"
VK_TOKEN_URL = "https://login.vk.com/?act=get_anonym_token"
VK_SEARCH_URL = f"https://api.vkvideo.ru/method/catalog.getVideoSearchWeb2?v=5.264&client_id={VK_CLIENT_ID}"
VK_SEARCH_OLD_URL = f"https://api.vk.com/method/video.search?v=5.131&adult=1"  # Старий API з підтримкою adult

_vk_token: str = ""
_vk_token_expires: float = 0.0

async def _get_vk_token() -> str:
    import time
    global _vk_token, _vk_token_expires
    if _vk_token and time.time() < _vk_token_expires:
        return _vk_token
    post_data = (
        f"client_secret={VK_CLIENT_SECRET}&client_id={VK_CLIENT_ID}"
        "&scopes=audio_anonymous%2Cvideo_anonymous%2Cphotos_anonymous%2Cprofile_anonymous"
        "&isApiOauthAnonymEnabled=false&version=1&app_id=6287487"
    )
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        r = await client.post(
            VK_TOKEN_URL,
            content=post_data.encode(),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            }
        )
    data = r.json()
    token = data.get("data", {}).get("access_token", "")
    expires = data.get("data", {}).get("expires", -1)
    if token:
        _vk_token = token
        import time as t2
        _vk_token_expires = t2.time() + 3600 * 6 if expires == -1 else float(expires) - 3600 * 4
    return token


@app.get("/vkvideo/image")
async def vkvideo_image_proxy(url: str):
    """Проксі для VK зображень щоб обійти блокування .ru доменів"""
    if not url:
        return Response(content="no url", status_code=400)
    
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        try:
            r = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://vk.com/",
            })
            
            return Response(
                content=r.content,
                status_code=r.status_code,
                media_type=r.headers.get("content-type", "image/jpeg"),
                headers={"Access-Control-Allow-Origin": "*"}
            )
        except Exception as e:
            return Response(content=f"error: {e}", status_code=500)


@app.get("/vkvideo/search")
async def vkvideo_search(q: str = "", offset: int = 0, count: int = 50):
    """Пошук відео VK Video. Для adult контенту використовує Yandex scraping"""
    if not q:
        return Response(content='{"error":"no query"}', status_code=400,
                        media_type="application/json", headers={"Access-Control-Allow-Origin": "*"})

    token = await _get_vk_token()
    if not token:
        return Response(content='{"error":"no_token"}', status_code=503,
                        media_type="application/json", headers={"Access-Control-Allow-Origin": "*"})

    import urllib.parse
    results = []

    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        # 1. Звичайний VK пошук
        try:
            post_data = (
                "screen_ref=search_video_service&input_method=keyboard_search_button"
                f"&q={urllib.parse.quote(q)}&offset={offset}&count={count}&access_token={token}"
            )
            r = await client.post(
                VK_SEARCH_URL,
                content=post_data.encode(),
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Origin": "https://vkvideo.ru",
                    "Referer": "https://vkvideo.ru/",
                }
            )
            data = r.json()
            for item in data.get("response", {}).get("catalog_videos", []):
                v = item.get("video")
                if not v:
                    continue
                files = v.get("files") or {}
                results.append({
                    "id": v.get("id"), "owner_id": v.get("owner_id"),
                    "title": v.get("title"), "description": v.get("description", ""),
                    "duration": v.get("duration", 0), "image": v.get("image", []),
                    "date": v.get("date"), "views": v.get("views", 0),
                    "player": v.get("player"),
                    "mp4_2160": files.get("mp4_2160"), "mp4_1440": files.get("mp4_1440"),
                    "mp4_1080": files.get("mp4_1080"), "mp4_720": files.get("mp4_720"),
                    "mp4_480": files.get("mp4_480"), "mp4_360": files.get("mp4_360"),
                    "mp4_240": files.get("mp4_240"),
                    "hls": files.get("hls"), "subtitles": v.get("subtitles") or [],
                })
        except Exception as e:
            print(f"[vkvideo] VK API error: {e}")

        # 2. Завжди додатково шукаємо через Yandex - знаходить те що VK ховає
        try:
            yandex_url = f"https://yandex.ru/search/?text={urllib.parse.quote(q + ' site:vkvideo.ru')}&lr=213&numdoc=50"
            yr = await client.get(yandex_url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
                "Accept-Language": "ru-RU,ru;q=0.9",
            })
            html = yr.text
            vk_ids = re.findall(r'vkvideo\.ru/video(-?\d+)_(\d+)', html)
            vk_ids += re.findall(r'vk\.com/video(-?\d+)_(\d+)', html)
            vk_ids = list(dict.fromkeys(vk_ids))
            existing = {f"{r.get('owner_id')}_{r.get('id')}" for r in results}

            for owner_id, video_id in vk_ids[:30]:
                if f"{owner_id}_{video_id}" in existing:
                    continue
                try:
                    vg_url = (
                        f"https://api.vkvideo.ru/method/video.get"
                        f"?v=5.264&client_id={VK_CLIENT_ID}"
                        f"&videos={owner_id}_{video_id}&access_token={token}"
                    )
                    vr = await client.get(vg_url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://vkvideo.ru/"})
                    vdata = vr.json()
                    for v in vdata.get("response", {}).get("items", []):
                        files = v.get("files") or {}
                        results.append({
                            "id": v.get("id"), "owner_id": v.get("owner_id"),
                            "title": v.get("title"), "description": v.get("description", ""),
                            "duration": v.get("duration", 0), "image": v.get("image", []),
                            "date": v.get("date"), "views": v.get("views", 0),
                            "player": v.get("player"),
                            "mp4_2160": files.get("mp4_2160"), "mp4_1440": files.get("mp4_1440"),
                            "mp4_1080": files.get("mp4_1080"), "mp4_720": files.get("mp4_720"),
                            "mp4_480": files.get("mp4_480"), "mp4_360": files.get("mp4_360"),
                            "mp4_240": files.get("mp4_240"),
                            "hls": files.get("hls"), "subtitles": v.get("subtitles") or [],
                        })
                        existing.add(f"{v.get('owner_id')}_{v.get('id')}")
                except Exception:
                    continue
        except Exception as e:
            print(f"[vkvideo] Yandex scrape error: {e}")

    import json
    return Response(
        content=json.dumps({"items": results, "count": len(results), "offset": offset}, ensure_ascii=False),
        media_type="application/json",
        headers={"Access-Control-Allow-Origin": "*"}
    )


# Список відомих порно пабліків VK - читається з owners.txt
_porn_owners = set([
    -231023619, -88754941, -224344313, -176294899, -211869299, -150019313,
    -209976560, -122033519, -229794799, -187019501, -227406675, -217878975,
    -228222133, -23482802, -229085991, -29901605, -37160097, -212451998,
    -41903770, -52620949, -228752787, -117717520, -105101581, -219366731,
    -211231029, -27477591, -216486929,
])
_porn_owners_loaded = False

def _load_owners_from_file():
    """Читає owner_id з owners.txt"""
    global _porn_owners
    try:
        owners_file = os.path.join(os.path.dirname(__file__), 'owners.txt')
        if os.path.exists(owners_file):
            with open(owners_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        try:
                            _porn_owners.add(int(line))
                        except ValueError:
                            pass
            print(f"[owners] loaded {len(_porn_owners)} owners from file")
    except Exception as e:
        print(f"[owners] file load error: {e}")

# Завантажуємо при старті
_load_owners_from_file()
async def vkvideo_debug(q: str = "anal"):
    """Дебаг - шукаємо порно паблики через catalog.getVideoSearchWeb2"""
    import urllib.parse as up
    token = await _get_vk_token()
    results = {}
    
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        # Шукаємо відео з порно-назвами щоб знайти owner_id пабліків
        porn_queries = ["legalporno", "anal creampie", "brazzers", "pornhub", "onlyfans"]
        owners = set()
        
        for pq in porn_queries:
            try:
                post_data = f"screen_ref=search_video_service&input_method=keyboard_search_button&q={up.quote(pq)}&count=20&access_token={token}"
                r = await client.post(
                    VK_SEARCH_URL,
                    content=post_data.encode(),
                    headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "Mozilla/5.0", "Referer": "https://vkvideo.ru/"}
                )
                data = r.json()
                videos = data.get("response", {}).get("catalog_videos", [])
                for item in videos:
                    v = item.get("video", {})
                    if v.get("owner_id"):
                        owners.add(v["owner_id"])
            except Exception:
                continue
        
        results["found_owners"] = list(owners)[:30]
        results["count"] = len(owners)

    return Response(
        content=json.dumps(results, ensure_ascii=False),
        media_type="application/json",
        headers={"Access-Control-Allow-Origin": "*"}
    )


@app.get("/vkvideo/adult")
async def vkvideo_adult_search(q: str = "", offset: int = 0, count: int = 50):
    """Пошук відео по всіх паблікам з owners.txt"""
    if not q:
        return Response(content='{"error":"no query"}', status_code=400,
                        media_type="application/json", headers={"Access-Control-Allow-Origin": "*"})

    token = await _get_vk_token()
    if not token:
        return Response(content='{"error":"no_token"}', status_code=503,
                        media_type="application/json", headers={"Access-Control-Allow-Origin": "*"})

    import json as _json
    results = []
    existing = set()
    q_words = [w.lower() for w in q.lower().split() if len(w) > 2]

    all_owners = list(_porn_owners)
    print(f"[adult] searching q='{q}' across {len(all_owners)} owners")

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        import asyncio

        async def fetch_owner(owner_id):
            try:
                vg_url = (
                    f"https://api.vkvideo.ru/method/video.get"
                    f"?v=5.264&client_id={VK_CLIENT_ID}"
                    f"&owner_id={owner_id}&count=200&access_token={token}"
                )
                vr = await client.get(vg_url, headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "https://vkvideo.ru/"
                })
                data = vr.json()
                items = data.get("response", {}).get("items", [])
                if not items:
                    # Логируем если паблик пустой или ошибка
                    error = data.get("error")
                    if error:
                        print(f"[adult] owner {owner_id} error: {error.get('error_msg', 'unknown')}")
                return items
            except Exception as e:
                print(f"[adult] owner {owner_id} fetch error: {e}")
                return []

        tasks = [fetch_owner(oid) for oid in all_owners]
        all_items_list = await asyncio.gather(*tasks)

        for items in all_items_list:
            for v in items:
                title = (v.get("title") or "").lower()
                if q_words and not any(w in title for w in q_words):
                    continue
                key = f"{v.get('owner_id')}_{v.get('id')}"
                if key in existing:
                    continue
                files = v.get("files") or {}
                existing.add(key)
                
                # Проверяем srcAg в URLs
                has_unknown = False
                for url in [files.get("mp4_1080"), files.get("mp4_720"), files.get("mp4_480")]:
                    if url and 'srcAg=UNKNOWN' in url:
                        has_unknown = True
                        break
                
                # Если URLs с UNKNOWN - логируем (для будущего исправления)
                if has_unknown:
                    print(f"[adult] video {key} has srcAg=UNKNOWN URLs (title: {v.get('title', '')[:50]})")
                
                results.append({
                    "id": v.get("id"), "owner_id": v.get("owner_id"),
                    "title": v.get("title"), "description": v.get("description", ""),
                    "duration": v.get("duration", 0), "image": v.get("image", []),
                    "date": v.get("date"), "views": v.get("views", 0),
                    "player": v.get("player"),
                    "mp4_2160": files.get("mp4_2160"), "mp4_1440": files.get("mp4_1440"),
                    "mp4_1080": files.get("mp4_1080"), "mp4_720": files.get("mp4_720"),
                    "mp4_480": files.get("mp4_480"), "mp4_360": files.get("mp4_360"),
                    "mp4_240": files.get("mp4_240"),
                    "hls": files.get("hls"), "subtitles": v.get("subtitles") or [],
                })

    # Подсчитываем сколько всего видео было получено до фильтрации
    total_videos_fetched = sum(len(items) for items in all_items_list)
    unknown_count = sum(1 for r in results if any('srcAg=UNKNOWN' in (r.get(k) or '') for k in ['mp4_1080', 'mp4_720', 'mp4_480']))
    print(f"[adult] q='{q}' total results={len(results)} ({unknown_count} with UNKNOWN srcAg) (fetched {total_videos_fetched} videos from {len(all_owners)} owners)")

    paginated = results[offset:offset + count]

    return Response(
        content=_json.dumps({"items": paginated, "count": len(results), "offset": offset}, ensure_ascii=False),
        media_type="application/json",
        headers={"Access-Control-Allow-Origin": "*"}
    )


@app.get("/vkvideo/upgrade")
async def vkvideo_upgrade_urls(owner_id: int, video_id: int):
    """Получает рабочие URL через video.getForPlay"""
    import json as _json
    print(f"[upgrade] START for {owner_id}_{video_id}")
    try:
        urls, _meta = await _vkvideo_getforplay_urls(owner_id=owner_id, video_id=video_id)
        if not urls:
            print(f"[upgrade] FAILED: no URLs from getForPlay")
            return Response(
                content='{"error":"no_urls"}',
                status_code=404,
                media_type="application/json",
                headers={"Access-Control-Allow-Origin": "*"}
            )

        urls_count = len(urls)
        has_sig = sum(1 for v in urls.values() if v and 'sig=' in v)
        has_subid = sum(1 for v in urls.values() if v and 'subId=' in v)
        has_unknown = sum(1 for v in urls.values() if v and 'srcAg=UNKNOWN' in v)
        print(f"[upgrade] extracted {urls_count} URLs (sig:{has_sig}, subId:{has_subid}, unknown:{has_unknown})")

        result = {
            "mp4_2160": urls.get("mp4_2160") or urls.get("mp4_1080"),
            "mp4_1440": urls.get("mp4_1440") or urls.get("mp4_1080"),
            "mp4_1080": urls.get("mp4_1080"),
            "mp4_720": urls.get("mp4_720"),
            "mp4_480": urls.get("mp4_480"),
            "mp4_360": urls.get("mp4_360"),
            "mp4_240": urls.get("mp4_240"),
            "mp4_144": urls.get("mp4_144"),
            "hls": urls.get("hls"),
            "subtitles": []
        }

        print(f"[upgrade] SUCCESS")
        return Response(
            content=_json.dumps(result, ensure_ascii=False),
            media_type="application/json",
            headers={"Access-Control-Allow-Origin": "*"}
        )
    except Exception as e:
        print(f"[upgrade] ERROR: {e}")
        return Response(
            content=f'{{"error":"{str(e)}"}}',
            status_code=500,
            media_type="application/json",
            headers={"Access-Control-Allow-Origin": "*"}
        )


def _vkvideo_strip_xssi(text: str) -> str:
    if not text:
        return text
    t = text.lstrip()
    if t.startswith(")]}'"):
        nl = t.find("\n")
        if nl != -1:
            return t[nl + 1 :]
        return ""
    return text


def _vkvideo_extract_urls_from_obj(obj, out: dict):
    if obj is None:
        return
    if isinstance(obj, dict):
        files = obj.get("files")
        if isinstance(files, dict):
            for k, v in files.items():
                if isinstance(v, str) and (k.startswith("mp4_") or k == "hls"):
                    out[k] = v
        for k, v in obj.items():
            if isinstance(v, str) and (k.startswith("mp4_") or k == "hls"):
                out[k] = v
            _vkvideo_extract_urls_from_obj(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _vkvideo_extract_urls_from_obj(v, out)


async def _vkvideo_getforplay_urls(owner_id: int, video_id: int):
    token = await _get_vk_token()
    if not token:
        return {}, {"error": "no_token"}

    url = f"https://api.vkvideo.ru/method/video.getForPlay?v=5.282&client_id={VK_CLIENT_ID}"
    post_data = (
        f"owner_id={owner_id}&video_id={video_id}"
        "&fields=skippable_parts%2Cis_serial"
        f"&access_token={token}"
    )

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        r = await client.post(
            url,
            content=post_data.encode(),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Origin": "https://vkvideo.ru",
                "Referer": "https://vkvideo.ru/",
                "Accept": "*/*",
            },
        )

    text = ""
    try:
        text = r.text or ""
    except Exception:
        text = ""

    meta = {
        "status_code": r.status_code,
        "content_type": r.headers.get("content-type", ""),
        "content_length": len(r.content or b""),
    }

    if not text:
        return {}, meta

    text = _vkvideo_strip_xssi(text)
    try:
        data = json.loads(text)
    except Exception:
        return {}, {**meta, "error": "json_parse_error", "preview": text[:400]}

    urls = {}
    _vkvideo_extract_urls_from_obj(data, urls)

    return urls, meta


@app.get("/vkvideo/getforplay")
async def vkvideo_getforplay_debug(owner_id: int, video_id: int):
    urls, meta = await _vkvideo_getforplay_urls(owner_id=owner_id, video_id=video_id)
    result = {
        "meta": meta,
        "urls": urls,
        "has_sig": sum(1 for v in urls.values() if v and "sig=" in v),
        "has_unknown": sum(1 for v in urls.values() if v and "srcAg=UNKNOWN" in v),
    }
    return Response(
        content=json.dumps(result, ensure_ascii=False),
        media_type="application/json",
        headers={"Access-Control-Allow-Origin": "*"},
    )


@app.get("/vkvideo/player")
async def vkvideo_player_extract(player: str):
    """Извлекает рабочие URL из встроенного плеера VK (с subId)"""
    if not player:
        return Response(
            content=json.dumps({"error": "no player url"}),
            status_code=400,
            media_type="application/json",
            headers={"Access-Control-Allow-Origin": "*"}
        )
    
    urls = await _extract_video_urls_from_player(player)
    
    return Response(
        content=json.dumps({"files": urls}, ensure_ascii=False),
        media_type="application/json",
        headers={"Access-Control-Allow-Origin": "*"}
    )


async def _extract_video_urls_from_player(player_url: str) -> dict:
    """Извлекает рабочие URL с subId из встроенного плеера VK"""
    if not player_url:
        return {}
    
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.get(player_url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://vkvideo.ru/",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                "Cookie": ""  # Пустые куки для незалогиненного пользователя
            })
            html = r.text
            
            print(f"[player] fetched {len(html)} bytes from {player_url}")
            
            # Ищем все <script> теги
            script_tags = re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL)
            print(f"[player] DEBUG found {len(script_tags)} script tags")
            
            # НОВЫЙ ПАТТЕРН: ищем playerParams или player.init с конфигом
            player_params_match = re.search(r'(?:playerParams|player\.init)\s*[:=]\s*(\{[^<]+?\});?\s*(?:</script>|var |let |const )', html, re.DOTALL)
            if player_params_match:
                params_json = player_params_match.group(1)
                print(f"[player] DEBUG found playerParams/init, length={len(params_json)}")
                try:
                    # Пробуем распарсить как JSON
                    import json as _json
                    params = _json.loads(params_json)
                    if 'params' in params and isinstance(params['params'], list) and len(params['params']) > 0:
                        player_data = params['params'][0]
                        print(f"[player] DEBUG playerParams has data: {list(player_data.keys())[:5]}")
                        
                        # Извлекаем URL из player_data
                        urls = {}
                        for key in ['url240', 'url360', 'url480', 'url720', 'url1080', 'url1440', 'url2160', 'hls']:
                            if key in player_data and player_data[key]:
                                quality = key.replace('url', 'mp4_') if key != 'hls' else 'hls'
                                urls[quality] = player_data[key]
                                print(f"[player] extracted {quality} from playerParams")
                        
                        if urls:
                            return urls
                except Exception as e:
                    print(f"[player] playerParams JSON parse error: {e}")
            
            # Ищем window.vk JavaScript объект
            vk_data_match = re.search(r'window\.vk\s*=\s*Object\.assign\(window\.vk\s*\|\|\s*\{\},\s*(\{.+?\})\s*\);', html, re.DOTALL)
            if vk_data_match:
                vk_json_text = vk_data_match.group(1)
                print(f"[player] DEBUG found window.vk object, length={len(vk_json_text)}")
            
            # Ищем любые упоминания okcdn или vkuser в <script> тегах
            for i, script_content in enumerate(script_tags[:10]):  # первые 10 скриптов
                if 'okcdn' in script_content or 'vkuser' in script_content or 'subId' in script_content:
                    print(f"[player] DEBUG script {i} contains CDN references, length={len(script_content)}")
                    # Ищем URLs в этом скрипте
                    urls_in_script = re.findall(r'https?://[^\s"\'<>\\]+(?:vkuser\.net|okcdn\.ru)[^\s"\'<>\\]*', script_content)
                    if urls_in_script:
                        print(f"[player] DEBUG script {i} found {len(urls_in_script)} URLs")
                        for u in urls_in_script[:2]:
                            print(f"[player] DEBUG   URL: {u[:150]}")
                    else:
                        # Выводим кусок скрипта для анализа
                        cdn_pos = max(script_content.find('okcdn'), script_content.find('vkuser'), script_content.find('subId'))
                        if cdn_pos > 0:
                            sample_start = max(0, cdn_pos - 100)
                            sample_end = min(len(script_content), cdn_pos + 200)
                            print(f"[player] DEBUG script {i} sample around CDN: {script_content[sample_start:sample_end]}")
            
            # Ищем все строки содержащие vkuser.net или okcdn.ru во ВСЁМ HTML
            vk_urls = re.findall(r'https?://[^\s"\'<>\\]+(?:vkuser\.net|okcdn\.ru)[^\s"\'<>\\]+', html)
            if vk_urls:
                print(f"[player] DEBUG found {len(vk_urls)} potential CDN URLs")
                for i, u in enumerate(vk_urls[:3]):  # показываем первые 3
                    # Декодируем escape-последовательности
                    try:
                        decoded = u.encode('utf-8').decode('unicode_escape')
                        print(f"[player] DEBUG CDN URL {i+1}: {decoded[:150]}")
                    except:
                        print(f"[player] DEBUG CDN URL {i+1}: {u[:150]}")
            
            # 2. Ищем JSON объекты с url
            json_objects = re.findall(r'\{[^{}]*"url[^{}]{10,500}\}', html)
            if json_objects:
                print(f"[player] DEBUG found {len(json_objects)} JSON objects with 'url'")
                for i, obj in enumerate(json_objects[:2]):  # показываем первые 2
                    print(f"[player] DEBUG JSON {i+1}: {obj[:200]}")
            
            # 3. Ищем <script> блоки с данными
            scripts = re.findall(r'<script[^>]*>var\s+\w+\s*=\s*(\{[^<]{100,1000}\})', html, re.DOTALL)
            if scripts:
                print(f"[player] DEBUG found {len(scripts)} script var assignments")
            
            # Пробуем извлечь URL из найденных паттернов
            urls = {}
            
            # Метод 1: Прямой поиск URL в HTML (самый надёжный)
            for url in vk_urls:
                # Декодируем escape-последовательности (например \/ -> /)
                try:
                    url = url.encode('utf-8').decode('unicode_escape')
                    # Убираем лишние escape символы
                    url = url.replace(r'\/', '/')
                except:
                    pass
                
                # Проверяем что это рабочий URL (с subId или полный URL)
                if 'subId=' in url or 'sig=' in url:
                    # Определяем качество по контексту или берём базовое
                    if '/720' in url or 'hd=1' in url or 'ct=21' in url:
                        urls['mp4_720'] = url
                    elif '/1080' in url or 'hd=2' in url or 'ct=22' in url:
                        urls['mp4_1080'] = url
                    elif '/480' in url or 'ct=20' in url:
                        urls['mp4_480'] = url
                    elif '/360' in url or 'ct=6' in url:
                        urls['mp4_360'] = url
                    elif '/1440' in url or 'ct=23' in url:
                        urls['mp4_1440'] = url
                    elif '/240' in url or 'ct=9' in url:
                        urls['mp4_240'] = url
                    elif '.m3u8' in url:
                        urls['hls'] = url
                    else:
                        # Если качество не определено, берём как 720p если ещё нет
                        if 'mp4_720' not in urls:
                            urls['mp4_720'] = url
                    print(f"[player] extracted URL with sig/subId: {url[:80]}... (len={len(url)})")
            
            # Метод 2: Парсинг JSON объектов
            for obj_text in json_objects:
                try:
                    # Пытаемся найти URL внутри JSON текста
                    url_match = re.search(r'"url\d*":\s*"([^"]+)"', obj_text)
                    if url_match:
                        url = url_match.group(1)
                        # Декодируем escape последовательности
                        url = url.encode('utf-8').decode('unicode_escape')
                        url = url.replace(r'\/', '/')
                        if ('subId=' in url or 'sig=' in url) and ('vkuser.net' in url or 'okcdn.ru' in url):
                            # Определяем качество
                            quality_match = re.search(r'"url(\d+)"', obj_text)
                            if quality_match:
                                quality = quality_match.group(1)
                                urls[f"mp4_{quality}"] = url
                                print(f"[player] extracted mp4_{quality} from JSON")
                except Exception as e:
                    print(f"[player] JSON parse error: {e}")
                    continue
            
            # Метод 3: Старые паттерны для совместимости
            if not urls:
                # Паттерн: "url720":"https://..."
                matches = re.findall(r'"url(\d+)":"([^"]+)"', html)
                for quality, url in matches:
                    try:
                        decoded_url = url.encode('utf-8').decode('unicode_escape')
                        decoded_url = decoded_url.replace(r'\/', '/')
                        if 'subId=' in decoded_url or 'sig=' in decoded_url or 'okcdn.ru' in decoded_url or 'vkuser.net' in decoded_url:
                            urls[f"mp4_{quality}"] = decoded_url
                    except Exception:
                        continue
            
            # Метод 4: НОВЫЙ - ищем паттерны типа "type":4,"url":"https://..."
            if not urls:
                type_url_matches = re.findall(r'"type":(\d+),"url":"([^"]+)"', html)
                for type_id, url in type_url_matches:
                    try:
                        decoded_url = url.encode('utf-8').decode('unicode_escape')
                        decoded_url = decoded_url.replace(r'\/', '/')
                        if 'okcdn.ru' in decoded_url or 'vkuser.net' in decoded_url:
                            # type маппинг: 4=720p, 22=1080p и т.д.
                            quality_map = {
                                '4': 'mp4_720',
                                '22': 'mp4_1080',
                                '21': 'mp4_720',
                                '6': 'mp4_360',
                                '9': 'mp4_240',
                                '20': 'mp4_480'
                            }
                            quality = quality_map.get(type_id, f'mp4_{type_id}')
                            urls[quality] = decoded_url
                            print(f"[player] extracted {quality} from type:{type_id} pattern")
                    except Exception:
                        continue
            
            print(f"[player] extracted {len(urls)} working URLs")
            for k, v in urls.items():
                print(f"[player]   {k}: has_sig={('sig=' in v)}, has_subId={('subId=' in v)}")
            return urls
    except Exception as e:
        import traceback
        print(f"[player] extraction error: {e}")
        print(f"[player] traceback: {traceback.format_exc()}")
        return {}


@app.get("/vkvideo/player")
async def vkvideo_player_extract(player: str):
    """Извлекает рабочие URL из встроенного плеера VK (с subId)"""
    if not player:
        return Response(
            content=json.dumps({"error": "no player url"}),
            status_code=400,
            media_type="application/json",
            headers={"Access-Control-Allow-Origin": "*"}
        )
    
    urls = await _extract_video_urls_from_player(player)
    
    return Response(
        content=json.dumps({"files": urls}, ensure_ascii=False),
        media_type="application/json",
        headers={"Access-Control-Allow-Origin": "*"}
    )


@app.api_route("/vkmovie/stream", methods=["GET", "HEAD"])
async def vkmovie_stream(request: Request):
    """Проксує VK відео/сегменти через HF Space (обхід блокування .ru доменів)"""
    raw_query = str(request.url.query)
    url = None
    if raw_query.startswith("url="):
        url_part = raw_query[4:]
        if "&" in url_part:
            url_part = url_part.split("&")[0]
        url = unquote(url_part)
        
    if not url:
        return Response(content="no url", status_code=400)
        
    url = url.strip().replace("`", "").strip().strip('"').strip("'").strip()
    for _ in range(2):
        url2 = unquote(url)
        if url2 == url:
            break
        url = url2
    url = url.strip().replace("`", "").strip().strip('"').strip("'").strip()
    print(f"[vkmovie/stream] Request: method={request.method}, url={repr(url)}, raw query={repr(raw_query)}")

    parsed_incoming = urlparse(url)
    
    # НЕ меняем srcIp - URL уже подписан VK с определенным IP
    # Render не блокирует .ru домены, так что просто проксируем как есть

    # Убрали редирект на HuggingFace - всё проксируем через Render напрямую
    # (раньше тут был редирект на OKCDN_UPSTREAM для okcdn.ru и vkuser.net)

    referer = "https://vkvideo.ru/"
    origin = "https://vkvideo.ru"

    print(f"[vkmovie/stream] Using referer: {repr(referer)}, origin: {repr(origin)}")

    # КРИТИЧНО: VK валидирует srcAg из URL с реальным User-Agent!
    # Парсим srcAg из URL и подставляем правильный UA
    import re
    srcag_match = re.search(r'srcAg=([^&]+)', url)
    srcag = srcag_match.group(1) if srcag_match else "CHROME"
    
    # Подбираем User-Agent и заголовки в зависимости от srcAg
    if srcag == "WEBKIT":
        user_agent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
        # Safari НЕ отправляет Sec-Ch-Ua и Sec-Fetch заголовки!
        # Safari НЕ отправляет Origin для простых GET запросов к медиа!
        req_headers = {
            "Accept": "*/*",
            "Accept-Encoding": "identity;q=1, *;q=0",
            "Accept-Language": "ru-RU,ru;q=0.9",
            "Connection": "keep-alive",
            "Host": parsed_incoming.netloc,
            "Referer": referer,
            "User-Agent": user_agent,
        }
    else:
        # Chrome отправляет все заголовки включая Origin
        user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
        req_headers = {
            "Accept": "*/*",
            "Accept-Encoding": "identity;q=1, *;q=0",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Connection": "keep-alive",
            "Host": parsed_incoming.netloc,
            "Origin": origin,
            "Referer": referer,
            "Sec-Ch-Ua": '"Not(A:Brand";v="8", "Chromium";v="144", "Google Chrome";v="144"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "video",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "cross-site",
            "User-Agent": user_agent,
        }
    
    print(f"[vkmovie/stream] Detected srcAg={srcag}, using User-Agent: {user_agent[:80]}...")
    
    hf = str(request.base_url).rstrip("/").replace("http://", "https://")

    # Helper function to rewrite m3u8
    def rewrite_m3u8_content(text_content):
        base = url.rsplit("/", 1)[0] + "/"
        print(f"[vkmovie/stream] m3u8 rewrite base URL: {base}")
        
        def rewrite_seg(seg):
            seg = seg.strip()
            if not seg: return seg
            if seg.startswith("http://") or seg.startswith("https://"):
                abs_url = seg
            elif seg.startswith("//"):
                abs_url = "https:" + seg
            else:
                abs_url = urljoin(base, seg)
            
            rewritten = f"{hf}/vkmovie/stream?url={quote(abs_url, safe='')}"
            # Логируем первые 3 сегмента
            if not hasattr(rewrite_seg, 'count'):
                rewrite_seg.count = 0
            if rewrite_seg.count < 3:
                print(f"[vkmovie/stream] m3u8 segment {rewrite_seg.count}: {seg[:80]} -> {rewritten[:120]}")
                rewrite_seg.count += 1
            return rewritten
        
        lines = text_content.splitlines()
        out = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                out.append(line)
            elif stripped.startswith("#"):
                rewritten = re.sub(r'URI="([^"]+)"', lambda m: f'URI="{rewrite_seg(m.group(1))}"', line)
                out.append(rewritten)
            else:
                out.append(rewrite_seg(stripped))
        return "\n".join(out)

    range_header = request.headers.get("range")
    if range_header:
        req_headers["range"] = range_header

    probe = None
    probe_ct = ""
    probe_cl_header = ""
    probe_cl = 0
    probe_text = ""

    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        try:
            probe_headers = dict(req_headers)
            probe_headers["range"] = "bytes=0-511"
            print(f"[vkmovie/stream] Probing with range: {url}, headers={probe_headers}")
            probe = await client.get(url, headers=probe_headers)
            print(f"[vkmovie/stream] Probe response 1: status={probe.status_code}, headers={dict(probe.headers)}, content={repr(probe.content)}")
        except Exception as e:
            print(f"[vkmovie/stream] Probe error: {repr(e)}")
            return Response(content=f"error: {e}", status_code=500)

    probe_ct = probe.headers.get("content-type", "")
    probe_cl_header = probe.headers.get("content-range", "")
    if probe_cl_header and "/" in probe_cl_header:
        probe_cl = int(probe_cl_header.split("/")[-1] or 0)
    else:
        probe_cl = int(probe.headers.get("content-length", 0))
    probe_text = probe.content[:512].decode("utf-8", errors="ignore")
    print(f"[vkmovie/stream] Final probe result: status={probe.status_code}, ct={probe_ct}, cl={probe_cl}, content={repr(probe.content)}")

    parsed_url = urlparse(url)
    is_m3u8_url = parsed_url.path.lower().endswith(".m3u8")
    is_m3u8 = "mpegurl" in probe_ct or probe_text.lstrip().startswith("#EXTM3U") or is_m3u8_url
    is_large = probe_cl > 10 * 1024 * 1024  # > 10MB

    if request.method == "HEAD":
        head_status = 200 if probe.status_code == 206 else probe.status_code

        if is_m3u8:
            return Response(
                content=b"",
                status_code=head_status,
                media_type="application/vnd.apple.mpegurl",
                headers={"Access-Control-Allow-Origin": "*"}
            )

        resp_headers = {
            "Access-Control-Allow-Origin": "*",
            "Accept-Ranges": "bytes",
            "Content-Type": probe_ct or "video/mp4",
        }

        if probe_cl_header:
            resp_headers["content-range"] = probe_cl_header
        if probe_cl:
            resp_headers["content-length"] = str(probe_cl)

        return Response(content=b"", status_code=head_status, headers=resp_headers, media_type=resp_headers["Content-Type"])

    if is_m3u8:
        # m3u8 — завантажуємо повністю БЕЗ range header (если он был)
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            try:
                # Используем ВСЕ заголовки (без range)
                m3u8_headers = dict(req_headers)
                if "range" in m3u8_headers:
                    del m3u8_headers["range"]
                
                print(f"[vkmovie/stream] Fetching m3u8: {url[:100]}...")
                print(f"[vkmovie/stream] m3u8 headers: {m3u8_headers}")
                r = await client.get(url, headers=m3u8_headers)
                print(f"[vkmovie/stream] m3u8 response: status={r.status_code}, content_length={len(r.content)}")
                text = r.text
                print(f"[vkmovie/stream] m3u8 content preview: {text[:200]}")
            except Exception as e:
                print(f"[vkmovie/stream] m3u8 fetch error: {e}")
                return Response(content=f"error: {e}", status_code=500)

        rewritten = rewrite_m3u8_content(text)
        print(f"[vkmovie/stream] m3u8 rewritten preview: {rewritten[:200]}")
        return Response(
            content=rewritten,
            media_type="application/vnd.apple.mpegurl",
            headers={"Access-Control-Allow-Origin": "*"}
        )

    if not is_large:
        # Малі файли (сегменти TS, субтитри < 10MB) — завантажуємо повністю
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            try:
                r = await client.get(url, headers=req_headers)
                ct = r.headers.get("content-type", "application/octet-stream")
                content = r.content

                # Очищаємо WebVTT від karaoke timing тегів <00:00:00.000>
                if "vtt" in ct or "text" in ct or url.lower().endswith(".vtt"):
                    try:
                        text = content.decode("utf-8", errors="ignore")
                        # Видаляємо <HH:MM:SS.mmm> теги (karaoke timing)
                        text = re.sub(r'<\d{2}:\d{2}:\d{2}\.\d{3}>', '', text)
                        # Видаляємо <c.color> теги
                        text = re.sub(r'<c\.[^>]+>', '', text)
                        text = re.sub(r'</c>', '', text)
                        content = text.encode("utf-8")
                    except Exception:
                        pass

                return Response(
                    content=content,
                    status_code=r.status_code,
                    media_type=ct,
                    headers={"Access-Control-Allow-Origin": "*"}
                )
            except Exception as e:
                return Response(content=f"error: {e}", status_code=500)

    # Великі файли (MP4) — стримінг з Range підтримкою
    client2 = httpx.AsyncClient(timeout=300, follow_redirects=True)
    final_headers = dict(req_headers)

    r2 = await client2.send(
        client2.build_request("GET", url, headers=final_headers),
        stream=True
    )
        
    resp_headers = {
        "Access-Control-Allow-Origin": "*",
        "Accept-Ranges": "bytes",
        "Content-Type": r2.headers.get("content-type", "video/mp4"),
    }
    for h in ("content-length", "content-range"):
        if h in r2.headers:
            resp_headers[h] = r2.headers[h]

    async def stream_and_close():
        try:
            async for chunk in r2.aiter_bytes(chunk_size=65536):
                yield chunk
        finally:
            await r2.aclose()
            await client2.aclose()

    return StreamingResponse(
        stream_and_close(),
        status_code=r2.status_code,
        headers=resp_headers,
        media_type=resp_headers["Content-Type"],
    )





@app.get("/")
async def root():
    return {"status": "ok", "build": "2026-07-08.1"}


# ── VeoVeo via kinoserial embed ───────────────────────────────────────────────
KINOSERIAL_HEADERS = {
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "referer": "https://veoveo.ru/",
    "origin": "https://veoveo.ru",
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "accept-language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "sec-fetch-dest": "iframe",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "cross-site",
    "upgrade-insecure-requests": "1",
    "cache-control": "no-cache",
    "pragma": "no-cache",
}

@app.get("/veoveo/embed")
async def veoveo_embed(url: str):
    """Fetches kinoserial embed HTML with proper Referer (browser can't set it directly)"""
    if not url or "kinoserial" not in url:
        return Response(content='{"error":"invalid url"}', status_code=400,
                        media_type="application/json", headers={"Access-Control-Allow-Origin": "*"})
    
    import traceback
    
    # Try with different transport settings
    for attempt, kwargs in enumerate([
        {"timeout": 20, "follow_redirects": True, "http2": True},
        {"timeout": 20, "follow_redirects": True},
    ]):
        try:
            async with httpx.AsyncClient(**kwargs) as client:
                r = await client.get(url, headers=KINOSERIAL_HEADERS)
                print(f"[veoveo/embed] attempt {attempt+1} OK: status={r.status_code} len={len(r.text)}")
                return Response(
                    content=r.text,
                    status_code=r.status_code,
                    media_type="text/html",
                    headers={"Access-Control-Allow-Origin": "*"}
                )
        except Exception as e:
            err_detail = traceback.format_exc()
            print(f"[veoveo/embed] attempt {attempt+1} failed: {type(e).__name__}: {repr(e)}\n{err_detail}")
            last_err = f"{type(e).__name__}: {repr(e)}"
    
    return Response(content=json.dumps({"error": last_err}), status_code=500,
                    media_type="application/json", headers={"Access-Control-Allow-Origin": "*"})


@app.get("/veoveo/search")
async def veoveo_search_new(q: str = "", kp_id: str = ""):
    """Search veoveo.ru by title or kp_id"""
    if kp_id:
        url = f"https://veoveo.ru/api/search.php?kp_id={kp_id}"
    elif q:
        url = f"https://veoveo.ru/api/search.php?q={quote(q)}"
    else:
        return Response(content='{"error":"no query"}', status_code=400,
                        media_type="application/json", headers={"Access-Control-Allow-Origin": "*"})
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        try:
            r = await client.get(url, headers={"user-agent": "Mozilla/5.0"})
            text = r.text.lstrip('\ufeff')  # strip BOM
            return Response(content=text, status_code=r.status_code,
                            media_type="application/json",
                            headers={"Access-Control-Allow-Origin": "*"})
        except Exception as e:
            return Response(content=json.dumps({"error": str(e)}), status_code=500,
                            media_type="application/json", headers={"Access-Control-Allow-Origin": "*"})


@app.get("/veoveo/page")
async def veoveo_page(path: str):
    """Fetch veoveo.ru page to extract embed iframe URL"""
    url = f"https://veoveo.ru{path}"
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        try:
            r = await client.get(url, headers={"user-agent": "Mozilla/5.0"})
            html = r.text
            # Extract kinoserial iframe src
            m = re.search(r'src="(https?://[^"]*kinoserial\.net/embed_[^"]+)"', html, re.I)
            if m:
                return Response(
                    content=json.dumps({"embed_url": m.group(1)}),
                    media_type="application/json",
                    headers={"Access-Control-Allow-Origin": "*"}
                )
            # Fallback: find token + embed id
            token_m = re.search(r'token=([a-f0-9]{32})', html, re.I)
            id_m = re.search(r'embed_(serial|movie)/(\d+)', html, re.I)
            if token_m and id_m:
                embed_url = f"https://tv-1-kinoserial.net/embed_{id_m.group(1)}/{id_m.group(2)}/?token={token_m.group(1)}"
                return Response(
                    content=json.dumps({"embed_url": embed_url}),
                    media_type="application/json",
                    headers={"Access-Control-Allow-Origin": "*"}
                )
            return Response(
                content=json.dumps({"embed_url": None, "error": "not found"}),
                media_type="application/json",
                headers={"Access-Control-Allow-Origin": "*"}
            )
        except Exception as e:
            return Response(content=json.dumps({"error": str(e)}), status_code=500,
                            media_type="application/json", headers={"Access-Control-Allow-Origin": "*"})


FANSERIAL_HOST = "https://fanserial.me"
# Поточний cookie — оновлюється при логіні або береться з env
FANSERIAL_COOKIE = os.environ.get("FANSERIAL_COOKIE", "dle_user_id=140677; dle_password=e14cb459266301e55b8c4df5fd9d1097; dle_newpm=0; PHPSESSID=e50735b7c15e6ff067b4a531536640d4")
FANSERIAL_LOGIN = os.environ.get("FANSERIAL_LOGIN", "imhotep")
FANSERIAL_PASSWORD = os.environ.get("FANSERIAL_PASSWORD", "reducto41032")

# Кэш сессии — храним актуальные cookies
_fanserial_session_cookie = FANSERIAL_COOKIE  # инициализируем сразу с захардкоженным cookie
_fanserial_session_lock = None  # будет asyncio.Lock

async def fanserial_login() -> str:
    """
    Логін через DLE cookie — використовуємо dle_user_id + dle_password (MD5 хеш).
    DLE зберігає MD5(пароль) в cookie dle_password.
    """
    import hashlib
    if not FANSERIAL_LOGIN or not FANSERIAL_PASSWORD:
        return FANSERIAL_COOKIE

    # MD5 хеш пароля — саме так DLE зберігає в cookie
    pwd_hash = hashlib.md5(FANSERIAL_PASSWORD.encode()).hexdigest()

    # Спочатку отримуємо PHPSESSID
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            r0 = await client.get(FANSERIAL_HOST + "/", headers={
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
            })
            phpsessid = r0.cookies.get("PHPSESSID", "")

            # POST логін з MD5 хешем
            post_data = {
                "login_name": FANSERIAL_LOGIN,
                "login_password": FANSERIAL_PASSWORD,
                "login": "submit",
            }
            r1 = await client.post(
                FANSERIAL_HOST + "/index.php",
                data=post_data,
                headers={
                    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
                    "content-type": "application/x-www-form-urlencoded",
                    "referer": FANSERIAL_HOST + "/",
                    "origin": FANSERIAL_HOST,
                    "cookie": f"PHPSESSID={phpsessid}" if phpsessid else "",
                }
            )

            # Збираємо всі cookies
            all_cookies = {}
            if phpsessid:
                all_cookies["PHPSESSID"] = phpsessid
            for k, v in r1.cookies.items():
                if v != "deleted":
                    all_cookies[k] = v

            # Якщо DLE не дав dle_user_id — будуємо cookie вручну з MD5
            if "dle_user_id" not in all_cookies:
                print(f"[FanSerial] Login POST failed, building cookie manually with MD5")
                # Знаходимо user_id з поточного FANSERIAL_COOKIE
                uid_match = re.search(r'dle_user_id=(\d+)', FANSERIAL_COOKIE)
                uid = uid_match.group(1) if uid_match else "140677"
                all_cookies["dle_user_id"] = uid
                all_cookies["dle_password"] = pwd_hash
                all_cookies["dle_newpm"] = "0"

            cookie_str = "; ".join([f"{k}={v}" for k, v in all_cookies.items()])
            print(f"[FanSerial] Login result ({len(all_cookies)} cookies): {cookie_str[:120]}")
            return cookie_str

    except Exception as e:
        print(f"[FanSerial] Login error: {e}")
        return FANSERIAL_COOKIE

async def get_fanserial_cookie() -> str:
    """Возвращает актуальный cookie, при необходимости логинится заново"""
    global _fanserial_session_cookie
    if _fanserial_session_cookie:
        return _fanserial_session_cookie
    # Нет cookie — логинимся
    _fanserial_session_cookie = await fanserial_login()
    return _fanserial_session_cookie

async def get_fanserial_headers_async(extra=None):
    cookie = await get_fanserial_cookie()
    h = {
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "accept-language": "ru-RU,ru;q=0.9,en;q=0.8",
        "referer": "https://fanserial.me/",
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "same-origin",
        "upgrade-insecure-requests": "1",
    }
    if cookie:
        h["cookie"] = cookie
    if extra:
        h.update(extra)
    return h

def get_fanserial_headers(extra=None):
    """Синхронная версия для обратной совместимости"""
    h = {
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "accept-language": "ru-RU,ru;q=0.9,en;q=0.8",
        "referer": "https://fanserial.me/",
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "same-origin",
        "upgrade-insecure-requests": "1",
    }
    if _fanserial_session_cookie:
        h["cookie"] = _fanserial_session_cookie
    if extra:
        h.update(extra)
    return h
FANCDN_IFRAME_HEADERS = {
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "referer": "https://fanserial.me/",
    "sec-fetch-dest": "iframe",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "cross-site",
}


@app.get("/fancdn/debug_js")
async def fancdn_debug_js(url: str = "https://fanserial.me/templates/FanSeries/js/scripts.min.js"):
    """Читає JS файл і шукає endpoint для плеєра"""
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        r = await client.get(url, headers=await get_fanserial_headers_async())
        js = r.text
        # Шукаємо контекст навколо controller.php
        controller_contexts = []
        for m in re.finditer(r'controller\.php', js):
            start = max(0, m.start() - 200)
            end = min(len(js), m.end() + 200)
            controller_contexts.append(js[start:end])
        # Шукаємо fancdn і player
        fancdn = re.findall(r'[^\s"\']{0,50}fancdn[^\s"\']{0,100}', js)[:10]
        player_load = re.findall(r'(?:loadPlayer|getPlayer|initPlayer|showPlayer)[^;]{0,200}', js)[:5]
        # Шукаємо mod= параметри для DLE
        mod_params = re.findall(r'mod=[^\s&"\']{1,50}', js)[:10]
        return Response(
            content=json.dumps({
                "controller_contexts": controller_contexts[:5],
                "fancdn": fancdn,
                "player_load": player_load,
                "mod_params": mod_params,
            }, ensure_ascii=False),
            media_type="application/json",
            headers={"Access-Control-Allow-Origin": "*"}
        )


@app.get("/fancdn/debug_page")
async def fancdn_debug_page(url: str):
    """Повертає HTML сторінки для дебагу"""
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        r = await client.get(url, headers=await get_fanserial_headers_async())
        html = r.text
        # Шукаємо fancdn CDN посилання
        cdn_links = re.findall(r'https?://[^\s"\'<>]*cdn\.fancdn\.net[^\s"\'<>]*', html, re.I)
        # Шукаємо /player/ виклики
        player_calls = re.findall(r'/player/\?[^\s"\'<>]{10,300}', html, re.I)
        # Шукаємо data-file або data-src атрибути
        data_file = re.findall(r'data-(?:file|src|url|player)[^=]*="([^"]{10,300})"', html, re.I)
        # Шукаємо JS об'єкти з file:
        js_file = re.findall(r'["\']file["\']\s*:\s*["\']([^"\']{10,200})["\']', html, re.I)
        # Шукаємо всі hls.m3u8 посилання
        hls_links = re.findall(r'https?://[^\s"\'<>]*\.m3u8[^\s"\'<>]*', html, re.I)
        return Response(
            content=json.dumps({
                "cdn_links": cdn_links[:10],
                "player_calls": player_calls[:10],
                "data_file": data_file[:10],
                "js_file": js_file[:10],
                "hls_links": hls_links[:10],
                "html_len": len(html),
            }, ensure_ascii=False),
            media_type="application/json",
            headers={"Access-Control-Allow-Origin": "*"}
        )


@app.get("/fancdn/episode")
async def fancdn_episode(url: str, voice_idx: int = 0):
    """Завантажує конкретний епізод і повертає HLS URL для вибраної озвучки"""
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        try:
            r = await client.get(url, headers=await get_fanserial_headers_async())
            ep_html = r.text
        except Exception as e:
            return Response(content=json.dumps({"error": str(e)}), status_code=500,
                            media_type="application/json", headers={"Access-Control-Allow-Origin": "*"})

        # Парсим window.cdnData
        cdn_data_matches = re.findall(r'window\.cdnData\[\d+\]\s*=\s*(\{[^}]+\})', ep_html, re.I)
        voices = []
        seen_hls = set()
        for match in cdn_data_matches:
            try:
                obj = json.loads(match)
                name = obj.get("name", "")
                player_url = obj.get("player", "")
                hls_m = re.search(r'file=(https?://[^\s&"\']+\.m3u8)', player_url)
                if hls_m:
                    hls_url = hls_m.group(1)
                    if hls_url not in seen_hls:
                        seen_hls.add(hls_url)
                        voices.append({"id": len(voices), "title": name, "hls": hls_url})
            except Exception:
                pass

        if not voices:
            return Response(content=json.dumps({"error": "no_voices", "url": url}), status_code=404,
                            media_type="application/json", headers={"Access-Control-Allow-Origin": "*"})

        idx = min(voice_idx, len(voices) - 1)
        return Response(
            content=json.dumps({"hls": voices[idx]["hls"], "name": voices[idx]["title"], "all": voices}, ensure_ascii=False),
            media_type="application/json",
            headers={"Access-Control-Allow-Origin": "*"}
        )


@app.get("/fancdn/relogin")
async def fancdn_relogin():
    """Примусово перелогінюємось на fanserial.me"""
    global _fanserial_session_cookie
    _fanserial_session_cookie = ""
    cookie = await fanserial_login()
    _fanserial_session_cookie = cookie
    return Response(
        content=json.dumps({"ok": bool(cookie), "cookie_len": len(cookie)}),
        media_type="application/json",
        headers={"Access-Control-Allow-Origin": "*"}
    )


@app.get("/fancdn/search")
async def fancdn_search(title: str, year: str = "", kp: str = ""):
    """
    Ищет контент на fanserial.me — парсит fanserials-player-2.js для получения HLS URL
    """
    global _fanserial_session_cookie

    async def do_search(retry=False):
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            # Шаг 1: поиск
            search_url = f"{FANSERIAL_HOST}/?do=search&subaction=search&story={quote(title)}"
            try:
                r = await client.get(search_url, headers=await get_fanserial_headers_async())
                html = r.text
            except Exception as e:
                return {"error": str(e)}

            if ("cf-browser-verification" in html or "Just a moment" in html) and not retry:
                global _fanserial_session_cookie
                _fanserial_session_cookie = ""
                _fanserial_session_cookie = await fanserial_login()
                return await do_search(retry=True)

            # Шаг 2: находим страницу — ищем и сериалы и фильмы
            page_url = None

            def find_best_match(html_text, search_year):
                """Знаходить найкращий результат пошуку з урахуванням року і назви"""
                # Шукаємо всі item-search блоки (serial і film)
                candidates = []
                # Розбиваємо по item-search
                parts = re.split(r'class="item-search-(?:serial|film)[^"]*"', html_text)
                for part in parts[1:]:
                    href_m = re.search(r'<a href="(https?://[^"]+\.html)"', part)
                    if not href_m:
                        continue
                    href = href_m.group(1)
                    # Рік — шукаємо в name-origin-search або просто (YYYY)
                    year_m = re.search(r'name-origin-search[^>]*>[^(]*\((\d{4})', part[:800])
                    if not year_m:
                        year_m = re.search(r'\((\d{4})\s*\)', part[:800])
                    found_year = int(year_m.group(1)) if year_m else 0
                    candidates.append({"href": href, "year": found_year})

                if not candidates:
                    return None

                # Якщо є рік — фільтруємо
                if search_year:
                    y = int(search_year)
                    by_year = [c for c in candidates if c["year"] and abs(c["year"] - y) <= 1]
                    if by_year:
                        return by_year[0]["href"]

                # Інакше перший результат
                return candidates[0]["href"]

            page_url = find_best_match(html, year)

            if not page_url:
                return {"error": "not_found", "html_len": len(html)}

            # Шаг 3: загружаем страницу фильма/сериала
            try:
                r2 = await client.get(page_url, headers=await get_fanserial_headers_async())
                page_html = r2.text
            except Exception as e:
                return {"error": f"page_load: {e}"}

            # Шаг 4: ищем ссылки на сезоны (например /8-the-boys/1-season.html)
            season_links = re.findall(r'href="(https?://fanserial\.me/[^"]+/(\d+)-season\.html)"', page_html, re.I)
            if not season_links:
                season_links_rel = re.findall(r'href="(/[^"]+/(\d+)-season\.html)"', page_html, re.I)
                season_links = [(FANSERIAL_HOST + l[0], l[1]) for l in season_links_rel]

            if not season_links:
                # Это фильм — парсим window.cdnData прямо со страницы
                cdn_data_matches = re.findall(r'window\.cdnData\[\d+\]\s*=\s*(\{[^}]+\})', page_html, re.I)
                voices = []
                seen_hls = set()
                for match in cdn_data_matches:
                    try:
                        obj = json.loads(match)
                        name = obj.get("name", "")
                        player_url = obj.get("player", "")
                        hls_m = re.search(r'file=(https?://[^\s&"\']+\.m3u8)', player_url)
                        if hls_m:
                            hls_url = hls_m.group(1)
                            if hls_url not in seen_hls:
                                seen_hls.add(hls_url)
                                voices.append({"id": len(voices), "title": name, "hls": hls_url})
                    except Exception:
                        pass

                if not voices:
                    return {"error": "no_voices_on_film_page", "page_url": page_url}

                return {"voices": voices, "seasons": {}, "ep_url_template": "", "page_url": page_url, "is_serial": False}

            # Парсим каждую страницу сезона
            seasons = {}
            all_ep_links = []
            
            for season_url, season_num in season_links:
                try:
                    r_season = await client.get(season_url, headers=await get_fanserial_headers_async())
                    season_html = r_season.text
                    
                    # Ищем ссылки на эпизоды на странице сезона
                    ep_links = re.findall(r'href="(https?://fanserial\.me/[^"]+/\d+-season/\d+-episode\.html)"', season_html, re.I)
                    if not ep_links:
                        ep_links_rel = re.findall(r'href="(/[^"]+/\d+-season/\d+-episode\.html)"', season_html, re.I)
                        ep_links = [FANSERIAL_HOST + l for l in ep_links_rel]
                    
                    all_ep_links.extend(ep_links)
                    
                    # Группируем эпизоды по сезонам
                    for ep_url in ep_links:
                        m = re.search(r'/(\d+)-season/(\d+)-episode', ep_url)
                        if m:
                            s, e = int(m.group(1)), int(m.group(2))
                            if s not in seasons:
                                seasons[s] = set()
                            seasons[s].add(e)
                except Exception as e:
                    # Если не удалось загрузить сезон, пропускаем
                    continue
            
            if not all_ep_links:
                return {"error": "no_episodes_found", "page_url": page_url}

            # Загружаем первый эпизод для получения HLS URLs озвучек
            first_ep_url = all_ep_links[0]
            try:
                r_ep = await client.get(first_ep_url, headers=await get_fanserial_headers_async())
                ep_html = r_ep.text
            except Exception as e:
                return {"error": f"episode_load: {e}"}

            # Парсим window.cdnData из HTML — структура: window.cdnData[N] = {"name":"...","player":"/player/?file=..."}
            cdn_data_matches = re.findall(
                r'window\.cdnData\[\d+\]\s*=\s*(\{[^}]+\})',
                ep_html, re.I
            )

            voices = []
            seen_hls = set()
            for match in cdn_data_matches:
                try:
                    obj = json.loads(match)
                    name = obj.get("name", "")
                    player_url = obj.get("player", "")
                    # Извлекаем HLS URL из /player/?file=URL
                    hls_m = re.search(r'file=(https?://[^\s&"\']+\.m3u8)', player_url)
                    if hls_m:
                        hls_url = hls_m.group(1)
                        if hls_url not in seen_hls:
                            seen_hls.add(hls_url)
                            voices.append({"id": len(voices), "title": name, "hls": hls_url})
                except Exception:
                    pass

            if not voices:
                return {"error": "cdndata_not_found", "ep_url": first_ep_url,
                        "ep_html_len": len(ep_html),
                        "has_cdndata": "cdnData" in ep_html,
                        "has_player": "player" in ep_html.lower(),
                        "ep_snippet": ep_html[:300]}

            # Шаблон URL для загрузки других эпизодов
            ep_url_template = re.sub(r'/\d+-season/\d+-episode\.html', '/{season}-season/{episode}-episode.html', first_ep_url)

            return {
                "voices": voices,
                "seasons": {str(s): sorted(list(eps)) for s, eps in sorted(seasons.items())},
                "ep_url_template": ep_url_template,
                "page_url": page_url,
                "is_serial": len(seasons) > 0
            }

    result = await do_search()
    status = 200 if "playlist" in result else 404
    return Response(
        content=json.dumps(result, ensure_ascii=False),
        status_code=status,
        media_type="application/json",
        headers={"Access-Control-Allow-Origin": "*"}
    )


@app.get("/fancdn/embed")
async def fancdn_embed(url: str):
    """Загружает fancdn iframe по прямому URL и возвращает playlist"""
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        try:
            r = await client.get(url, headers=FANCDN_IFRAME_HEADERS)
            iframe_html = r.text
        except Exception as e:
            return Response(content=json.dumps({"error": str(e)}), status_code=500,
                            media_type="application/json", headers={"Access-Control-Allow-Origin": "*"})

        clean = re.sub(r'[\n\r\t]+', '', iframe_html).replace('var ', '\n')
        pm = re.search(r'playlist\s*=\s*(\[[\s\S]+?\]);', clean)
        if not pm:
            return Response(content=json.dumps({"error": "playlist_not_found"}), status_code=404,
                            media_type="application/json", headers={"Access-Control-Allow-Origin": "*"})

        try:
            playlist = json.loads(pm.group(1))
        except Exception as e:
            return Response(content=json.dumps({"error": f"parse: {e}"}), status_code=500,
                            media_type="application/json", headers={"Access-Control-Allow-Origin": "*"})

        return Response(
            content=json.dumps({"playlist": playlist}, ensure_ascii=False),
            media_type="application/json",
            headers={"Access-Control-Allow-Origin": "*"}
        )


@app.get("/mp4")
async def mp4_proxy(vkId: str, quality: str = "720p", request: Request = None):
    """Стримит MP4 через проксі с поддержкой Range запросов"""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"{CDN_HOST}/api/v1/player/sv/video/{vkId}",
            headers=CDN_HEADERS
        )
        data = r.json()
        sources = data.get("sources") or {}

    quality_map = {
        "1080p": sources.get("mpegFullHdUrl"),
        "720p":  sources.get("mpegHighUrl"),
        "480p":  sources.get("mpegMediumUrl"),
        "360p":  sources.get("mpegLowUrl"),
        "240p":  sources.get("mpegLowestUrl"),
        "144p":  sources.get("mpegTinyUrl"),
    }
    url = quality_map.get(quality) or sources.get("mpegHighUrl") or sources.get("mpegFullHdUrl")
    if not url:
        return Response(content="not found", status_code=404)

    # Передаём Range заголовок если есть (для перемотки)
    req_headers = dict(VK_HEADERS)
    if request and request.headers.get("range"):
        req_headers["range"] = request.headers["range"]

    from fastapi.responses import StreamingResponse as SR
    import asyncio

    async def stream_mp4():
        async with httpx.AsyncClient(timeout=300) as client:
            async with client.stream("GET", url, headers=req_headers) as r2:
                async for chunk in r2.aiter_bytes(chunk_size=65536):
                    yield chunk

    # Получаем заголовки без тела
    async with httpx.AsyncClient(timeout=15) as client:
        head = await client.head(url, headers=req_headers)

    resp_headers = {
        "Access-Control-Allow-Origin": "*",
        "Accept-Ranges": "bytes",
        "Content-Type": head.headers.get("content-type", "video/mp4"),
    }
    if "content-length" in head.headers:
        resp_headers["Content-Length"] = head.headers["content-length"]
    if "content-range" in head.headers:
        resp_headers["Content-Range"] = head.headers["content-range"]

    status = 206 if request and request.headers.get("range") else 200

    return StreamingResponse(stream_mp4(), status_code=status, headers=resp_headers, media_type="video/mp4")


@app.get("/fresh_hls")
async def fresh_hls(vkId: str, request: Request, quality: str = ""):
    """Получает свежий HLS URL для vkId — решает проблему протухших expires= URL"""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"{CDN_HOST}/api/v1/player/sv/video/{vkId}",
            headers=CDN_HEADERS
        )
        data = r.json()
        hls_url = (data.get("sources") or {}).get("hlsUrl", "")
        if not hls_url:
            return Response(content="#EXTM3U\n#EXT-X-ENDLIST", media_type="application/vnd.apple.mpegurl")

    hf = str(request.base_url).rstrip("/").replace("http://", "https://")
    base = hls_url.rsplit("/", 1)[0] + "/"

    # Получаем master m3u8 и ищем нужное качество
    async with httpx.AsyncClient(timeout=15) as client:
        r2 = await client.get(hls_url, headers=VK_HEADERS)
        master = r2.text

    if not master.strip().startswith("#EXTM3U"):
        proxy_url = f"{hf}/proxy?url={quote(hls_url, safe='')}"
        return Response(content=f"#EXTM3U\n#EXT-X-STREAM-INF:PROGRAM-ID=1\n{proxy_url}\n",
                       media_type="application/vnd.apple.mpegurl",
                       headers={"Access-Control-Allow-Origin": "*"})

    # Маппинг качеств
    quality_map = {
        'ultra': '2160p', '4k': '2160p', 'quad': '1440p', '2k': '1440p',
        'full': '1080p', 'hd': '720p', 'sd': '480p', 'low': '360p',
        'lowest': '240p', 'mobile': '144p'
    }
    quality_order = ['2160p','1440p','1080p','720p','480p','360p','240p','144p']

    # Парсим master и находим нужный sub-playlist
    lines = master.splitlines()
    streams = []
    for i, line in enumerate(lines):
        if line.startswith('#EXT-X-STREAM-INF') and i + 1 < len(lines):
            next_line = lines[i + 1].strip()
            if not next_line or next_line.startswith('#'):
                continue
            q_match = re.search(r'QUALITY=([^,\s]+)', line, re.I)
            r_match = re.search(r'RESOLUTION=(\d+x\d+)', line, re.I)
            label = None
            if q_match:
                label = quality_map.get(q_match.group(1).lower())
                if not label:
                    n = re.match(r'\d+', q_match.group(1))
                    label = n.group() + 'p' if n else q_match.group(1)
            elif r_match:
                h = int(r_match.group(1).split('x')[1])
                if h >= 2160: label = '2160p'
                elif h >= 1440: label = '1440p'
                elif h >= 1080: label = '1080p'
                elif h >= 720: label = '720p'
                elif h >= 480: label = '480p'
                elif h >= 360: label = '360p'
                else: label = f'{h}p'
            if label:
                abs_url = next_line if next_line.startswith('http') else urljoin(base, next_line)
                streams.append((label, abs_url))

    # Выбираем нужное качество или лучшее
    target_url = None
    if quality and streams:
        for label, url in streams:
            if label == quality:
                target_url = url
                break
    if not target_url and streams:
        # Сортируем и берём лучшее
        def q_rank(item):
            try: return quality_order.index(item[0])
            except: return 99
        streams.sort(key=q_rank)
        target_url = streams[0][1]

    if not target_url:
        target_url = hls_url

    # Проксируем выбранный sub-playlist
    sub_base = target_url.rsplit("/", 1)[0] + "/"
    proxy_url = f"{hf}/proxy?url={quote(target_url, safe='')}&base={quote(sub_base, safe='')}"

    return Response(
        content=f"#EXTM3U\n#EXT-X-STREAM-INF:PROGRAM-ID=1\n{proxy_url}\n",
        media_type="application/vnd.apple.mpegurl",
        headers={"Access-Control-Allow-Origin": "*"}
    )
@app.get("/kp_by_imdb")
async def kp_by_imdb(imdb: str = "", title: str = "", year: str = ""):
    """Ищет Кинопоиск ID по IMDB ID или названию"""
    KP_API_KEY = "0319695d-7be3-4b6b-9d55-58baa6527f39"
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        # Поиск по imdb_id
        if imdb:
            try:
                r = await client.get(
                    f"https://kinopoiskapiunofficial.tech/api/v2.2/films?imdbId={imdb}",
                    headers={"X-API-KEY": KP_API_KEY}
                )
                if r.status_code == 200:
                    data = r.json()
                    items = data.get("items", [])
                    if items and items[0].get("kinopoiskId"):
                        return {"kp_id": items[0]["kinopoiskId"]}
            except Exception:
                pass

        # Поиск по названию (fallback)
        if title:
            try:
                r2 = await client.get(
                    f"https://kinopoiskapiunofficial.tech/api/v2.1/films/search-by-keyword?keyword={title}",
                    headers={"X-API-KEY": KP_API_KEY}
                )
                if r2.status_code == 200:
                    data2 = r2.json()
                    films = data2.get("films", [])
                    for film in films:
                        film_year = str(film.get("year", ""))
                        if not year or film_year == year or abs(int(film_year or 0) - int(year or 0)) <= 1:
                            kp = film.get("filmId")
                            if kp:
                                return {"kp_id": kp}
            except Exception:
                pass

    return {"kp_id": None}


@app.get("/cdnvideohub/playlist")
async def playlist(kp: int, pub: int = 12):
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"{CDN_HOST}/api/v1/player/sv/playlist?pub={pub}&aggr=kp&id={kp}",
            headers=CDN_HEADERS
        )
    return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.get("/cdnvideohub/video")
async def video(vkId: str):
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"{CDN_HOST}/api/v1/player/sv/video/{vkId}",
            headers=CDN_HEADERS
        )
    return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@app.get("/proxy")
async def proxy(request: Request):
    url, base_override = parse_proxy_params(request)

    if not url:
        return Response(content="no url", status_code=400)

    base = base_override if base_override else url.rsplit("/", 1)[0] + "/"

    # Выбираем заголовки в зависимости от хоста
    range_header = request.headers.get("range")
    if "interkh.com" in url:
        req_headers = {
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
            "origin": "https://api.ortified.ws",
            "referer": "https://api.ortified.ws/",
        }
    else:
        req_headers = dict(VK_HEADERS)
    if range_header:
        req_headers["range"] = range_header

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        r = await client.get(url, headers=req_headers)

    content_type = r.headers.get("content-type", "application/octet-stream")
    text = r.text
    is_m3u8 = "mpegurl" in content_type or ".m3u8" in url.split("?")[0] or text.strip().startswith("#EXTM3U")

    if not is_m3u8:
        resp_headers = {"Access-Control-Allow-Origin": "*"}
        # Пробрасываем заголовки для Range/MP4
        for h in ("content-range", "accept-ranges", "content-length"):
            if h in r.headers:
                resp_headers[h] = r.headers[h]
        return Response(
            content=r.content,
            status_code=r.status_code,
            media_type=content_type,
            headers=resp_headers
        )

    hf = str(request.base_url).rstrip("/").replace("http://", "https://")

    def to_proxy(seg: str) -> str:
        seg = seg.strip()
        if not seg:
            return seg
        if seg.startswith("http://") or seg.startswith("https://"):
            abs_url = seg
        elif seg.startswith("//"):
            abs_url = "https:" + seg
        else:
            abs_url = urljoin(base, seg)
        # Якщо URL вже через наш прокси — не обгортаємо знову
        if abs_url.startswith(hf + "/"):
            return abs_url
        sub_base = abs_url.rsplit("/", 1)[0] + "/"
        return f"{hf}/proxy?url={quote(abs_url, safe='')}&base={quote(sub_base, safe='')}"

    lines = text.splitlines()
    out = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            out.append(line)
        elif stripped.startswith("#"):
            rewritten = re.sub(r'URI="([^"]+)"', lambda m: f'URI="{to_proxy(m.group(1))}"', line)
            out.append(rewritten)
        else:
            out.append(to_proxy(stripped))

    return Response(
        content="\n".join(out),
        media_type="application/vnd.apple.mpegurl",
        headers={"Access-Control-Allow-Origin": "*"}
    )

@app.get("/fetch")
async def fetch(url: str):
    """Простой прокси для JSON API — обходит Cloudflare с браузерными заголовками"""
    if not url:
        return Response(content="no url", status_code=400)
    headers = {
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "accept": "application/json, text/plain, */*",
        "accept-language": "ru-RU,ru;q=0.9,en;q=0.8",
        "referer": url.split("/")[0] + "//" + url.split("/")[2] + "/",
    }
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        r = await client.get(url, headers=headers)
    return Response(
        content=r.content,
        status_code=r.status_code,
        media_type=r.headers.get("content-type", "application/json"),
        headers={"Access-Control-Allow-Origin": "*"}
    )

@app.get("/proxy_mp4")
async def proxy_mp4(url: str, request: Request):
    """Стриминг MP4 с поддержкой Range запросов для перемотки"""
    if not url:
        return Response(content="no url", status_code=400)

    req_headers = {
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
        "accept": "*/*",
    }
    range_header = request.headers.get("range")
    if range_header:
        req_headers["range"] = range_header

    # Открываем соединение, читаем заголовки, потом стримим тело
    client = httpx.AsyncClient(timeout=300, follow_redirects=True)
    r = await client.send(
        client.build_request("GET", url, headers=req_headers),
        stream=True
    )

    resp_headers = {
        "Access-Control-Allow-Origin": "*",
        "Accept-Ranges": "bytes",
        "Content-Type": r.headers.get("content-type", "video/mp4"),
    }
    for h in ("content-length", "content-range"):
        if h in r.headers:
            resp_headers[h] = r.headers[h]

    async def stream_and_close():
        try:
            async for chunk in r.aiter_bytes(chunk_size=65536):
                yield chunk
        finally:
            await r.aclose()
            await client.aclose()

    return StreamingResponse(
        stream_and_close(),
        status_code=r.status_code,
        headers=resp_headers,
        media_type=resp_headers["Content-Type"],
    )
