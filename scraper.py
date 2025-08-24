import os, re, requests, feedparser
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urljoin, urlparse, parse_qs

TIMEOUT = int(os.getenv('TIMEOUT_SECONDS', '45'))

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/119 Safari/537.36'
HDRS = {'User-Agent': UA, 'Accept-Language': 'pt-BR,pt;q=0.9'}

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

def _resolve_meta_refresh(html, base):
    m = re.search(r'<meta[^>]+http-equiv=["\']refresh["\'][^>]+content=["\']\s*\d+\s*;\s*url=([^"\']+)["\']', html, re.I)
    if m:
        url = m.group(1).strip()
        return urljoin(base, url)
    return ''

def _first_external_link(html):
    soup = BeautifulSoup(html, 'lxml')
    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        if href.startswith('/'):
            continue
        pu = urlparse(href)
        if not pu.scheme.startswith('http'):
            continue
        if 'news.google.' in pu.netloc:
            continue
        return href
    return ''

def _amp_link(html, base):
    soup = BeautifulSoup(html, 'lxml')
    # rel=amphtml costuma apontar para a matéria AMP do veículo
    tag = soup.find('link', rel=lambda v: v and 'amphtml' in v)
    if tag and tag.get('href'):
        return urljoin(base, tag['href'].strip())
    return ''

def resolve_google_news(url: str, timeout=TIMEOUT) -> str:
    """
    Converte links do tipo https://news.google.com/rss/articles/... para a URL do veículo.
    Estratégia:
      1) Se tiver ?url= no query -> usa.
      2) GET no HTML do Google News e tenta: meta refresh -> amphtml -> 1º link externo.
    """
    # 1) url= no query
    q = parse_qs(urlparse(url).query)
    if 'url' in q and q['url']:
        return q['url'][0]

    # 2) Baixa a página do Google News
    try:
        r = requests.get(url, headers=HDRS, timeout=timeout, allow_redirects=True)
        r.raise_for_status()
        html = r.text
        # meta refresh
        u = _resolve_meta_refresh(html, r.url)
        if u:
            return u
        # link amp
        u = _amp_link(html, r.url)
        if u:
            return u
        # primeiro link externo
        u = _first_external_link(html)
        if u:
            return u
    except Exception:
        pass
    return url  # fallback

def normalize_candidate_url(u: str) -> str:
    if 'news.google.' in u:
        if '/articles/' in u or '/rss/articles/' in u or '/articles/CB' in u:
            return resolve_google_news(u)
        # também cobre o caso antigo com ?url=
        return resolve_google_news(u)
    return u

def get_fresh_article_candidates(limit=10):
    feeds = get_feeds()
    items = []
    try:
        for feed in feeds:
            fp = feedparser.parse(feed)
            for e in fp.entries[:5]:
                url = e.get('link') or ''
                title = e.get('title') or ''
                url = normalize_candidate_url(url)
                items.append({'url': url, 'title': title})
                if len(items) >= limit:
                    return items
    except Exception:
        pass
    return items

def fetch_and_extract(url: str, timeout=TIMEOUT):
    """
    Garante que, se vier um link do Google News, resolvemos para o veículo antes de extrair.
    """
    real_url = normalize_candidate_url(url)
    headers = HDRS
    r = requests.get(real_url, headers=headers, timeout=timeout, allow_redirects=True)
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
        if txt and len(txt) > 40 and not txt.lower().startswith(('leia tambem','veja tambem','leia também','veja também','publicidade','anúncio','anuncio')):
            paragraphs.append(txt)

    text = '\n\n'.join(paragraphs)

    def escape_html(s):
        return s.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')

    body_html = ''.join([f'<p>{escape_html(t)}</p>' for t in paragraphs])

    return {'title': title, 'image': image, 'text': text, 'html': body_html}
