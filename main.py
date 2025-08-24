import os
import json
import time
import logging
from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, request, Response
from apscheduler.schedulers.background import BackgroundScheduler
from dateutil import parser as dtparse

from scraper import (
    new_session, search_gnews, resolve_gnews_url, fetch_and_extract,
    clean_html, guess_source_name, make_tags, strip_html_keep_p, try_amp,
)

# ===================== CONFIG RÁPIDA =====================
# janelas/limiares
RECENCY_HOURS = int(os.getenv("RECENCY_HOURS", "12"))
GNEWS_WAIT_SECS = int(os.getenv("GNEWS_WAIT_SECS", "5"))
TIMEOUT_SECONDS = int(os.getenv("TIMEOUT_SECONDS", "20"))

MIN_PARAGRAPHS = int(os.getenv("MIN_PARAGRAPHS", "3"))
MIN_CHARS = int(os.getenv("MIN_CHARS", "400"))
FALLBACK_MIN_CHARS = int(os.getenv("FALLBACK_MIN_CHARS", "300"))
ALLOW_FALLBACK_SNIPPET = os.getenv("ALLOW_FALLBACK_SNIPPET", "true").lower() == "true"

# keywords (separe por vírgula, NÃO por ponto-e-vírgula)
KEYWORDS = os.getenv(
    "KEYWORDS",
    (
        "litoral norte de sao paulo, ilhabela, são sebastião, sao sebastiao, "
        "caraguatatuba, ubatuba, governo do estado de são paulo, brasil, "
        "futebol, fórmula 1, f1, vôlei, surf, regata"
    ),
)

# ========================================================

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = app.logger

session = new_session()
last_article = {}  # cache do último artigo pronto

def utcnow():
    return datetime.now(timezone.utc)

def is_recent(pub_dt, max_age_hours=12):
    try:
        if isinstance(pub_dt, str):
            pub_dt = dtparse.parse(pub_dt)
        if pub_dt.tzinfo is None:
            pub_dt = pub_dt.replace(tzinfo=timezone.utc)
    except Exception:
        return True  # se não sei a data, não bloqueio por recência
    return (utcnow() - pub_dt) <= timedelta(hours=max_age_hours)

def build_json_payload(title, content_html, meta_desc, tags, image_url, source, url, published_at):
    return {
        "title": title,
        "content_html": content_html,
        "meta_description": meta_desc or "",
        "tags": tags or [],
        "image_url": image_url or "",
        "source": source or "",
        "url": url,
        "published_at": published_at.isoformat() if isinstance(published_at, datetime) else str(published_at),
    }

def process_gnews_item(item):
    """
    Recebe um item do GNews (dict) e tenta:
    - resolver a URL real
    - extrair conteúdo limpo
    - validar tamanho
    - montar payload JSON
    Retorna dict ou None.
    """
    article_url = resolve_gnews_url(item["link"], session, wait_secs=GNEWS_WAIT_SECS, timeout=TIMEOUT_SECONDS)
    published_at = item.get("published") or utcnow()

    raw_html, title, top_image = fetch_and_extract(article_url, session, timeout=TIMEOUT_SECONDS)
    cleaned = clean_html(raw_html)

    # valida tamanho
    pcount = cleaned["paragraphs"]
    chrs = cleaned["chars"]

    if pcount < MIN_PARAGRAPHS or chrs < MIN_CHARS:
        # tenta AMP
        amp = try_amp(article_url, session, timeout=TIMEOUT_SECONDS)
        if amp:
            amp_clean = clean_html(amp)
            if amp_clean["paragraphs"] >= MIN_PARAGRAPHS and amp_clean["chars"] >= MIN_CHARS:
                cleaned = amp_clean
            else:
                # como último recurso aceita conteúdo um pouco menor?
                if not (ALLOW_FALLBACK_SNIPPET and amp_clean["chars"] >= FALLBACK_MIN_CHARS):
                    return None
                cleaned = amp_clean
        else:
            if not (ALLOW_FALLBACK_SNIPPET and chrs >= FALLBACK_MIN_CHARS):
                return None

    source = guess_source_name(article_url) or item.get("source", "")
    tags = make_tags(f"{title} {cleaned['text']}")
    meta = (cleaned["text"][:156] + "…") if len(cleaned["text"]) > 160 else cleaned["text"]
    content_html = strip_html_keep_p(cleaned["html"])

    return build_json_payload(
        title=title,
        content_html=content_html,
        meta_desc=meta,
        tags=tags,
        image_url=top_image or cleaned.get("first_img", ""),
        source=source,
        url=article_url,
        published_at=published_at if isinstance(published_at, datetime) else utcnow()
    )

def job_run():
    global last_article
    try:
        kws = [k.strip() for k in KEYWORDS.split(",") if k.strip()]
        if not kws:
            log.warning("[JOB] Sem keywords configuradas.")
            return

        max_age = RECENCY_HOURS
        best_payload = None

        for kw in kws:
            feed_items = search_gnews(kw, session=session)
            # percorre do mais recente para o mais antigo
            for it in feed_items:
                # checa recência (do item do feed)
                if not is_recent(it.get("published_dt") or utcnow(), max_age_hours=max_age):
                    continue
                payload = process_gnews_item(it)
                if payload:
                    best_payload = payload
                    break  # achou um válido para esta KW
            if best_payload:
                break  # já temos um artigo

        if best_payload:
            last_article = best_payload
            log.info("[JOB] Novo artigo armazenado: %s", best_payload.get("title", "")[:80])
        else:
            log.warning("[JOB] Nenhuma keyword RECENTE com texto suficiente.")

    except Exception as e:
        log.exception("[JOB] Erro geral: %s", e)

# ================== Flask endpoints ==================
@app.route("/")
def root():
    return Response(
        "RS Autopost Server — OK<br>"
        f"Keywords: {KEYWORDS}<br>"
        f"Recency(h): {RECENCY_HOURS}<br>"
        f"Timeout(s): {TIMEOUT_SECONDS}<br>"
        f"Último: {'SIM' if last_article else 'NÃO'}",
        mimetype="text/html",
    )

@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": utcnow().isoformat()})

@app.route("/artigos/ultimo.json")
def artigos_ultimo():
    if not last_article:
        return jsonify({})
    return jsonify(last_article)

@app.route("/debug/fetch")
def debug_fetch():
    url = request.args.get("u", "").strip()
    if not url:
        return jsonify({"error": "Parâmetro u é obrigatório"}), 400
    try:
        html, title, top_img = fetch_and_extract(url, session, timeout=TIMEOUT_SECONDS)
        cleaned = clean_html(html)
        return jsonify({
            "url": url,
            "resolved": url,
            "title": title,
            "top_image": top_img,
            "p": cleaned["paragraphs"],
            "chars": cleaned["chars"],
            "sample": cleaned["text"][:400]
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ================== Scheduler ==================
def start_scheduler():
    sched = BackgroundScheduler(daemon=True, timezone="UTC")
    # roda a cada 5 min
    sched.add_job(job_run, "interval", minutes=5, id="job_run", max_instances=1, coalesce=True)
    sched.start()
    # dispara 1x logo no boot
    try:
        job_run()
    except Exception:
        pass

if __name__ == "__main__":
    start_scheduler()
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
