#!/usr/bin/env python3
"""
Guía iLuvatar - FastAPI backend para EMET Surf
- Combina EPG de TDTChannels + Movistar (ejemplo)
- Autorefresh en background (por defecto 3h)
- Endpoints:
    /manifest.json  (opcional, el manifest final lo sirve Hostinger)
    /catalog/channels
    /meta/{channel_id}
    /epg.xml.gz
    /logo.png
    /health
"""
import os
import gzip
import io
import time
import asyncio
import logging
import xml.etree.ElementTree as ET
from logging.handlers import RotatingFileHandler
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, JSONResponse
import httpx

# --- Config (puedes sobreescribir con env vars en el host)
CFG = {
    "TDT_URL": os.environ.get("TDT_URL", "https://www.tdtchannels.com/epg/TV.xml.gz"),
    "MOV_URL": os.environ.get("MOV_URL", "https://raw.githubusercontent.com/MPAndrew/EpgGratis/master/guide.xml.gz"),
    "REFRESH_INTERVAL": int(os.environ.get("REFRESH_INTERVAL", str(60*60*3))),  # segundos
    "CACHE_PATH": os.environ.get("CACHE_PATH", "cache/epg.xml.gz"),
    "MAX_PROGRAMS": int(os.environ.get("MAX_PROGRAMS", "24")),
    "HTTPX_TIMEOUT": int(os.environ.get("HTTPX_TIMEOUT", "20")),
    "MANIFEST_ID": os.environ.get("MANIFEST_ID", "guia-iluvatar"),
    "MANIFEST_NAME": os.environ.get("MANIFEST_NAME", "Guía iLuvatar"),
    "MANIFEST_LOGO": os.environ.get("MANIFEST_LOGO", "https://kodifacil.com/emet/logo.png"),
}

# --- Logging
logger = logging.getLogger("guia_iluvatar")
logger.setLevel(logging.INFO)
fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
ch = logging.StreamHandler()
ch.setFormatter(fmt)
logger.addHandler(ch)
fh = RotatingFileHandler("guia_iluvatar.log", maxBytes=2*1024*1024, backupCount=2)
fh.setFormatter(fmt)
logger.addHandler(fh)

app = FastAPI(title=CFG["MANIFEST_NAME"])

# In-memory cache
_CACHE = {"raw": None, "channels": {}, "programmes": {}, "fetched_at": 0, "sources": []}

# Helper: fetch bytes with httpx async
async def fetch_bytes(url: str) -> bytes:
    timeout = CFG["HTTPX_TIMEOUT"]
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url)
            r.raise_for_status()
            return r.content
    except Exception as e:
        logger.warning("fetch failed %s: %s", url, e)
        raise

# Try decompress gzip if needed
def try_decompress(data: bytes) -> bytes:
    if not data:
        return b""
    if data.startswith(b'\x1f\x8b'):
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as gf:
                return gf.read()
        except Exception:
            return data
    return data

# Parse XMLTV bytes into channels + programmes dicts
def parse_xmltv(xml_bytes: bytes):
    channels = {}
    programmes = {}
    if not xml_bytes:
        return channels, programmes
    try:
        root = ET.fromstring(xml_bytes)
    except Exception as e:
        logger.exception("XML parse error: %s", e)
        raise
    for ch in root.findall('channel'):
        cid = ch.get('id')
        name = None
        for dn in ch.findall('display-name'):
            if dn.text and dn.text.strip():
                name = dn.text.strip()
                break
        icon = ch.find('icon')
        logo = icon.get('src') if icon is not None else None
        channels[cid] = {"id": cid, "name": name or cid, "logo": logo}
    for p in root.findall('programme'):
        ch = p.get('channel')
        programmes.setdefault(ch, []).append({
            "title": (p.findtext('title') or "").strip(),
            "desc": (p.findtext('desc') or "").strip(),
            "start": p.get('start'),
            "stop": p.get('stop')
        })
    return channels, programmes

# Refresh EPG: fetch sources, merge and parse
async def refresh(force=False):
    now = time.time()
    if not force and _CACHE["raw"] and _CACHE["fetched_at"] + CFG["REFRESH_INTERVAL"] > now:
        logger.info("Cache still fresh, skipping refresh (age %ds)", int(now - _CACHE["fetched_at"]))
        return
    logger.info("Refreshing EPG from sources...")
    sources = []
    tdt_b = b""; mov_b = b""
    # Fetch TDT
    try:
        raw = await fetch_bytes(CFG["TDT_URL"])
        tdt_b = try_decompress(raw)
        sources.append({"url": CFG["TDT_URL"], "ok": True, "size": len(tdt_b)})
    except Exception:
        tdt_b = b""
        sources.append({"url": CFG["TDT_URL"], "ok": False, "size": 0})
    # Fetch Movistar sample
    try:
        raw = await fetch_bytes(CFG["MOV_URL"])
        mov_b = try_decompress(raw)
        sources.append({"url": CFG["MOV_URL"], "ok": True, "size": len(mov_b)})
    except Exception:
        mov_b = b""
        sources.append({"url": CFG["MOV_URL"], "ok": False, "size": 0})
    # Merge feeds under single <tv> root
    combined = None
    if tdt_b or mov_b:
        try:
            root_main = ET.Element('tv')
            for feed in (tdt_b, mov_b):
                if not feed:
                    continue
                try:
                    rt = ET.fromstring(feed)
                    for child in list(rt):
                        root_main.append(child)
                except Exception as e:
                    logger.warning("feed parse skip: %s", e)
            combined = ET.tostring(root_main, encoding='utf-8', xml_declaration=True)
        except Exception as e:
            logger.exception("Merge failed: %s", e)
            combined = tdt_b or mov_b or b""
    else:
        combined = tdt_b or mov_b or b""
    if not combined:
        logger.error("No EPG data available after fetch attempts.")
        return
    # write disk cache compressed
    try:
        os.makedirs(os.path.dirname(CFG["CACHE_PATH"]), exist_ok=True)
        with open(CFG["CACHE_PATH"], "wb") as f:
            f.write(gzip.compress(combined))
    except Exception as e:
        logger.warning("Failed to write disk cache: %s", e)
    # parse to memory
    try:
        channels, programmes = parse_xmltv(combined)
        _CACHE.update({"raw": combined, "channels": channels, "programmes": programmes, "fetched_at": int(now), "sources": sources})
        logger.info("EPG refreshed: channels=%d programmes_total=%d", len(channels), sum(len(v) for v in programmes.values()))
    except Exception as e:
        logger.exception("Parsing merged EPG failed: %s", e)

# Startup: load disk cache if exists, then initial refresh and background loop
@app.on_event("startup")
async def startup_event():
    # try load disk cache
    try:
        if os.path.exists(CFG["CACHE_PATH"]):
            with open(CFG["CACHE_PATH"], "rb") as f:
                data = gzip.decompress(f.read())
                ch, pg = parse_xmltv(data)
                _CACHE.update({"raw": data, "channels": ch, "programmes": pg, "fetched_at": int(time.time())})
                logger.info("Loaded EPG from disk cache (%d channels)", len(ch))
    except Exception as e:
        logger.warning("Failed to load disk cache: %s", e)
    # initial refresh and periodic refresher
    await refresh(force=True)
    async def loop():
        while True:
            try:
                await asyncio.sleep(CFG["REFRESH_INTERVAL"])
                await refresh(force=True)
            except Exception as e:
                logger.exception("Background refresh error: %s", e)
    asyncio.create_task(loop())

# --- Endpoints
@app.get("/manifest.json")
async def manifest():
    # Useful for direct backend testing, Hostinger will serve the public manifest.
    m = {
        "id": CFG["MANIFEST_ID"],
        "version": "1.0.0",
        "name": CFG["MANIFEST_NAME"],
        "description": "Guía EPG de canales España (TDT+Movistar)",
        "logo": CFG["MANIFEST_LOGO"],
        "resources": ["catalog", "meta", "stream"],
        "types": ["tv"],
        "catalogs": [{"type":"tv","id":"channels","name":"Canales España"}],
        "localizedDescription": {"es":"Guía iLuvatar"}
    }
    return JSONResponse(m)

@app.get("/catalog/channels")
async def catalog():
    if not _CACHE["channels"]:
        raise HTTPException(status_code=503, detail="EPG not ready")
    metas = []
    for cid, ch in _CACHE["channels"].items():
        metas.append({"id": cid, "type": "tv", "title": ch.get("name"), "poster": ch.get("logo") or "/logo.png", "description": ""})
    return JSONResponse({"metas": metas})

@app.get("/meta/{cid}")
async def meta(cid: str):
    ch = _CACHE["channels"].get(cid)
    if not ch:
        raise HTTPException(status_code=404, detail="channel not found")
    progs = _CACHE["programmes"].get(cid, [])[:CFG["MAX_PROGRAMS"]]
    return JSONResponse({"id": cid, "type": "tv", "title": ch.get("name"), "poster": ch.get("logo") or "/logo.png", "programming": progs})

@app.get("/epg.xml.gz")
async def epg():
    if _CACHE["raw"]:
        return Response(content=gzip.compress(_CACHE["raw"]), media_type="application/gzip", headers={"Content-Encoding":"gzip"})
    if os.path.exists(CFG["CACHE_PATH"]):
        return FileResponse(CFG["CACHE_PATH"], media_type="application/gzip")
    raise HTTPException(status_code=503, detail="epg not available")

@app.get("/logo.png")
async def logo():
    if os.path.exists("logo.png"):
        return FileResponse("logo.png", media_type="image/png")
    raise HTTPException(status_code=404, detail="logo not found")

@app.get("/health")
async def health():
    return JSONResponse({"status": "ok", "cached_at": _CACHE.get("fetched_at"), "channels": len(_CACHE.get("channels", {})), "sources": _CACHE.get("sources")})
