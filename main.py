# -*- coding: utf-8 -*-
"""
main.py — orquestra o agendador e a publicação
"""

from __future__ import annotations

import logging
import os
import sys
import time
from typing import List

from apscheduler.schedulers.background import BackgroundScheduler

from scraper import fetch_rss, is_recent

# -----------------------------------------------------------------------------
# LOGGING
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s:%(name)s:%(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("main")

# -----------------------------------------------------------------------------
# CONFIG (carrega de env ou usa padrões — você pode trocar pelos seus getters)
# -----------------------------------------------------------------------------
def getenv_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)).strip())
    except Exception:
        return default


def getenv_str(key: str, default: str) -> str:
    return os.getenv(key, default)


# espelha os campos do seu painel
FETCH_WAIT_SECONDS = getenv_int("FETCH_WAIT_SECONDS", 20)  # entre publicações finais
WAIT_GNEWS = getenv_int("WAIT_GNEWS", 5)  # entre requests de feed
MAX_PER_RUN = getenv_int("MAX_PER_RUN", 1)

MIN_CHARS = getenv_int("MIN_CHARS", 220)
MIN_PARAGRAPHS = getenv_int("MIN_PARAGRAPHS", 2)
RECENT_HOURS = getenv_int("RECENT_HOURS", 3)

RUN_INTERVAL_MIN = getenv_int("RUN_INTERVAL_MIN", 5)

# região é só metadado
REGIAO = getenv_str("REGIAO", "Litoral Norte de Sao Paulo")

# Keywords: separadas por vírgula
KEYWORDS_RAW = getenv_str(
    "KEYWORDS",
    "litoral norte de sao paulo, ilhabela, sao sebastiao, caraguatatuba, ubatuba, "
    "futebol, formula 1, f1, governo do estado de são paulo, regata, surf, vôlei, brasil, mundo",
)

# normaliza keywords
KEYWORDS: List[str] = [k.strip() for k in KEYWORDS_RAW.split(",") if k.strip()]

# -----------------------------------------------------------------------------
# FUNÇÕES DE APOIO
# -----------------------------------------------------------------------------
def _count_paragraphs(text: str) -> int:
    # considera linhas em branco como quebra
    parts = [p for p in text.replace("\r", "").split("\n\n") if p.strip()]
    # fallback: se veio tudo em uma linha, tenta por ponto final
    if len(parts) <= 1:
        parts = [p for p in text.split(". ") if p.strip()]
    return len(parts)


def _has_sufficient_content(summary: str) -> bool:
    if not summary:
        return False
    if len(summary) < max(0, MIN_CHARS):
        return False
    if _count_paragraphs(summary) < max(1, MIN_PARAGRAPHS):
        return False
    return True


def _publish_item(item: dict) -> None:
    """
    ADAPTE AQUI se você tem a sua função de publicação.
    Atualmente só loga. No seu sistema, chame o plugin/camada que envia a matéria.
    """
    logger.info(f"[PUBLISH] {item.get('title', '')} — {item.get('link', '')}")


# -----------------------------------------------------------------------------
# JOB
# -----------------------------------------------------------------------------
def job_run():
    logger.info("[JOB] start")

    for kw in KEYWORDS:
        try:
            items, url = fetch_rss(kw, limit=MAX_PER_RUN)
        except Exception as e:
            logger.exception(f"[JOB] Erro construindo/buscando feed para '{kw}': {e}")
            continue

        if not items:
            logger.info(f"[GNEWS] sem itens: {url}")
            time.sleep(WAIT_GNEWS)
            continue

        # percorre itens retornados da busca
        for it in items:
            logger.info(f"[GNEWS] aguardando {WAIT_GNEWS}s: {it.get('link') or url}")
            time.sleep(WAIT_GNEWS)

            summary = it.get("summary", "") or ""
            recent_ok = is_recent(it.get("published_iso", ""), RECENT_HOURS)

            if not recent_ok or not _has_sufficient_content(summary):
                logger.warning(
                    f"[JOB] Conteúdo insuficiente (kw: {kw}) em "
                    f"{(it.get('link') or url)}"
                )
                continue

            # publica
            _publish_item(
                {
                    "regiao": REGIAO,
                    "keyword": kw,
                    "title": it.get("title", ""),
                    "link": it.get("link", ""),
                    "summary": summary,
                    "published_iso": it.get("published_iso", ""),
                }
            )

            # espera entre publicações finais
            if FETCH_WAIT_SECONDS > 0:
                time.sleep(FETCH_WAIT_SECONDS)

    logger.info("[JOB] done")


# -----------------------------------------------------------------------------
# SCHEDULER
# -----------------------------------------------------------------------------
scheduler = BackgroundScheduler()


def start_scheduler():
    scheduler.add_job(job_run, "interval", minutes=max(1, RUN_INTERVAL_MIN))
    scheduler.start()
    logger.info("Scheduler iniciado")
    # opcional: rodada imediata na subida
    job_run()


# -----------------------------------------------------------------------------
# FLASK/WSGI opcional (se você já roda via outra app, pode remover)
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    try:
        start_scheduler()
        # Mantém o processo vivo
        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
