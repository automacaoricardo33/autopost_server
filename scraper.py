import os, re, requests, feedparser
from bs4 import BeautifulSoup

TIMEOUT = int(os.getenv('TIMEOUT_SECONDS', '45'))

def google_news_rss_for(query: str, lang='pt-BR', country='BR'):
    import urllib.parse as up
    q = up.quote(query)
    return f'https://news.google.com/rss/search?q={q}&hl={lang}&gl={country}&ceid={country}:pt-419'

def get_feeds():
    raw_feeds = os.getenv('FEEDS', '').strip()
    if raw_feeds:
        return [f.strip() for f in raw_feeds.split(';') if f.strip()]
    kws = os.getenv('KEYWORDS', 'litoral norte de sao paulo; ilhabela; sao sebastiao; caraguatatuba; ubatuba')
    return [google_news_rss_for(k.strip()) for k in kws.split(';') if k.strip()]

def get_fresh_article_candidates(limit=10):
    feeds = get_feeds()
    items = []
    try:
        for feed in feeds:
            fp = feedparser.parse(feed)
            for e in fp.entries[:5]:
                url = e.get('link') or ''
                title = e.get('title') or ''
                if 'news.google.com' in url and 'url=' in url:
                    import urllib.parse as up
                    qs = up.urlparse(url).query
                    qsd = up.parse_qs(qs)
                    real = qsd.get('url', [url])[0]
                    url = real
                items.append({'url': url, 'title': title})
                if len(items) >= limit:
                    return items
    except Exception:
        pass
    return items

def fetch_and_extract(url: str, timeout=TIMEOUT):
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/119 Safari/537.36'}
    r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
    r.raise_for_status()
    html = r.text
    soup = BeautifulSoup(html, 'lxml')
    title = soup.title.string.strip() if soup.title and soup.title.string else None
    image = None
    og = soup.find('meta', property='og:image')
    if og and og.get('content'):
        image = og['content'].strip()
    paragraphs = []
    for p in soup.find_all('p'):
        txt = p.get_text(' ', strip=True)
        if txt and len(txt) > 40 and not txt.lower().startswith(('leia tambem','veja tambem','leia tamb')):
            paragraphs.append(txt)
    text = '\n\n'.join(paragraphs)
    def escape_html(s):
        return s.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')
    body_html = ''.join([f'<p>{escape_html(t)}</p>' for t in paragraphs])
    return {'title': title, 'image': image, 'text': text, 'html': body_html}
