#!/usr/bin/env python3
"""
Guía iLuvatar - Movistar-only backend, optimized for Render Free.

Behavior:
- Does NOT download the full EPG at startup (avoids OOM).
- Provides a manual /refresh endpoint to trigger an update when you choose.
- Loads cached EPG from disk (if present) on startup.
- Optional background refresh if env ENABLE_BG_REFRESH is "1" (disabled by default).
"""
import os, gzip, io, time, asyncio, logging
from logging.handlers import RotatingFileHandler
from fastapi import FastAPI, HTTPException, Response, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse
import httpx
from lxml import etree

MOV_URL = os.environ.get("MOV_URL", "https://raw.githubusercontent.com/MPAndrew/EpgGratis/master/guide.xml.gz")
CACHE_PATH = os.environ.get("CACHE_PATH", "cache/movistar_epg.xml.gz")
REFRESH_INTERVAL = int(os.environ.get("REFRESH_INTERVAL", str(60*60*3)))  # seconds
HTTPX_TIMEOUT = int(os.environ.get("HTTPX_TIMEOUT", "20"))
MAX_PROGRAMS = int(os.environ.get("MAX_PROGRAMS", "24"))
ENABLE_BG_REFRESH = os.environ.get("ENABLE_BG_REFRESH", "0") == "1"

logger = logging.getLogger("iLuvatar")
logger.setLevel(logging.INFO)
fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
ch = logging.StreamHandler()
ch.setFormatter(fmt)
logger.addHandler(ch)
fh = RotatingFileHandler("iluvatar.log", maxBytes=2_000_000, backupCount=2)
fh.setFormatter(fmt)
logger.addHandler(fh)

app = FastAPI(title="Guía iLuvatar (Movistar EPG Only)")

_CACHE = {
    "raw": None,
    "channels": {},
    "programmes": {},
    "fetched_at": 0,
}

async def fetch_movistar_bytes() -> bytes:
    async with httpx.AsyncClient(timeout=HTTPX_TIMEOUT) as client:
        r = await client.get(MOV_URL)
        r.raise_for_status()
        return r.content

def safe_decompress(data: bytes) -> bytes:
    if not data:
        return b""
    if data[:2] == b'\x1f\x8b':  # gzip
        try:
            return gzip.decompress(data)
        except Exception:
            try:
                import gzip as _gzip, io as _io
                with _gzip.GzipFile(fileobj=_io.BytesIO(data)) as gf:
                    return gf.read()
            except Exception:
                return data
    return data

def parse_movistar(xml_bytes: bytes):
    channels = {}
    programmes = {}
    if not xml_bytes:
        return channels, programmes
    try:
        parser = etree.XMLParser(recover=True, encoding='utf-8')
        root = etree.fromstring(xml_bytes, parser=parser)
    except Exception as e:
        logger.warning("lxml parse root failed: %s", e)
        return channels, programmes
    for c in root.findall(".//channel"):
        cid = c.get("id")
        name = (c.findtext("display-name") or cid).strip()
        logo = None
        icon = c.find("icon")
        if icon is not None and icon.get("src"):
            logo = icon.get("src")
        channels[cid] = {"id": cid, "name": name, "logo": logo}
    for p in root.findall(".//programme"):
        try:
            cid = p.get("channel")
            title = (p.findtext("title") or "").strip()
            desc = (p.findtext("desc") or "").strip()
            start = p.get("start") or ""
            stop = p.get("stop") or ""
            programmes.setdefault(cid, []).append({"title": title, "desc": desc, "start": start, "stop": stop})
        except Exception:
            continue
    return channels, programmes

async def do_refresh():
    logger.info("Starting manual refresh of Movistar EPG...")
    try:
        raw = await fetch_movistar_bytes()
    except Exception as e:
        logger.error("Failed to download Movistar EPG: %s", e)
        return False, str(e)
    data = safe_decompress(raw)
    channels, programmes = parse_movistar(data)
    if not channels:
        logger.error("Parsed Movistar EPG is empty or invalid")
        return False, "parsed empty"
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    try:
        with open(CACHE_PATH, "wb") as f:
            f.write(gzip.compress(data))
    except Exception as e:
        logger.warning("Failed to write cache: %s", e)
    _CACHE["raw"] = data
    _CACHE["channels"] = channels
    _CACHE["programmes"] = programmes
    _CACHE["fetched_at"] = int(time.time())
    logger.info("Refresh complete: channels=%d programmes=%d", len(channels), sum(len(v) for v in programmes.values()))
    return True, "ok"

# Load cache if exists, but do NOT refresh automatically to avoid OOM on startup
if os.path.exists(CACHE_PATH):
    try:
        with open(CACHE_PATH, "rb") as f:
            data = gzip.decompress(f.read())
            ch, pg = parse_movistar(data)
            if ch:
                _CACHE["raw"] = data
                _CACHE["channels"] = ch
                _CACHE["programmes"] = pg
                _CACHE["fetched_at"] = int(time.time())
                logger.info("Loaded Movistar EPG from disk cache (%d channels)", len(ch))
    except Exception as e:
        logger.warning("Failed to load disk cache: %s", e)

# Optional background refresher (disabled by default)
@app.on_event("startup")
async def startup_event():
    if ENABLE_BG_REFRESH:
        async def bg_loop():
            while True:
                try:
                    await do_refresh()
                except Exception as e:
                    logger.exception("Background refresh error: %s", e)
                await asyncio.sleep(REFRESH_INTERVAL)
        asyncio.create_task(bg_loop())

# Endpoints
@app.get("/health")
async def health():
    return {"status": "ok", "channels": len(_CACHE["channels"]), "last_update": _CACHE["fetched_at"]}

@app.get("/catalog/channels")
async def catalog():
    if not _CACHE["channels"]:
        raise HTTPException(status_code=503, detail="EPG no disponible")
    metas = [{"id": cid, "type": "tv", "title": ch["name"], "poster": ch["logo"] or "/logo.png", "description": ""} for cid, ch in _CACHE["channels"].items()]
    return {"metas": metas}

@app.get("/meta/{cid}")
async def meta(cid: str):
    if cid not in _CACHE["channels"]:
        raise HTTPException(status_code=404, detail="Canal no encontrado")
    progs = _CACHE["programmes"].get(cid, [])[:MAX_PROGRAMS]
    return {"id": cid, "type": "tv", "title": _CACHE["channels"][cid]["name"], "poster": _CACHE["channels"][cid]["logo"] or "/logo.png", "programming": progs}

@app.post("/refresh")
async def refresh_endpoint():
    ok, msg = await do_refresh()
    if not ok:
        raise HTTPException(status_code=500, detail=f"Refresh failed: {msg}")
    return {"status": "ok", "channels": len(_CACHE["channels"]), "last_update": _CACHE["fetched_at"]}

@app.get("/logo.png")
async def logo():
    if os.path.exists("logo.png"):
        return FileResponse("logo.png", media_type="image/png")
    raise HTTPException(status_code=404, detail="logo not found")
