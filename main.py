import time
import re
import html
import feedparser
import requests
from urllib.parse import quote_plus
from bs4 import BeautifulSoup
from readability import Document

# ===================== CONFIG =====================
ENDPOINT_URL = "https://jornalvozdolitoral.com/rs-auto-publisher-endpoint"
SECRET_KEY   = "3b62b8216593f8593397ed2debb074fc"  # pode ser seu token do TextSynth

DEFAULT_CATEGORY = "Litoral Norte de SP"
DEFAULT_TAGS     = "Litoral Norte, Ilhabela, São Sebastião, Caraguatatuba, Ubatuba, SP"

MAX_PER_RUN    = 3
RECENT_HOURS   = 6
MIN_CHARS      = 220
MIN_PARAGRAPHS = 2

KEYWORDS_PRIORITARIAS = [
    "litoral norte de sao paulo",
    "ilhabela",
    "sao sebastiao",
    "caraguatatuba",
    "ubatuba",
]
KEYWORDS_FALLBACK = [
    "prefeitura sp litoral norte",
    "defesa civil são paulo litoral",
    "rodovia rio-santos",
    "ciclone brasil",
    "chuvas sao paulo litoral norte",
    "brasil",
    "mundo",
    "esportes",
]
# ==================================================


UA ="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"

def gnews_url(q: str) -> str:
    return f"https://news.google.com/rss/search?q={quote_plus(q)}&hl=pt-BR&gl=BR&ceid=BR:pt-419"

def get_first(items):
    return items[0] if items else None

def valid_content(text: str) -> bool:
    if not text: return False
    chars = len(text.strip())
    paras = len([p for p in re.split(r'\n{2,}', text.strip()) if p.strip()])
    return chars >= MIN_CHARS and paras >= MIN_PARAGRAPHS

def fetch_article(url: str):
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
        r.raise_for_status()
    except Exception:
        return None

    doc = Document(r.text)
    title = html.unescape(doc.short_title() or "")
    content_html = doc.summary(html_partial=True)
    soup = BeautifulSoup(r.text, "lxml")

    # og:description / meta description
    og_desc = soup.find("meta", attrs={"property":"og:description"})
    if not og_desc:
        og_desc = soup.find("meta", attrs={"name":"description"})
    excerpt = (og_desc["content"].strip() if og_desc and og_desc.has_attr("content") else "")

    # og:image
    og_img = soup.find("meta", attrs={"property":"og:image"})
    image_url = (og_img["content"].strip() if og_img and og_img.has_attr("content") else "")

    # fallback: primeira imagem do conteúdo do readability
    if not image_url:
        s2 = BeautifulSoup(content_html, "lxml")
        img = s2.find("img")
        if img and img.get("src"):
            image_url = img["src"]

    # texto plano para validação
    text_plain = BeautifulSoup(content_html, "lxml").get_text(separator="\n")
    if not valid_content(text_plain):
        return None

    return {
        "title": title if title else soup.title.string if soup.title else "",
        "content_html": content_html,
        "excerpt": excerpt[:280],
        "image_url": image_url,
    }

def fetch_rss_items(keyword: str, limit: int = 5):
    url = gnews_url(keyword)
    feed = feedparser.parse(url)
    for e in feed.entries[:limit]:
        link = getattr(e, "link", "")
        # links do GNews às vezes vêm com redirecionador; pega o "url=" final se existir
        if "url=" in link:
            try:
                from urllib.parse import parse_qs, urlparse, unquote
                qs = parse_qs(urlparse(link).query)
                real = get_first(qs.get("url"))
                if real: link = unquote(real)
            except Exception:
                pass
        yield {"title": getattr(e, "title", ""), "link": link}

def post_to_wp(payload: dict):
    data = {
        "api_key": SECRET_KEY,
        "title": payload["title"],
        "content": payload["content"],
        "excerpt": payload.get("excerpt", ""),
        "image_url": payload.get("image_url",""),
        "source_url": payload.get("source_url",""),
        "category": payload.get("category", DEFAULT_CATEGORY),
        "tags": payload.get("tags", DEFAULT_TAGS),
        "status": payload.get("status", "publish"),
    }
    try:
        r = requests.post(ENDPOINT_URL, data=data, timeout=40)
        ok = r.status_code == 200 and '"success":true' in r.text
        return ok, r.text
    except Exception as ex:
        return False, str(ex)

def build_content(block_html, fonte_url):
    # Modelo simples com "Artigo via IA" + link de origem
    extra = f'<p><strong>Artigo via IA</strong></p>'
    return f"{extra}\n{block_html}"

def run_batch():
    posted = 0
    tried_links = set()

    for kw in (KEYWORDS_PRIORITARIAS + KEYWORDS_FALLBACK):
        for item in fetch_rss_items(kw, limit=5):
            if posted >= MAX_PER_RUN:
                return posted
            link = item["link"]
            if not link or link in tried_links:
                continue
            tried_links.add(link)

            art = fetch_article(link)
            if not art: 
                continue

            content = build_content(art["content_html"], link)
            payload = {
                "title": art["title"] or item["title"],
                "content": content,
                "excerpt": art["excerpt"],
                "image_url": art["image_url"],
                "source_url": link,
                "category": DEFAULT_CATEGORY,
                "tags": DEFAULT_TAGS,
                "status": "publish",
            }
            ok, resp = post_to_wp(payload)
            print( ("[PUBLISHED]" if ok else "[FAILED]"), kw, "->", payload["title"], resp[:200])
            if ok:
                posted += 1
                time.sleep(2)  # suaviza
        if posted >= MAX_PER_RUN:
            break
    return posted

if __name__ == "__main__":
    total = run_batch()
    print(f"Done. Posts publicados: {total}")
