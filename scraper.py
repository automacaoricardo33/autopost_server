import re
import time
import html
import json
import random
from urllib.parse import urlparse, urljoin

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import feedparser
from bs4 import BeautifulSoup
from dateutil import parser as dtparse
import bleach

# -------------------------
# Sessão HTTP com retries
# -------------------------
def new_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        ),
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
    })
    retry = Retry(
        total=4,
        backoff_factor=0.6,
        status_forcelist=[429, 500, 502, 503, 504],
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    s.mount("http://", HTTPAdapter(max_retries=retry))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s

# -------------------------
# Google News (RSS)
# -------------------------
def search_gnews(query, session=None, hl="pt-BR", gl="BR", ceid="BR:pt-419", max_items=12):
    """
    Retorna uma lista de itens do Google News RSS para a 'query'.
    Cada item tem: title, link, published_dt (datetime), source (str)
    """
    session = session or new_session()
    q = requests.utils.quote(query)
    url = f"https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={ceid}"
    r = session.get(url, timeout=20)
    r.raise_for_status()
    feed = feedparser.parse(r.content)

    items = []
    for e in feed.entries[:max_items]:
        pub = None
        if hasattr(e, "published"):
            try:
                pub = dtparse.parse(e.published)
            except Exception:
                pub = None
        src = None
        # Em muitos casos, a fonte vem em e.source.title
        try:
            if hasattr(e, "source") and hasattr(e.source, "title"):
                src = e.source.title
        except Exception:
            src = None

        items.append({
            "title": e.title,
            "link": e.link,
            "published_dt": pub,
            "source": src,
        })
    return items

def resolve_gnews_url(link, session=None, wait_secs=3, timeout=20):
    """
    Os links do Google News são wrappers. Aqui seguimos os redirects
    e devolvemos a URL final do publisher.
    """
    session = session or new_session()
    # alguns hosts demoram a responder; pequena espera opcional
    if wait_secs and wait_secs > 0:
        time.sleep(wait_secs)
    resp = session.get(link, timeout=timeout, allow_redirects=True)
    resp.raise_for_status()
    return resp.url

# -------------------------
# Heurísticas de AMP
# -------------------------
def _amp_candidates(url):
    u = url.rstrip("/")
    paths = [
        u + "/amp",
        u + "?output=amp",
        u + "/amp/",
    ]
    # urls com /?outputType=amp também aparecem
    if "g1.globo.com" in u and not u.endswith(".amp"):
        paths.append(u + ".amp")
    return paths

def try_amp(url, session=None, timeout=20):
    """
    Tenta carregar uma versão AMP da página, que geralmente tem HTML mais limpo.
    Retorna HTML (str) ou None.
    """
    session = session or new_session()
    for cand in _amp_candidates(url):
        try:
            r = session.get(cand, timeout=timeout)
            if r.status_code == 200 and len(r.text) > 500:
                return r.text
        except Exception:
            continue
    return None

# -------------------------
# Extração de conteúdo
# -------------------------
def fetch_and_extract(url, session=None, timeout=20):
    """
    Baixa a página e extrai:
      - raw_html (str)
      - title (str)
      - top_image (str ou "")
    """
    session = session or new_session()
    r = session.get(url, timeout=timeout)
    r.raise_for_status()
    html_text = r.text

    soup = BeautifulSoup(html_text, "lxml")

    # título
    title = None
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    if not title:
        ogt = soup.find("meta", attrs={"property": "og:title"}) or soup.find("meta", attrs={"name": "og:title"})
        if ogt and ogt.get("content"):
            title = ogt["content"].strip()
    if not title:
        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(strip=True)
    if not title:
        title = url

    # imagem
    top_image = ""
    ogimg = soup.find("meta", attrs={"property": "og:image"}) or soup.find("meta", attrs={"name": "og:image"})
    if ogimg and ogimg.get("content"):
        top_image = ogimg["content"].strip()

    return html_text, title, top_image

def _is_visible_tag(tag):
    # ignora elementos que raramente têm conteúdo editorial
    skip = {"script", "style", "noscript", "header", "footer", "nav", "aside"}
    return tag.name not in skip

def clean_html(html_text):
    """
    Extrai texto dos parágrafos <p> (e alguns <li>), devolvendo:
      - html: HTML reconstruído só com <p>/<li>/<ul>/<ol>/<a>/<strong>/<em>
      - text: texto puro concatenado
      - paragraphs: número de <p> aceitos
      - chars: número de caracteres
      - first_img: primeira imagem encontrada (se houver)
    """
    soup = BeautifulSoup(html_text, "lxml")

    # primeira imagem visível
    first_img = ""
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-original")
        if src and src.startswith("http"):
            first_img = src
            break

    # coleta parágrafos significativos
    parts = []
    for p in soup.find_all(["p", "li"]):
        if not p.get_text(strip=True):
            continue
        # ignora parágrafos dentro de elementos invisíveis
        if p.parent and not _is_visible_tag(p.parent):
            continue
        txt = p.get_text(" ", strip=True)
        # remove “Leia também”, “Assista”, etc.
        if len(txt) < 30:
            continue
        parts.append(f"<p>{html.escape(txt)}</p>")

    if not parts:
        text = ""
        html_out = ""
        pcount = 0
    else:
        html_out = "\n".join(parts)
        # sanitização leve
        html_out = bleach.clean(
            html_out,
            tags=["p", "a", "strong", "em", "ul", "ol", "li", "br"],
            attributes={"a": ["href", "title", "rel", "target"]},
            strip=True,
        )
        text = BeautifulSoup(html_out, "lxml").get_text(" ", strip=True)
        pcount = html_out.count("<p>")

    return {
        "html": html_out,
        "text": text,
        "paragraphs": pcount,
        "chars": len(text),
        "first_img": first_img,
    }

def strip_html_keep_p(html_text):
    """Garante whitelist de tags finais (para enviar ao WP)."""
    return bleach.clean(
        html_text or "",
        tags=["p", "a", "strong", "em", "ul", "ol", "li", "br"],
        attributes={"a": ["href", "title", "rel", "target"]},
        strip=True,
    )

# -------------------------
# Metadados
# -------------------------
def guess_source_name(url):
    netloc = urlparse(url).netloc.lower()
    # mapeia domínios conhecidos p/ nomes mais bonitos
    mapping = {
        "g1.globo.com": "G1",
        "oglobo.globo.com": "O Globo",
        "ge.globo.com": "ge",
        "uol.com.br": "UOL",
        "folha.uol.com.br": "Folha de S.Paulo",
        "estadao.com.br": "Estadão",
        "bbc.com": "BBC",
        "cnnbrasil.com.br": "CNN Brasil",
        "jovempan.com.br": "Jovem Pan",
        "gazetadopovo.com.br": "Gazeta do Povo",
    }
    for dom, name in mapping.items():
        if dom in netloc:
            return name
    # “site.com.br” -> “Site”
    base = netloc.split(":")[0]
    if base.startswith("www."):
        base = base[4:]
    base = base.split(".")[0]
    return base.capitalize()

def make_tags(text):
    """
    Extração bem simples de tags: palavras relevantes + cidades do LN.
    Retorna lista (minúsculas, sem repetição).
    """
    cities = ["caraguatatuba", "são sebastião", "sao sebastiao", "ilhabela", "ubatuba", "litoral norte"]
    sports = ["futebol", "fórmula 1", "formula 1", "f1", "vôlei", "volei", "surf", "regata"]
    gen = ["brasil", "governo de são paulo", "tempo", "alerta", "polícia", "economia", "saúde"]

    base_words = cities + sports + gen

    found = []
    low = (text or "").lower()
    for w in base_words:
        if w in low and w not in found:
            found.append(w)

    # pega substantivos longos simples
    tokens = re.findall(r"[a-záéíóúâêôàãõç]{5,}", low)
    for t in tokens[:30]:
