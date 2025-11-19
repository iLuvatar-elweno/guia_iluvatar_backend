#!/usr/bin/env python3
"""
Guía iLuvatar - Backend solo Movistar EPG
Optimizado para Render Free (usa <150MB RAM)
"""

import os, gzip, io, time, asyncio, logging
from logging.handlers import RotatingFileHandler
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, JSONResponse
import httpx
from lxml import etree

CFG = {
    "MOV_URL": "https://raw.githubusercontent.com/MPAndrew/EpgGratis/master/guide.xml.gz",
    "REFRESH_INTERVAL": 3600 * 3,
    "CACHE_PATH": "cache/movistar_epg.xml.gz",
    "MAX_PROGRAMS": 24,
    "HTTPX_TIMEOUT": 20,
}

# Logging
logger = logging.getLogger("iLuvatar")
logger.setLevel(logging.INFO)
fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
ch = logging.StreamHandler()
ch.setFormatter(fmt)
logger.addHandler(ch)
fh = RotatingFileHandler("iluvatar.log", maxBytes=3_000_000, backupCount=2)
fh.setFormatter(fmt)
logger.addHandler(fh)

app = FastAPI(title="Guía iLuvatar (Movistar EPG Only)")

_CACHE = {
    "raw": None,
    "channels": {},
    "programmes": {},
    "fetched_at": 0,
}


async def fetch_movistar():
    """Descarga el XML Movistar y lo descomprime."""
    try:
        async with httpx.AsyncClient(timeout=CFG["HTTPX_TIMEOUT"]) as client:
            r = await client.get(CFG["MOV_URL"])
            r.raise_for_status()
            data = gzip.decompress(r.content)
            return data
    except Exception as e:
        logger.error("Error descargando Movistar: %s", e)
        return None


def parse_epg(xml_bytes: bytes):
    """Parsea Movistar EPG usando lxml con recover=True."""
    channels = {}
    programmes = {}

    try:
        parser = etree.XMLParser(recover=True)
        root = etree.fromstring(xml_bytes, parser=parser)
    except Exception as e:
        logger.error("Error parseando XML Movistar: %s", e)
        return channels, programmes

    # canales
    for c in root.findall(".//channel"):
        cid = c.get("id")
        name = c.findtext("display-name") or cid
        logo = None

        icon = c.find("icon")
        if icon is not None and icon.get("src"):
            logo = icon.get("src")

        channels[cid] = {
            "id": cid,
            "name": name,
            "logo": logo
        }

    # programación
    for p in root.findall(".//programme"):
        cid = p.get("channel")
        programmes.setdefault(cid, []).append({
            "title": p.findtext("title") or "",
            "desc": p.findtext("desc") or "",
            "start": p.get("start") or "",
            "stop": p.get("stop") or ""
        })

    return channels, programmes


async def refresh(force=False):
    """Actualiza el EPG."""
    now = time.time()

    if not force and (_CACHE["fetched_at"] + CFG["REFRESH_INTERVAL"] > now):
        return

    logger.info("Actualizando EPG Movistar…")

    xml_raw = await fetch_movistar()
    if not xml_raw:
        logger.error("No se pudo descargar Movistar EPG")
        return

    channels, programmes = parse_epg(xml_raw)
    if not channels:
        logger.error("EPG Movistar vacío o corrupto")
        return

    # Guardar
    _CACHE["raw"] = xml_raw
    _CACHE["channels"] = channels
    _CACHE["programmes"] = programmes
    _CACHE["fetched_at"] = now

    # Guardar en disco (gzip)
    os.makedirs("cache", exist_ok=True)
    with open(CFG["CACHE_PATH"], "wb") as f:
        f.write(gzip.compress(xml_raw))

    logger.info("EPG Movistar actualizado correctamente (%d canales)", len(channels))


# Cargar del cache en disco si existe
if os.path.exists(CFG["CACHE_PATH"]):
    try:
        with open(CFG["CACHE_PATH"], "rb") as f:
            data = gzip.decompress(f.read())
            ch, pg = parse_epg(data)
            if ch:
                _CACHE["raw"] = data
                _CACHE["channels"] = ch
                _CACHE["programmes"] = pg
                _CACHE["fetched_at"] = int(time.time())
                logger.info("EPG Movistar cargado desde cache (%d canales)", len(ch))
    except:
        pass


# Tareas al iniciar
@app.on_event("startup")
async def startup():
    await refresh(force=True)
    asyncio.create_task(bg_refresh())


async def bg_refresh():
    while True:
        await asyncio.sleep(CFG["REFRESH_INTERVAL"])
        await refresh(force=True)


### ENDPOINTS ###

@app.get("/catalog/channels")
async def catalog():
    if not _CACHE["channels"]:
        raise HTTPException(503, "EPG no disponible")
    metas = []
    for cid, ch in _CACHE["channels"].items():
        metas.append({
            "id": cid,
            "type": "tv",
            "title": ch["name"],
            "poster": ch["logo"] or "/logo.png",
            "description": ""
        })
    return {"metas": metas}


@app.get("/meta/{cid}")
async def meta(cid: str):
    if cid not in _CACHE["channels"]:
        raise HTTPException(404, "Canal no encontrado")
    progs = _CACHE["programmes"].get(cid, [])[:CFG["MAX_PROGRAMS"]]
    return {
        "id": cid,
        "type": "tv",
        "title": _CACHE["channels"][cid]["name"],
        "poster": _CACHE["channels"][cid]["logo"] or "/logo.png",
        "programming": progs
    }


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "channels": len(_CACHE["channels"]),
        "last_update": _CACHE["fetched_at"]
    }
