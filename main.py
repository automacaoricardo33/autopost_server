import os, re, time, json, html, logging, hashlib
from datetime import datetime, timezone, timedelta
from urllib.parse import quote_plus, urlparse
import requests
import feedparser
from flask import Flask, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from dateutil import parser as dtparser
import trafilatura

# ---------------------------
# Config
# ---------------------------
PORT = int(os.environ.get("PORT", "10000"))
CRON_MINUTES = int(os.environ.get("CRON_MINUTES", "5"))
RESOLVE_WAIT_SECONDS = int(os.environ.get("RESOLVE_WAIT_SECONDS", "3"))  # tempo de “espera” entre chamadas
RECENCY_HOURS = int(os.environ.get("RECENCY_HOURS", "6"))  # só pega notícia recente
KEYWORDS = [k.strip() for k in os.environ.get("KEYWORDS", "").split(",") if k.strip()]
FEEDS = [f.strip() for f in os.environ.get("FEEDS", "").split(",") if f.strip()]
MIN_CHARS = int(os.environ.get("MIN_CHARS", "350"))
MIN_P_COUNT = int(os.environ.get("MIN_P_COUNT", "2"))
UA = os.environ.get("UA", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# cidade/termos locais para tags
LOCAL_CITIES = ["caraguatatuba","são sebastião","sao sebastiao","ilhabela","ubatuba","litoral norte","litoral norte de são paulo"]

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("main")

app = Flask(__name__)
scheduler = BackgroundScheduler(timezone="UTC")

STATE = {
    "last_item": None,   # dict pronto pro WP
    "last_raw": None,    # debug
    "last_run": None
}

# ---------------------------
# Utils
# ---------------------------
def http_get(url, allow_redirects=True, timeout=25):
    headers = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
    r = requests.get(url, headers=headers, allow_redirects=allow_redirects, timeout=timeout)
    r.raise_for_status()
    return r

def is_recent(dt: datetime) -> bool:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt) <= timedelta(hours=RECENCY_HOURS)

def normspace(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()

def domain_name(u: str) -> str:
    try:
        return urlparse(u).netloc.replace("www.","")
    except:
        return "fonte"

def summarize_sentences(text: str, n=2) -> str:
    # corte muito simples para lide/meta
    parts = re.split(r"(?<=[\.\!\?])\s+", text.strip())
    return " ".join(parts[:n]).strip()

def build_tags(title: str, body: str, base_keywords):
    t = (title + " " + body).lower()
    tags = set()
    for kw in base_keywords:
        if kw.lower() in t:
            tags.add(kw.lower())
    for city in LOCAL_CITIES:
        if city in t:
            tags.add(city)
    # complementa com termos gerais
    for extra in ["brasil","mundo","governo","polícia","esporte","economia","tempo","trânsito"]:
        if extra in t:
            tags.add(extra)
    # limita 10 tags
    return list(tags)[:10] if tags else list(set((base_keywords or [])[:10]))

def render_markdown(title, corpo, fonte_nome, meta_desc, tags_list):
    # Estrutura exigida
    md = []
    md.append(f"### {title}")
    md.append("")
    for p in corpo:
        md.append(normspace(p))
        md.append("")
    md.append(f"Fonte: {fonte_nome}")
    md.append("")
    md.append("---")
    md.append("")
    md.append(f"**Meta descrição:** {meta_desc}")
    md.append("")
    md.append("---")
    md.append("")
    md.append("**Tags:** " + ", ".join(tags_list))
    return "\n".join(md).strip()

def rewrite_to_portal_style(title, text, src_name):
    # Reescrita simples e objetiva (sem IA externa), 4–5 parágrafos.
    text = normspace(text)
    if len(text) < 200:
        return None

    lide = summarize_sentences(text, 2)
    resto = text[len(lide):].strip()
    # divide em 3 blocos
    chunks = re.split(r"(?<=[\.\!\?])\s+", resto)
    p2 = " ".join(chunks[:3]).strip()
    p3 = " ".join(chunks[3:7]).strip()
    p4 = " ".join(chunks[7:11]).strip()

    corpo = [lide]
    for p in [p2, p3, p4]:
        if p:
            corpo.append(p)
    if len(corpo) < 4:
        # força ao menos 4 parágrafos: duplica partes se necessário
        while len(corpo) < 4 and text:
            corpo.append(summarize_sentences(resto, 1))

    meta = summarize_sentences(text, 1)
    meta = meta[:158]  # margem pro "…"
    return corpo, meta

# ---------------------------
# Google News Search (RSS)
# ---------------------------
def gnews_search_urls(keywords):
    urls = []
    for kw in keywords:
        q = quote_plus(kw)
        # feed de busca
        u = f"https://news.google.com/rss/search?q={q}&hl=pt-BR&gl=BR&ceid=BR:pt-419"
        urls.append(u)
    return urls

def pick_items_from_feed(feed_url):
    d = feedparser.parse(feed_url)
    items = []
    for e in d.entries:
        link = e.get("link") or ""
        title = html.unescape(e.get("title") or "").strip()
        pub = e.get("published") or e.get("pubDate") or e.get("updated") or ""
        try:
            published = dtparser.parse(pub) if pub else datetime.now(timezone.utc)
        except:
            published = datetime.now(timezone.utc)
        items.append({
            "title": title,
            "link": link,
            "published": published if isinstance(published, datetime) else datetime.now(timezone.utc),
            "source": e.get("source", {}).get("title") if isinstance(e.get("source"), dict) else None
        })
    return items

def resolve_news_google(link):
    """Resolve link do news.google para URL final do publisher."""
    try:
        time.sleep(max(0, RESOLVE_WAIT_SECONDS))
        r = http_get(link, allow_redirects=True, timeout=25)
        # Após redirecionar, use URL final
        return r.url
    except Exception as ex:
        log.warning("[RESOLVE] falhou resolver %s: %s", link, ex)
        return link  # tenta mesmo assim

def extract_text(url):
    try:
        r = http_get(url, allow_redirects=True, timeout=30)
        downloaded = trafilatura.extract(r.text, favor_recall=True, include_links=False)
        if not downloaded:
            return None
        txt = normspace(downloaded)
        # exige um mínimo pra evitar notas curtas
        if len(txt) < MIN_CHARS or txt.count(".") < MIN_P_COUNT:
            return None
        return txt
    except Exception as ex:
        log.warning("[EXTRACT] erro %s: %s", url, ex)
        return None

# ---------------------------
# Job principal
# ---------------------------
def job_run():
    try:
        STATE["last_run"] = datetime.now(timezone.utc).isoformat()
        base_feeds = FEEDS[:] if FEEDS else gnews_search_urls(KEYWORDS or ["Litoral Norte SP"])
        log.info("[JOB] Feeds = %d | KW = %s", len(base_feeds), "; ".join(KEYWORDS) if KEYWORDS else "(padrão)")
        candidates = []

        for f in base_feeds:
            try:
                items = pick_items_from_feed(f)
                for it in items:
                    if not is_recent(it["published"]):
                        continue
                    # se KEYWORDS definidas, filtra pelo título
                    if KEYWORDS:
                        lowt = (it["title"] or "").lower()
                        if not any(kw.lower() in lowt for kw in KEYWORDS):
                            continue
                    # resolve link se for do news.google
                    final_url = resolve_news_google(it["link"]) if "news.google.com" in it["link"] else it["link"]
                    candidates.append({
                        "title": it["title"],
                        "url": final_url,
                        "published": it["published"],
                        "src": it.get("source") or domain_name(final_url)
                    })
            except Exception as ex:
                log.warning("[JOB] feed erro %s: %s", f, ex)

        # ordena por mais recente
        candidates.sort(key=lambda x: x["published"], reverse=True)

        # tenta extrair o primeiro que tiver conteúdo suficiente
        for c in candidates[:25]:
            txt = extract_text(c["url"])
            if not txt:
                continue

            # reescrever no padrão do portal
            rew = rewrite_to_portal_style(c["title"], txt, c["src"])
            if not rew:
                continue

            corpo, meta = rew
            tags = build_tags(c["title"], " ".join(corpo), KEYWORDS)
            titulo_ok = c["title"]
            fonte_nome = c["src"] or domain_name(c["url"])
            md = render_markdown(titulo_ok, corpo, fonte_nome, meta, tags)

            payload = {
                "ok": True,
                "source_url": c["url"],
                "source_domain": domain_name(c["url"]),
                "source_title": c["title"],
                "published_at": c["published"].isoformat(),
                "title": titulo_ok,
                "tags": tags,
                "render_markdown": md
            }
            STATE["last_item"] = payload
            STATE["last_raw"] = {"title": c["title"], "url": c["url"], "text_len": len(txt)}
            log.info("[OK] %s | %s", titulo_ok, c["url"])
            return

        log.warning("[JOB] Nenhum item com conteúdo suficiente.")
        STATE["last_item"] = {"ok": False, "reason": "no_content"}
    except Exception as ex:
        log.exception("[JOB] ERRO: %s", ex)
        STATE["last_item"] = {"ok": False, "reason": "exception"}

# ---------------------------
# HTTP
# ---------------------------
@app.get("/")
def root():
    return "OK - Autopost server"

@app.get("/health")
def health():
    return jsonify({"ok": True, "last_run": STATE["last_run"]})

@app.get("/debug/config")
def debug_config():
    cfg = {
        "PORT": PORT,
        "CRON_MINUTES": CRON_MINUTES,
        "RESOLVE_WAIT_SECONDS": RESOLVE_WAIT_SECONDS,
        "RECENCY_HOURS": RECENCY_HOURS,
        "KEYWORDS": KEYWORDS,
        "FEEDS": FEEDS,
        "MIN_CHARS": MIN_CHARS,
        "MIN_P_COUNT": MIN_P_COUNT
    }
    return jsonify(cfg)

@app.get("/debug/run")
def debug_run():
    job_run()
    return jsonify({"ok": True, "last": STATE["last_item"], "raw": STATE["last_raw"]})

@app.get("/artigos/ultimo.json")
def ultimo_json():
    item = STATE["last_item"]
    return jsonify(item if item else {"ok": False, "reason": "no_run_yet"})

# ---------------------------
# Main
# ---------------------------
if __name__ == "__main__":
    scheduler.add_job(job_run, "interval", minutes=CRON_MINUTES, id="job_run", replace_existing=True)
    scheduler.start()
    # roda 1x no boot para já ter algo
    try:
        job_run()
    except Exception:
        pass
    app.run(host="0.0.0.0", port=PORT)
