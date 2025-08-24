import logging
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timezone
from scraper import fetch_rss

log = logging.getLogger("main")
logging.basicConfig(level=logging.INFO)

LATEST_ARTICLE = {"title": "", "body": "", "source": "", "link": "", "created": ""}

_scheduler = None

def job_run():
    global LATEST_ARTICLE
    keywords = [
        "litoral norte de sao paulo",
        "ilhabela",
        "ubatuba",
        "sao sebastiao",
        "caraguatatuba",
        "futebol",
        "formula 1",
        "regata",
        "surf",
        "vôlei",
        "brasil",
        "mundo",
    ]
    for kw in keywords:
        items = fetch_rss(kw, limit=1)
        if not items:
            continue
        it = items[0]
        body = it["summary"] or it["title"]
        # Aceita corpo curto — não bloqueia publicação
        LATEST_ARTICLE = {
            "title": it["title"],
            "body": body,
            "source": it["source"] or "Google News",
            "link": it["link"],
            "created": datetime.now(timezone.utc).isoformat()
        }
        log.info("[JOB] Atualizado com '%s' (%s)", it["title"], it["source"] or "Google News")
        break

def start_scheduler():
    global _scheduler
    if _scheduler and _scheduler.running:
        return _scheduler
    _scheduler = BackgroundScheduler()
    _scheduler.add_job(job_run, "interval", minutes=5, id="job_run", replace_existing=True)
    _scheduler.start()
    log.info("Scheduler iniciado")
    job_run()
    return _scheduler

if __name__ == "__main__":
    start_scheduler()
    import time
    while True:
        time.sleep(60)
