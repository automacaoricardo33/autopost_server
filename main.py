# main.py
import os
import logging
from typing import List, Dict
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.jobstores.memory import MemoryJobStore

from scraper import fetch_latest_gnews

logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
log = logging.getLogger("main")

# Compartilhado com o server.py
LATEST: Dict[str, dict] = {}
KEYWORDS: List[str] = [
    "vôlei",
    "futebol",
    "basquete",
    "f1",
    "economia",
]  # ajuste como quiser

def job_run():
    """Job que roda periodicamente e guarda o último artigo por keyword."""
    try:
        count = 0
        for kw in KEYWORDS:
            data = fetch_latest_gnews(kw)
            if data:
                LATEST[kw] = data
                count += 1
        log.info("[JOB] Atualizado %s keywords em %s", count, datetime.utcnow().isoformat())
    except Exception as e:
        log.exception("Erro no job_run: %s", e)

def start_scheduler_if_needed():
    """Inicia o APScheduler (para modo worker local/comando manual)."""
    jobstores = {"default": MemoryJobStore()}
    executors = {"default": ThreadPoolExecutor(5)}
    scheduler = BackgroundScheduler(jobstores=jobstores, executors=executors, timezone="UTC")
    scheduler.add_job(job_run, "interval", minutes=5, id="job_run", max_instances=1, coalesce=True)
    scheduler.start()
    log.info("Scheduler iniciado")
    # roda uma vez no boot
    job_run()
    return scheduler

if __name__ == "__main__":
    # Opcional: permite rodar localmente: `python main.py`
    start_scheduler_if_needed()
    # Mantém processo vivo quando executado direto
    import time
    while True:
        time.sleep(3600)
