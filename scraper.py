import feedparser
from bs4 import BeautifulSoup

def fetch_rss(keyword: str, limit: int = 5):
    """Busca notícias no Google News RSS já com título, resumo e link canônico."""
    url = f"https://news.google.com/rss/search?q={keyword}&hl=pt-BR&gl=BR&ceid=BR:pt-419"
    feed = feedparser.parse(url)
    items = []
    for e in feed.entries[:limit]:
        title = e.get("title", "").strip()
        # summary vem em HTML; limpamos e garantimos texto
        raw_summary = e.get("summary", "") or e.get("description", "")
        summary = BeautifulSoup(raw_summary, "html.parser").get_text(" ").strip()
        # tenta pegar o primeiro link alternativo; se não, usa e.link
        link = (e.links[0].href if getattr(e, "links", []) else e.get("link", "")).strip()
        source = ""
        if hasattr(e, "source") and getattr(e.source, "title", None):
            source = e.source.title
        items.append({
            "title": title,
            "summary": summary,
            "link": link,
            "source": source
        })
    return items
