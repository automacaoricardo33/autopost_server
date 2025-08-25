import requests
import feedparser
from bs4 import BeautifulSoup
from urllib.parse import quote_plus

try:
    from readability import Document
    HAS_READABILITY = True
except ModuleNotFoundError:
    HAS_READABILITY = False


def gnews_search_url(query: str, lang="pt-BR", gl="BR", ceid="BR:pt-419"):
    q = quote_plus(query)
    return f"https://news.google.com/rss/search?q={q}&hl={lang}&gl={gl}&ceid={ceid}"


def fetch_rss(keyword: str, limit: int = 5):
    url = gnews_search_url(keyword)
    feed = feedparser.parse(url)
    return feed.entries[:limit]


def extract_main_html(html: str) -> str:
    if HAS_READABILITY:
        try:
            doc = Document(html)
            return doc.summary(html_partial=True)
        except Exception:
            pass

    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    article = soup.find("article")
    if article:
        return str(article)

    divs = soup.find_all("div")
    best = max(divs, key=lambda d: len(d.get_text(" ", strip=True)), default=soup.body or soup)
    return str(best)


def fetch_article(url: str) -> str:
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            return ""
        return extract_main_html(resp.text)
    except Exception as e:
        print(f"[ERRO] Falha ao buscar artigo: {e}")
        return ""
