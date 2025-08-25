# main.py
# Python 3.10+
# pip install feedparser requests beautifulsoup4 lxml readability-lxml python-dateutil

import os, json, time, hashlib, logging
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus, urlparse
import requests, feedparser
from bs4 import BeautifulSoup
from readability import Document
from dateutil import parser as dtparser

# =======================
# CONFIGURAÇÃO RÁPIDA
# =======================
WP_ENDPOINT = "https://SEUSITE.com/wp-json/rs/v1/post"   # <- teu WP
WP_BEARER   = "SUA_CHAVE_FIXA_AQUI"                      # <- mesma chave que você colou no PHP
CATEGORIA   = "Notícias"                                 # nome ou ID
TAGS_FIXAS  = "Litoral Norte, São Paulo"                 # vírgula separada
MAX_POR_RUN = 4
RECENT_HOURS = 6

KEYWORDS = [
    "litoral norte de sao paulo", "ilhabela", "sao sebastiao", "caraguatatuba",
    "ubatuba", "governo do estado de são paulo", "regata", "surf", "vôlei",
    "brasil", "mundo", "esporte", "futebol", "F1", "formula 1"
]

# Limites mais flexíveis pra gerar volume
MIN_CHARS = 140
MIN_PARAGRAPHS = 1

# =======================
# LOG & ESTADO
# =======================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)

STATE_FILE = "state_seen.json"
if os.path.exists(STATE_FILE):
    try:
        SEEN = set(json.load(open(STATE_FILE)))
    except Exception:
        SEEN = set()
else:
    SEEN = set()

def save_state():
    try:
        json.dump(list(SEEN), open(STATE_FILE, "w"))
    except Exception:
        pass

def h(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

# =======================
# GNEWS
# =======================
def gnews_search_url(q: str) -> str:
    # Escapa espaços e acentos de forma segura
    return f"https://news.google.com/rss/search?q={quote_plus(q)}&hl=pt-BR&gl=BR&ceid=BR:pt-419"

def fetch_rss(q: str, limit: int = 3):
    url = gnews_search_url(q)
    logging.info("[GNEWS] %s", url)
    feed = feedparser.parse(url)
    return feed.entries[:limit]

# =======================
# EXTRAÇÃO DE CONTEÚDO
# =======================
def extract_article(url: str):
    """
    Baixa a página e tenta extrair:
      - html limpo (body)
      - texto plano
      - imagem (og:image)
      - fonte (domínio)
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; RSBot/1.0; +https://SEUSITE.com)"
    }
    r = requests.get(url, headers=headers, timeout=20)
    r.raise_for_status()
    html = r.text

    # Tenta pegar imagem (og:image)
    soup = BeautifulSoup(html, "lxml")
    ogimg = soup.find("meta", property="og:image")
    image_url = ogimg["content"].strip() if ogimg and ogimg.get("content") else ""

    # Readability pra extrair o corpo principal
    doc = Document(html)
    content_html = doc.summary(html_partial=True)
    # Limpa lixo simples
    content_soup = BeautifulSoup(content_html, "lxml")
    # Remove links de compartilhamento óbvios
    for bad in content_soup.select("script, style, noscript"):
        bad.decompose()
    txt = content_soup.get_text("\n").strip()
    # Normaliza parágrafos
    for p in content_soup.find_all("p"):
        if not p.get_text(strip=True):
            p.decompose()
    content_html = str(content_soup)

    # Fonte = domínio
    netloc = urlparse(url).netloc
    source_name = netloc.replace("www.", "")

    return content_html, txt, image_url, source_name

def safe_excerpt(text: str, maxlen: int = 160) -> str:
    t = " ".join(text.split())
    if len(t) <= maxlen:
        return t
    cut = t[:maxlen]
    # corta na última palavra
    sp = cut.rfind(" ")
    if sp > 60:
        cut = cut[:sp]
    return cut + "…"

def published_within(entry, hours=RECENT_HOURS) -> bool:
    try:
        if hasattr(entry, "published"):
            dt = dtparser.parse(entry.published)
        elif hasattr(entry, "updated"):
            dt = dtparser.parse(entry.updated)
        else:
            return True  # sem data, passa
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt >= datetime.now(timezone.utc) - timedelta(hours=hours)
    except Exception:
        return True

# =======================
# PUBLICAÇÃO WP
# =======================
def post_to_wp(payload: dict) -> dict:
    headers = {
        "Authorization": f"Bearer {WP_BEARER}",
        "Content-Type": "application/json",
    }
    r = requests.post(WP_ENDPOINT, headers=headers, json=payload, timeout=30)
    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text}
    if r.status_code >= 300:
        logging.error("WP ERRO %s: %s", r.status_code, data)
    else:
        logging.info("[PUBLISH] %s", data.get("view_link", "OK"))
    return {"status": r.status_code, "data": data}

def build_html(body_html: str, source_name: str, original_url: str) -> str:
    footer = []
    if source_name:
        footer.append(f'<p><em>Fonte: {source_name}</em></p>')
    if original_url:
        footer.append(
            f'<p><a href="{original_url}" target="_blank" rel="nofollow noopener">Leia a matéria original</a></p>'
        )
    return body_html + "\n\n" + "\n".join(footer)

# =======================
# PIPELINE
# =======================
def process_keyword(q: str, max_posts: int) -> int:
    posted = 0
    for entry in fetch_rss(q, limit=6):
        if posted >= max_posts:
            break
        link = entry.get("link") or entry.get("id") or ""
        title = entry.get("title", "").strip()

        # id único por URL
        key = h(link)
        if key in SEEN:
            continue
        if not published_within(entry, RECENT_HOURS):
            continue

        # baixa matéria
        try:
            body_html, body_text, image_url, source_guess = extract_article(link)
        except Exception as e:
            logging.warning("Falha ao extrair: %s (%s)", title, e)
            continue

        # valida conteúdo (com fallback pro resumo do feed)
        summary_text = BeautifulSoup(entry.get("summary", ""), "lxml").get_text(" ").strip()
        if len(body_text) < MIN_CHARS or body_text.count("\n") + 1 < MIN_PARAGRAPHS:
            if len(summary_text) >= MIN_CHARS:
                body_text = summary_text
                body_html = f"<p>{summary_text}</p>"
            else:
                logging.warning("[JOB] Conteúdo insuficiente (kw: %s) em %s", q, link)
                continue

        # excerpt/meta desc
        excerpt = safe_excerpt(body_text, 160)

        # HTML final com rodapé de fonte
        content_html = build_html(body_html, entry.get("source", {}).get("title", "") or source_guess, link)

        payload = {
            "title": title,
            "content_html": content_html,
            "excerpt": excerpt,
            "category": CATEGORIA,      # nome ou ID
            "tags": TAGS_FIXAS,         # vírgula
            "image_url": image_url,
            "status": "publish",
            "source_name": entry.get("source", {}).get("title", "") or source_guess,
            "original_url": link,
        }

        resp = post_to_wp(payload)
        if resp["status"] < 300 and resp["data"].get("ok"):
            SEEN.add(key)
            save_state()
            posted += 1
        else:
            logging.error("Falha ao publicar: %s", resp)

    return posted

def main():
    total = 0
    for kw in KEYWORDS:
        total += process_keyword(kw, max_posts=max(1, MAX_POR_RUN//2))
        if total >= MAX_POR_RUN:
            break
        time.sleep(2)
    if total == 0:
        logging.info("[JOB] Nenhuma keyword RECENTE com texto suficiente.")
    else:
        logging.info("[JOB] Publicadas %d matéria(s).", total)

if __name__ == "__main__":
    main()
