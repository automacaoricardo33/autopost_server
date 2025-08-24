import os, time, re, json, logging, html
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
import tldextract
import requests
import feedparser
from bs4 import BeautifulSoup
from flask import Flask, jsonify, Response
from apscheduler.schedulers.background import BackgroundScheduler

# -------------------------
# Config via ENV
# -------------------------
KEYWORDS = [k.strip() for k in os.getenv("KEYWORDS", "litoral norte de sao paulo, ilhabela, sao sebastiao, caraguatatuba, ubatuba, brasil, futebol, formula 1, f1").split(",") if k.strip()]
RECENCY_HOURS = int(os.getenv("RECENCY_HOURS", "6"))
CRON_MINUTES = int(os.getenv("CRON_MINUTES", "5"))
RESOLVE_WAIT_SECONDS = int(os.getenv("RESOLVE_WAIT_SECONDS", "3"))
MIN_CHARS = int(os.getenv("MIN_CHARS", "300"))
MIN_P_COUNT = int(os.getenv("MIN_P_COUNT", "2"))

# headers para evitar 403/anti-bot simples
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("main")

app = Flask(__name__)
scheduler = BackgroundScheduler()
last_article = {}  # memória do último artigo válido

# -------------------------
# Utilidades
# -------------------------
def now_utc():
    return datetime.now(timezone.utc)

def within_recency(dt):
    return dt and (now_utc() - dt <= timedelta(hours=RECENCY_HOURS))

def parse_pubdate(entry):
    try:
        if getattr(entry, "published_parsed", None):
            return datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
    except Exception:
        pass
    return None

def domain_name(url):
    try:
        ext = tldextract.extract(url)
        root = ".".join(p for p in [ext.domain, ext.suffix] if p)
        return root or urlparse(url).netloc
    except Exception:
        return urlparse(url).netloc

def clean_text(txt):
    txt = html.unescape(txt or "")
    txt = re.sub(r"\s+\n", "\n", txt)
    txt = re.sub(r"\n{3,}", "\n\n", txt)
    txt = re.sub(r"\s{2,}", " ", txt)
    return txt.strip()

def summarize_to_paragraphs(text, min_par=4, max_par=5):
    # Divide por quebras de linha ou pontos grandes; faz blocos curtos
    parts = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    if not parts:
        # fallback por frases
        sentences = re.split(r"(?<=[\.\!\?])\s+", text)
        parts = []
        buf = []
        for s in sentences:
            buf.append(s)
            if len(" ".join(buf)) >= 400:
                parts.append(" ".join(buf).strip())
                buf = []
        if buf:
            parts.append(" ".join(buf).strip())
    # limita 4–5 parágrafos
    if len(parts) < min_par:
        # quebra artificial em ~350 chars
        chunk = 350
        blocks = [text[i:i+chunk].strip() for i in range(0, len(text), chunk)]
        parts = [b for b in blocks if len(b) > 80]
    parts = parts[:max_par] if len(parts) > max_par else parts
    if len(parts) >= 1:
        # lide mais direto (primeiro parágrafo)
        parts[0] = re.sub(r"^\W+", "", parts[0])
    return parts[:max_par]

# -------------------------
# Extração de HTML
# -------------------------
def extract_main(url, timeout=20):
    r = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
    r.raise_for_status()
    html_doc = r.text
    soup = BeautifulSoup(html_doc, "lxml")

    # remove elementos óbvios
    for tag in soup(["script", "style", "noscript", "iframe"]):
        tag.decompose()
    for sel in ["header", "footer", "nav", ".share", ".social", ".breadcrumbs", ".advertisement", ".ads"]:
        for t in soup.select(sel):
            t.decompose()

    # título
    title = ""
    mt = soup.find("meta", property="og:title")
    if mt and mt.get("content"):
        title = mt["content"].strip()
    if not title and soup.title:
        title = soup.title.get_text(strip=True)

    # imagem
    img = None
    for key in ["og:image", "twitter:image", "image"]:
        m = soup.find("meta", property=key) or soup.find("meta", attrs={"name": key})
        if m and m.get("content"):
            img = m["content"].strip()
            break

    # conteúdo — prioriza <article>, depois main, depois divs grandes com <p>
    container = soup.find("article") or soup.find("main")
    if not container:
        candidates = sorted(soup.find_all(["div", "section"]), key=lambda x: len(x.get_text(" ", strip=True)), reverse=True)
        container = candidates[0] if candidates else soup.body

    paragraphs = []
    for p in container.find_all("p"):
        t = p.get_text(" ", strip=True)
        if t and len(t) > 40:
            paragraphs.append(t)

    text = "\n\n".join(paragraphs)
    text = clean_text(text)

    # contagem mínima
    pcount = len([p for p in text.split("\n") if p.strip()])
    if len(text) < MIN_CHARS or pcount < MIN_P_COUNT:
        raise ValueError("conteudo_insuficiente")

    return {
        "title": clean_text(title) if title else "",
        "text": text,
        "image": img,
    }

# -------------------------
# Montagem no padrão pedido
# -------------------------
LOCAL_CITIES = ["Caraguatatuba", "São Sebastião", "Ilhabela", "Ubatuba", "Litoral Norte"]
def build_article(url, raw):
    title = raw["title"] or ""
    txt = raw["text"]

    # gera 4–5 parágrafos
    paras = summarize_to_paragraphs(txt, 4, 5)
    body = "\n\n".join(paras)

    # fonte
    fonte = domain_name(url)
    fonte_fmt = fonte.replace("www.", "").split("/")[0]

    # meta descrição (até 160 chars)
    md = clean_text(paras[0]) if paras else clean_text(title)
    meta = (md[:157] + "…") if len(md) > 160 else md

    # tags (auto): keywords + cidades detectadas + domínio
    tags = set([k.lower() for k in KEYWORDS])
    detect = [c for c in LOCAL_CITIES if re.search(rf"\b{re.escape(c)}\b", txt, flags=re.I)]
    for c in detect:
        tags.add(c.lower())
    tags.add(fonte_fmt.lower())
    tag_line = ", ".join(sorted(tags))[:500]

    estrutura = {
        "titulo": title.strip() or "(sem título)",
        "corpo": body.strip(),
        "fonte": fonte_fmt,
        "meta_descricao": meta,
        "tags": tag_line,
        "imagem": raw.get("image"),
        "url_origem": url,
        "gerado_em": now_utc().isoformat()
    }
    return estrutura

# -------------------------
# Buscador Google News
# -------------------------
def gnews_search(keyword):
    # Usa “q=keyword” no Google News RSS em PT-BR
    q = requests.utils.quote(keyword)
    url = f"https://news.google.com/rss/search?q={q}&hl=pt-BR&gl=BR&ceid=BR:pt-419"
    return feedparser.parse(url)

def resolve_gnews_link(link):
    # Espera curto se necessário (sites que sobem devagar)
    if RESOLVE_WAIT_SECONDS > 0:
        log.info(f"[GNEWS] aguardando {RESOLVE_WAIT_SECONDS}s: {link}")
        time.sleep(RESOLVE_WAIT_SECONDS)
    # Segue redirects do próprio link do Google News
    try:
        r = requests.get(link, headers=HEADERS, timeout=20, allow_redirects=True)
        r.raise_for_status()
        return r.url
    except Exception:
        return link  # fallback: deixa como está

# -------------------------
# JOB principal
# -------------------------
def job_run():
    global last_article
    log.info("[JOB] start")
    newest = None
    newest_dt = None
    newest_kw = None

    for kw in KEYWORDS:
        feed = gnews_search(kw)
        for entry in feed.entries[:12]:  # pega poucos por keyword
            pub = parse_pubdate(entry)
            if not within_recency(pub):
                continue
            link = entry.link
            real_url = resolve_gnews_link(link)

            try:
                raw = extract_main(real_url, timeout=20)
            except ValueError:
                log.warning(f"[JOB] Conteúdo insuficiente (kw: {kw}) em {real_url}")
                continue
            except Exception as e:
                log.warning(f"[JOB] Falha ao extrair (kw: {kw}) {real_url}: {e}")
                continue

            # escolhe o mais novo
            if newest is None or (pub and pub > newest_dt):
                newest = build_article(real_url, raw)
                newest_dt = pub
                newest_kw = kw

    if newest:
        last_article = newest
        log.info(f"[JOB] OK: {newest.get('titulo')} (kw: {newest_kw})")
    else:
        log.warning("[JOB] Nenhuma keyword RECENTE com texto suficiente.")

# -------------------------
# Flask endpoints
# -------------------------
@app.route("/")
def home():
    return Response("OK – RS Autoposter Bridge (v3)", mimetype="text/plain")

@app.route("/health")
def health():
    return jsonify({"ok": True, "time": now_utc().isoformat(), "keywords": KEYWORDS})

@app.route("/debug/run")
def debug_run():
    job_run()
    return jsonify({"ok": True, "has_article": bool(last_article)})

@app.route("/artigos/ultimo.json")
def ultimo_json():
    if not last_article:
        return jsonify({"ok": False, "msg": "sem_artigo"}), 200
    # Formato simples para plugin WP
    return jsonify({
        "ok": True,
        "artigo": last_article
    })

# -------------------------
# Boot
# -------------------------
if __name__ == "__main__":
    # scheduler
    scheduler.add_job(job_run, "interval", minutes=CRON_MINUTES, next_run_time=now_utc())
    scheduler.start()
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
else:
    # quando rodar via gunicorn no Render
    scheduler.add_job(job_run, "interval", minutes=CRON_MINUTES, next_run_time=now_utc())
    scheduler.start()
