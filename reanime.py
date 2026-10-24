#!/usr/bin/env python3
import asyncio
import json
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

BASE = "https://reanime.to"
FLIX = "https://flixcloud.cc"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
HEADERS = {"User-Agent": _UA, "Accept": "application/json, */*"}
_DECRYPT_MJS = str(Path(__file__).parent / "decrypt.mjs")

_client: Optional[httpx.AsyncClient] = None


@asynccontextmanager
async def lifespan(_app):
    global _client
    _client = httpx.AsyncClient(
        http2=True,
        timeout=httpx.Timeout(20.0),
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        headers=HEADERS,
        follow_redirects=True,
    )
    yield
    await _client.aclose()


app = FastAPI(title="ReAnime Scraper", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


async def _get(path: str, params: dict = None, base: str = BASE) -> Any:
    r = await _client.get(f"{base}{path}", params=params)
    if r.status_code == 404:
        raise HTTPException(404, detail="Not found")
    if not r.is_success:
        raise HTTPException(r.status_code, detail=r.text[:300])
    return r.json()


def _anilist_from_anime(anime: dict) -> Optional[int]:
    if not anime:
        return None
    if anime.get("anilist"):
        return int(anime["anilist"])
    for key in ("extra_large", "large", "medium"):
        url = (anime.get("cover_image") or {}).get(key, "")
        m = re.search(r"/bx(\d+)-", url)
        if m:
            return int(m.group(1))
    return None


async def _decrypt_embed(html: bytes) -> dict:
    proc = await asyncio.create_subprocess_exec(
        "node", _DECRYPT_MJS, "-",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(input=html), timeout=20.0)
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(504, detail="Decrypt subprocess timed out")
    if proc.returncode != 0:
        raise HTTPException(502, detail=f"Decrypt error: {stderr.decode()[:300]}")
    return json.loads(stdout)


async def get_stream_url(access_id: str, v: int = 2) -> dict:
    r = await _client.get(f"{FLIX}/e/{access_id}?v={v}", headers={**HEADERS, "Referer": f"{BASE}/"})
    if not r.is_success:
        raise HTTPException(r.status_code, detail=f"Embed fetch failed: {r.status_code}")
    return await _decrypt_embed(r.content)


async def _servers(slug: str, ep: int, anilist_id: Optional[int] = None) -> dict:
    watch = await _get(f"/api/v1/watch/{slug}", {"ep": ep})
    aid = anilist_id or _anilist_from_anime(watch.get("anime"))

    flix: dict = {}
    if aid:
        try:
            flix = await _get(f"/api/flix/{aid}/{ep}")
        except HTTPException:
            pass

    links = list(watch.get("episode_links") or [])
    if flix.get("success") and flix.get("servers"):
        seen = {s.get("$id") for s in links}
        for s in flix["servers"]:
            if s.get("$id") not in seen:
                links.append(s)

    _order = {"HD-2": 0, "HD-1": 1}
    _sort = lambda lst: sorted(lst, key=lambda s: _order.get(s.get("serverName", ""), 9))

    return {
        "sub":         _sort([s for s in links if s.get("dataType") in ("sub", "s-sub")]),
        "dub":         _sort([s for s in links if s.get("dataType") in ("dub", "s-dub")]),
        "anime":       watch.get("anime"),
        "current":     watch.get("current"),
        "duration":    watch.get("duration"),
        "intro_start": watch.get("intro_start"),
        "intro_end":   watch.get("intro_end"),
        "outro_start": watch.get("outro_start"),
        "outro_end":   watch.get("outro_end"),
        "anilist_id":  aid,
    }


@app.get("/")
async def root():
    return {
        "status": "ok",
        "endpoints": {
            "search":          "GET /search?q=...&limit=20",
            "home":            "GET /home?limit=20",
            "top":             "GET /top?period=week&limit=20",
            "schedule":        "GET /schedule",
            "info":            "GET /info/{slug}",
            "episodes":        "GET /episodes/{slug}",
            "servers":         "GET /servers/{slug}/{episode}[?anilist_id=...]",
            "stream":          "GET /stream/{access_id}[?v=2]",
            "stream_link":     "GET /stream/from-link?link={flixcloud_url}",
            "thumbnails":      "GET /thumbnails/{anilist_id}",
            "recommendations": "GET /recommendations/{slug}",
        },
    }


@app.get("/search")
async def search(
    q: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    return await _get("/api/v1/search", {"q": q, "limit": limit, "offset": offset})


@app.get("/home")
async def home(limit: int = Query(20, ge=1, le=100)):
    latest, top = await asyncio.gather(
        _get("/api/v1/home/latest-aired", {"limit": limit}),
        _get("/api/v1/top/anime", {"period": "week", "limit": limit}),
    )
    return {"latest_aired": latest, "top_weekly": top}


@app.get("/top")
async def top(
    period: str = Query("week", pattern="^(day|week|month)$"),
    limit: int = Query(20, ge=1, le=100),
):
    return await _get("/api/v1/top/anime", {"period": period, "limit": limit})


@app.get("/schedule")
async def schedule():
    return await _get("/api/v1/schedule")


@app.get("/info/{slug}")
async def anime_info(slug: str):
    anime, eps = await asyncio.gather(
        _get(f"/api/v1/anime/{slug}"),
        _get(f"/api/v1/anime/{slug}/episodes", {"limit": 2000}),
    )
    anilist_id = anime.get("anilist_id") or _anilist_from_anime(anime)
    ep_list = eps if isinstance(eps, list) else eps.get("data", eps.get("episodes", []))
    return {**anime, "episodes": ep_list, "anilist_id": anilist_id}


@app.get("/episodes/{slug}")
async def episodes(slug: str):
    data = await _get(f"/api/v1/anime/{slug}/episodes", {"limit": 2000})
    return data if isinstance(data, list) else data.get("data", data.get("episodes", data))


@app.get("/servers/{slug}/{episode}")
async def servers(slug: str, episode: int, anilist_id: Optional[int] = Query(None)):
    return await _servers(slug, episode, anilist_id)


@app.get("/stream/from-link")
async def stream_from_link(link: str = Query(...)):
    m = re.search(r"/e/([^?#\s]+)\?v=(\d+)", link)
    if not m:
        raise HTTPException(400, detail="Expected URL: https://flixcloud.cc/e/{id}?v={1|2}")
    return await get_stream_url(m.group(1), int(m.group(2)))


@app.get("/stream/{access_id}")
async def stream(access_id: str, v: int = Query(2, ge=1, le=2)):
    return await get_stream_url(access_id, v)


@app.get("/thumbnails/{anilist_id}")
async def thumbnails(anilist_id: int):
    return await _get(f"/api/thumbnails/{anilist_id}")


@app.get("/recommendations/{slug}")
async def recommendations(slug: str):
    return await _get(f"/api/v1/anime/{slug}/recommendations")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("reanime:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)), workers=1, reload=False)
