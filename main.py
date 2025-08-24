# server.py
import os
import json
import logging
from pathlib import Path
from fastapi import FastAPI, Response
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.triggers.interval import IntervalTrigger

# Importa sua função existente. NÃO executa main.py como script.
# O seu main.py deve ter def job_run(): ...
try:
    from main import job_run
except Exception as e:
    # fallback seguro para não quebrar o servidor caso import falhe
    logging.exception("Falha ao importar job_run de main.py")
    def job_run():
        logging.warning("[JOB] fallback: job_run não encontrado em main.py")

logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
log = logging.getLogger("server")

# Caminho do último artigo (ajuste se seu main.py grava em outro lugar)
DATA_DIR = Path(os.environ.get("DATA_DIR", "/opt/render/project/src"))
ULTIMO_JSON = DATA_DIR / "ultimo.json"

app = FastAPI(title="Autopost Server", version="1.0.0")

# ---------- agendador ----------
executors = {"default": ThreadPoolExecutor(1)}  # evita concorrência
scheduler = BackgroundScheduler(executors=executors, timezone="UTC")
# roda a cada 5 min; coalesce evita fila; max_instances=1 evita overlap
trigger = IntervalTrigger(minutes=5)

def _safe_job():
    try:
        job_run()
    except Exception:
        log.exception("[JOB] erro durante job_run()")

# registra o job uma única vez
scheduler.add_job(
    _safe_job,
    trigger=trigger,
    id="job_run",
    max_instances=1,
    coalesce=True,
    misfire_grace_time=120,
    replace_existing=True,
)
scheduler.start()
log.info("Scheduler iniciado.")

# ---------- rotas ----------
@app.get("/")
def root():
    return {
        "ok": True,
        "msg": "Seu serviço está de pé e com porta aberta 🙌",
        "endpoints": ["/healthz", "/debug/run", "/artigos/ultimo.json"],
    }

@app.get("/healthz")
def healthz():
    return {"status": "ok"}

@app.get("/debug/run")
def debug_run():
    _safe_job()
    return {"ran": True}

@app.get("/artigos/ultimo.json")
def ultimo_json():
    if not ULTIMO_JSON.exists():
        return Response(json.dumps({"error": "ainda sem arquivo"}), media_type="application/json", status_code=404)
    return Response(ULTIMO_JSON.read_text(encoding="utf-8"), media_type="application/json")
