Guía iLuvatar (Movistar-only) - Ready for Render (Free)

Files:
- app.py
- requirements.txt
- Dockerfile
- logo.png
- manifest_hostinger.json

Quick steps:

1) Upload to GitHub
   - Create a repo (guia_iluvatar_backend)
   - Upload all files to the repo root
   - Commit to branch main

2) Deploy on Render
   - Go to https://render.com -> New -> Web Service
   - Connect GitHub and select the repo
   - Render will detect Dockerfile; choose Free plan
   - Create Web Service -> wait for deploy

3) Manual refresh (important)
   - Do NOT auto-refresh on startup; after deploy trigger a refresh:
     curl -X POST https://<BACKEND_HOST>/refresh
   - Replace <BACKEND_HOST> with the Render domain (e.g. guia-iluvatar.onrender.com)

4) Update Hostinger manifest
   - Edit manifest_hostinger.json: replace https://<BACKEND_HOST> with your render URL
   - Upload to Hostinger as /emet/manifest.json
   - Upload logo.png to /emet/logo.png

5) Install in EMET
   - Add-on by URL: https://kodifacil.com/emet/manifest.json

Notes:
- Background refresh disabled by default to avoid OOM. To enable, set env var ENABLE_BG_REFRESH=1 in Render settings.
- First refresh may take some seconds; monitor Render logs if needed.
