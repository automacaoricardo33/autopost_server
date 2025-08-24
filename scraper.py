# scraper.py
import time
import requests
import feedparser

USER_AGENT = "Mozilla/5.0 (compatible; AutopostBot/1.0; +https://example.com)"

def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})
    s.timeout = 20
    return s

def fetch_latest_gnews(query: str, lang: str = "pt-BR", country: str = "BR") -> dict:
    """
    Busca a notícia mais recente no GNews RSS para uma keyword.
    Retorna dict simples com {title, url, published}.
    """
    base = "https://news.google.com/rss/search"
    # Ex: https://news.google.com/rss/search?q=v%C3%B4lei&hl=pt-BR&gl=BR&ceid=BR:pt-419
    params = {
        "q": query,
        "hl": lang,
        "gl": country,
        "ceid": "BR:pt-419",
    }
    session = new_session()
    r = session.get(base, params=params)
    r.raise_for_status()
    feed = feedparser.parse(r.text)

    if not feed.entries:
        return {}

    e = feed.entries[0]
    return {
        "title": e.get("title"),
        "url": e.get("link"),
        "published": e.get("published", ""),
        "query": query,
        "source": "gnews",
    }
