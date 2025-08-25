# scraper.py
from urllib.parse import quote_plus

GNEWS_BASE = "https://news.google.com"

def fetch_rss(query: str, limit: int = 10, hl="pt-BR", gl="BR", ceid="BR:pt-419"):
    # Encode seguro do termo de busca (espaços, acentos, etc.)
    q = quote_plus(query.strip())
    url = f"{GNEWS_BASE}/rss/search?q={q}&hl={hl}&gl={gl}&ceid={ceid}"

    import feedparser
    feed = feedparser.parse(url)

    items = []
    for entry in feed.entries[:limit]:
        items.append({
            "title": entry.get("title", ""),
            "link": entry.get("link", ""),
            "summary": entry.get("summary", "") or entry.get("description", ""),
            "published": entry.get("published", ""),
        })
    return items, url
