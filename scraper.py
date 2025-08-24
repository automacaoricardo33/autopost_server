import re
import time
from datetime import datetime, timezone
from urllib.parse import urlencode, urlparse, parse_qs, unquote

import requests
from bs4 import BeautifulSoup
from readability import Document

# ---------- headers/sessão ----------
GNEWS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}
DEFAULT_HEADERS = {
    "User-Agent": GNEWS_HEADERS["User-Agent"],
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": GNEWS_HEADERS["Accept-Language"],
    "Referer": "https://news.google.com/",
}

def new_session():
    s = requests.Session()
    s.headers.update(DEFAULT_HEADERS)
    return s

# ---------- util http ----------
def http_get(url, session, timeout=20):
    r = session.get(url, timeout=timeout, allow_redirects=True)
    r.raise_for_status()
    return r

# ---------- GNews ----------
def search_gnews(keyword, session):
    """
    Busca RSS do Google News em português/BR, ordenado por data.
    Retorna lista de itens: {title, link, source, published, published_dt}
    """
    base = "https://news.google.com/rss/search"
    params = {
        "q": keyword,
        "hl": "pt-BR",
        "gl": "BR",
        "ceid": "BR:pt-419",
    }
    url = f"{base}?{urlencode(params)}"
    r = http_get(url, session, timeout=20)
    soup = BeautifulSoup(r.text, "xml")
    items = []
    for item in soup.find_all("item"):
        title = (item.title.text or "").strip()
        link = (item.link.text or "").strip()
        pub = (item.pubDate.text or "").strip() if item.pubDate else ""
        src = ""
        source_tag = item.find("source")
        if source_tag:
            src = source_tag.text.strip()
        # published_dt
        try:
            from email.utils import parsedate_to_datetime
            published_dt = parsedate_to_datetime(pub) if pub else datetime.now(timezone.utc)
        except Exception:
            published_dt = datetime.now(timezone.utc)

        items.append({
            "title": title,
            "link": link,
            "source": src,
            "published": pub,
            "published_dt": published_dt,
        })
    return items

def resolve_gnews_url(gnews_url, session, wait_secs=5, timeout=20):
    """
    Resolve a URL real a partir de um link de item do Google News.
    1) tenta ?url=..., 2) segue redirects, 3) procura href absoluto no HTML.
    """
    # 1) parâmetro url=...
    try:
        q = urlparse(gnews_url).query
        if q:
            qs = parse_qs(q)
            if "url" in qs and qs["url"]:
                candidate = unquote(qs["url"][0])
                if candidate.startswith("http"):
                    return candidate
    except Exception:
        pass

    # 2) seguir redirects
    try:
        r = session.get(gnews_url, headers=GNEWS_HEADERS, allow_redirects=True, timeout=timeout)
        final = r.url
        if "news.google.com" not in urlparse(final).netloc:
            return final
        # 3) procurar href absoluto no HTML
        html = r.text or ""
        m = re.search(r'href="(https?://[^"]+)"', html)
        if m:
            return m.group(1)
    except Exception:
        pass

    # espera/teima um pouco (evita bloqueio de hotlink)
    if wait_secs > 0:
        time.sleep(min(wait_secs, 20))

    return gnews_url

# ---------- extração/limpeza ----------
KILL_PATTERNS = [
    re.compile(r"<(aside|nav|footer|form|script|style)[\s\S]*?</\1>", re.I),
    re.compile(r"<header[\s\S]*?</header>", re.I),
    re.compile(r"<figure[\s\S]*?</figure>", re.I),
]
KILL_PHRASES = re.compile(
    r"(leia também|veja também|publicidade|anúncio|anuncio|colunista|opinião|blogs|assine|vídeo relacionado|video relacionado)",
    re.I,
)

def readability_extract(html):
    doc = Document(html)
    title = (doc.short_title() or "").strip()
    content_html = doc.summary(html_partial=True) or ""
    return title, content_html

def best_first_image(html):
    m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', html, re.I)
    return m.group(1) if m else ""

def clean_noise(html):
    h = html or ""
    for rx in KILL_PATTERNS:
        h = rx.sub("", h)
    # remove âncoras mantendo texto
    h = re.sub(r"<a[^>]*>(.*?)</a>", r"\1", h, flags=re.I|re.S)
    # remove blocos de "leia também" etc.
    h = KILL_PHRASES.sub("", h)
    # colapsa quebras longas
    h = re.sub(r"(\s*\n\s*){3,}", "\n\n", h)
    return h

def clean_html(html):
    """
    Recebe html da matéria.
    Retorna dict: {html, text, paragraphs, chars, first_img}
    """
    html = clean_noise(html)
    soup = BeautifulSoup(html, "html.parser")
    # mantém apenas <p> e <h2>/<h3> básicos
    for tag in soup.find_all():
        if tag.name not in {"p", "h2", "h3", "strong", "em", "ul", "ol", "li"}:
            continue
    text = soup.get_text(separator=" ", strip=True)
    pcount = len(soup.find_all("p"))
    chars = len(text)
    first_img = best_first_image(html)
    return {"html": str(soup), "text": text, "paragraphs": pcount, "chars": chars, "first_img": first_img}

def fetch_and_extract(url, session, timeout=20):
    """
    Baixa página e extrai com readability.
    Retorna (html_extraido, titulo, imagem_topo)
    """
    r = http_get(url, session, timeout)
    title, article_html = readability_extract(r.text)
    if not article_html:
        article_html = r.text
    return article_html, title or "", best_first_image(r.text) or best_first_image(article_html)

def strip_html_keep_p(html):
    """Garante <p> bem formados; se não houver, cria a partir de quebras."""
    html = (html or "").strip()
    if "<p" not in html.lower():
        # quebra por linhas duplas
        parts = [x.strip() for x in re.split(r"\n\s*\n", BeautifulSoup(html, "html.parser").get_text()) if x.strip()]
        buf = "".join(f"<p>{requests.utils.requote_uri(p)}</p>" for p in parts)
        return buf or f"<p>{requests.utils.requote_uri(BeautifulSoup(html,'html.parser').get_text())}</p>"
    return html

def make_tags(text):
    stop = set("""
a o os as um uma uns umas de do da dos das em no na nos nas para por com sem sob sobre entre e ou que se sua seu suas seus ao à às aos até como mais menos muito muita muitos muitas já não sim foi será ser está estão era são pelo pela pelos pelas lhe eles elas dia ano anos hoje ontem amanhã
the and of to in on for with from
""".split())
    t = (text or "").lower()
    t = re.sub(r"[^a-z0-9á-úà-ùâ-ûã-õç\s-]", " ", t)
    words = [w for w in re.split(r"\s+", t) if len(w) >= 3 and not w.isnumeric() and w not in stop]
    freq = {}
    for w in words:
        freq[w] = freq.get(w, 0) + 1
    keys = sorted(freq, key=freq.get, reverse=True)[:12]
    # força cidades quando aparecerem
    cities = ["ilhabela","são sebastião","sao sebastiao","caraguatatuba","ubatuba","litoral","brasil"]
    for c in cities:
        if c in t and c not in keys:
            keys.append(c)
    return keys[:12]

def guess_source_name(url):
    try:
        host = urlparse(url).netloc.lower()
        host = host.replace("www.", "")
        return host
    except Exception:
        return ""

def try_amp(url, session, timeout=20):
    """Tenta baixar a versão AMP, se existir."""
    try:
        r = http_get(url, session, timeout)
        soup = BeautifulSoup(r.text, "html.parser")
        link = soup.find("link", rel=lambda v: v and "amphtml" in v)
        if link and link.get("href"):
            amp = http_get(link["href"], session, timeout)
            return amp.text
    except Exception:
        return None
    return None
