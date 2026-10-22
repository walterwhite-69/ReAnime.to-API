## Move to this api 
https://github.com/walterwhite-69/Anivexa-API/

# reanime-scraper

A self-hosted anime streaming API that scrapes [reanime.to](https://reanime.to) and fully decrypts [flixcloud.cc](https://flixcloud.cc) HLS streams. Works as a drop-in alternative to Consumet. No headless browsers — pure Python + Node.js.

## What it does

- Search anime, browse home/top charts, get airing schedules
- Full anime info with episode lists
- Get all available streaming servers (HD-1 sub, HD-1 dub, HD-2 sub, HD-2 dub)
- **Decrypt the actual `.m3u8` stream URL** by reverse-engineering flixcloud.cc's rotating WASM-based AES-256-CBC encryption
- Returns subtitles (SRT/VTT, multiple languages), thumbnail VTT sprites, intro/outro chapter timestamps

## Setup

**Requirements:** Python 3.11+, Node.js 20+

```bash
pip install fastapi uvicorn httpx[http2] pycryptodome
cd reanime
uvicorn reanime:app --host 0.0.0.0 --port 8000
```

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/search?q=...&limit=20` | Search anime by name |
| GET | `/home?limit=20` | Latest aired + top weekly |
| GET | `/top?period=week&limit=20` | Top anime (`day` / `week` / `month`) |
| GET | `/schedule` | Weekly airing schedule |
| GET | `/info/{slug}` | Anime metadata + full episode list |
| GET | `/episodes/{slug}` | Episode list only |
| GET | `/servers/{slug}/{episode}` | All streaming servers for an episode |
| GET | `/stream/{access_id}?v=2` | Decrypt stream → HLS URL + subtitles |
| GET | `/stream/from-link?link={url}` | Same, but pass the full flixcloud URL |
| GET | `/thumbnails/{anilist_id}` | Episode thumbnail data |
| GET | `/recommendations/{slug}` | Related anime |

The `slug` is the URL-friendly anime ID from reanime.to (e.g. `one-piece-xamk74`).

## Typical flow

```
1. GET /search?q=demon+slayer
   → pick a slug from results

2. GET /servers/{slug}/{episode}
   → returns sub[] and dub[] arrays, each with serverName + dataLink
   → dataLink is a flixcloud.cc embed URL

3. GET /stream/from-link?link={dataLink}
   → returns the decrypted m3u8 URL, subtitles, thumbnail VTT, chapters
```

### `/servers` response

```json
{
  "sub": [
    { "serverName": "HD-2", "dataLink": "https://flixcloud.cc/e/abc123?v=2", "dataType": "sub" },
    { "serverName": "HD-1", "dataLink": "https://flixcloud.cc/e/abc123?v=1", "dataType": "sub" }
  ],
  "dub": [ ... ],
  "anilist_id": 178005,
  "anime": { ... },
  "intro_start": 90,
  "intro_end": 180
}
```

### `/stream` response

```json
{
  "url": "https://fetch1.flixcloud.cc/_v7/{video_id}/master.m3u8?token=...",
  "subtitles": [
    { "url": "https://...", "language": "English (Track 2 (ENG))", "format": "srt", "default": true },
    ...
  ],
  "thumbnails_vtt": "https://fetch1.flixcloud.cc/thumbnails_vtt/{video_id}",
  "video_title": "Episode.Title.1080p.mkv",
  "intro_chapter": null,
  "outro_chapter": { "start": 1340, "end": 1420, "title": "Credits" },
  "video_id": "0f477519-..."
}
```

The `thumbnails_vtt` URL returns a standard WEBVTT file with sprite sheet regions (`160×90`, 5-second intervals) for seek preview thumbnails.

## Performance

Full pipeline from slug + episode to playable stream:

| Step | Time |
|------|------|
| `/servers` (reanime.to flix API) | ~700ms |
| `/stream` — embed page fetch | ~1,200ms |
| `/stream` — token API + WASM + AES | ~350ms |
| **Total cold** | **~2.5–3s** |

All latency is network — the crypto itself (WASM + PBKDF2 + AES) takes under 10ms.

## How the decryption works

flixcloud.cc embeds streams behind a rotating WASM-based encryption scheme. Every page load gets a fresh WASM binary with different constants, a new one-time token, and new encrypted key material.

The decryption pipeline (`decrypt.mjs`):

1. Fetch `flixcloud.cc/e/{access_id}?v={1|2}` — parse SvelteKit SSR data block
2. Derive 7 obfuscated field names via 6 rounds of SHA-256 on `obfuscation_seed`
3. Extract `frag1`, `iv` from nested crypto object; `keyFrag2`, `token` from page data
4. `GET /api/m3u8/{token}` — one-time payload; field keys = `sha256(token+"vid")[:10]` and `sha256(token+"key")[:10]`
5. Run the embed page's own WASM: `out[i] = ((frag1[i] ^ kf2[i] ^ T[i]) * 2 + 16) & 0xFF) ^ ((i * 35 + seed_int) & 0xFF)`
6. `key_material = PBKDF2(wasm_out, salt=seed, iterations=1000, len=32, hash=SHA-256)`
7. `key_material[i] ^= ord(seed[i % len(seed)])`
8. `aes_key = SHA-256(key_material)`
9. `stream_url = AES-256-CBC_decrypt(aes_key, iv, encrypted_url).trim()`

The WASM is executed via Node.js `WebAssembly.instantiate` using the binary embedded in the page — no hardcoded constants, works across WASM rotations.

> **Note:** Tokens are one-time-use. The token API returns `410 Gone` on reuse. Stream URLs are short-lived JWTs (~6 hours). Do not cache `/stream` responses.

## Files

```
reanime/
├── reanime.py      # FastAPI app — all endpoints and reanime.to API wrappers
└── decrypt.mjs     # Node.js — WASM execution + PBKDF2 + AES-256-CBC decryption
```
