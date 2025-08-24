import os, re, requests, feedparser
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urljoin, parse_qs

TIMEOUT = int(os.getenv('TIMEOUT_SECONDS', '45'))

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/119 Safari/537.36'
HDRS = {
    'User-Agent': UA,
    'Accept-Language': 'pt-BR,pt;q=0.9',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
}

# ---------------- Google News helpers ----------------
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
    tag = soup.find('link', rel=lambda v: v and 'amphtml' in v)
    if tag and tag.get('href'):
        return urljoin(base, tag['href'].strip())
    return ''

def resolve_google_news(url: str, timeout=TIMEOUT) -> str:
    # 1) ?url= no query
    q = parse_qs(urlparse(url).query)
    if 'url' in q and q['url']:
        return q['url'][0]

    # 2) baixa página do Google News e tenta meta refresh, amphtml, 1º link externo
    try:
        r = requests.get(url, headers=HDRS, timeout=timeout, allow_redirects=True)
        r.raise_for_status()
        html = r.text
        u = _resolve_meta_refresh(html, r.url)
        if u:
            return u
        u = _amp_link(html, r.url)
        if u:
            return u
        u = _first_external_link(html)
        if u:
            return u
    except Exception:
        pass
    return url

def normalize_candidate_url(u: str) -> str:
    if 'news.google.' in u:
        return resolve_google_news(u)
    return u

# ---------------- Conteúdo helpers ----------------
BAD_PREFIX = [
    'leia também','leia tambem','veja também','veja tambem','publicidade','anúncio','anuncio',
    'compartilhe','assine','siga-nos','saiba mais','link patrocinado','oferta',
    'vídeo relacionado','video relacionado'
]

def is_bad_line(text):
    low = text.lower().strip()
    for bp in BAD_PREFIX:
        if low.startswith(bp):
            return True
    return False

def clean_noise_blocks(html: str) -> str:
    if not html:
        return ''
    patterns = [
        r'<script\b[^>]*>.*?</script>',
        r'<style\b[^>]*>.*?</style>',
        r'<noscript\b[^>]*>.*?</noscript>',
        r'<iframe\b[^>]*>.*?</iframe>',
        r'<form\b[^>]*>.*?</form>',
        r'<figure\b[^>]*>.*?</figure>',
        r'<!--.*?-->',
        r'<header\b[^>]*>.*?</header>',
        r'<footer\b[^>]*>.*?</footer>',
        r'<nav\b[^>]*>.*?</nav>',
        # div/section com classes comuns de lixo
        r'<(div|section)[^>]+class="[^"]*(ads|advert|adunit|banner|sponsor|share|sharing|sidebar|related|relacionadas|outbrain|taboola|cookie|gdpr|newsletter|comments?)[^"]*"[^>]*>.*?</\1>',
    ]
    for p in patterns:
        html = re.sub(p, '', html, flags=re.I|re.S)
    # remove âncoras mantendo texto
    html = re.sub(r'<a[^>]*>(.*?)</a>', r'\1', html, flags=re.I|re.S)
    return html

def pick_content_html(page_html: str) -> str:
    """
    Pega o HTML do corpo a partir de article|main|div com classes de conteúdo,
    e reconstrói whitelist: p/h2/li (embrulha lis em ul).
    """
    if not page_html:
        return ''
    page_html = clean_noise_blocks(page_html)
    soup = BeautifulSoup(page_html, 'lxml')

    # preferências de contêiner
    candidates = []
    node = soup.find('article')
    if node: candidates.append(node)
    node = soup.find('main')
    if node: candidates.append(node)

    # classes comuns de conteúdo
    for sel in ['div.entry-content','div.post-content','div.single-content',
                'div.article-content','div.content__article','div.materia-conteudo',
                'div[itemprop="articleBody"]']:
        node = soup.select_one(sel)
        if node: candidates.append(node)

    if not candidates:
        candidates = [soup.body or soup]

    best = ''
    best_score = 0
    for c in candidates:
        # pega somente p/h2/li
        parts = []
        for el in c.find_all(['p','h2','li']):
            txt = el.get_text(' ', strip=True)
            if not txt:
                continue
            if is_bad_line(txt):
                continue
            # ignora parágrafos muito curtos
            if el.name == 'p' and len(txt) < 20:
                continue
            if el.name == 'h2':
                parts.append(f'<h2>{escape_html(txt)}</h2>')
            elif el.name == 'li':
                parts.append(f'<li>{escape_html(txt)}</li>')
            else:
                parts.append(f'<p>{escape_html(txt)}</p>')

        if not parts:
            continue
        body = '\n'.join(parts)
        # embrulha listas
        body = re.sub(r'(?:\s*<li>.*?</li>\s*){2,}', r'<ul>\g<0></ul>', body, flags=re.S|re.I)
        # pontua: nº de <p> e tamanho do texto
        pcount = len(re.findall(r'<p\b', body, flags=re.I))
        tlen = len(BeautifulSoup(body, 'lxml').get_text(' ', strip=True))
        score = pcount * 10 + tlen
        if score > best_score:
            best = body
            best_score = score

    return best

def escape_html(s: str) -> str:
    return s.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')

# ---------------- Pipeline ----------------
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
    Resolve Google News -> baixa página do veículo -> extrai corpo limpo.
    Sempre retorna 'html'; 'text' pode ficar vazio em casos raros, mas html vem.
    """
    real_url = normalize_candidate_url(url)

    r = requests.get(real_url, headers=HDRS, timeout=timeout, allow_redirects=True)
    r.raise_for_status()
    html = r.text

    soup = BeautifulSoup(html, 'lxml')

    # título
    title = soup.title.string.strip() if soup.title and soup.title.string else None
    if not title:
        mt = soup.find('meta', attrs={'property':'og:title'}) or soup.find('meta', attrs={'name':'title'})
        if mt and mt.get('content'):
            title = mt['content'].strip()

    # imagem
    image = None
    og = soup.find('meta', property='og:image')
    if og and og.get('content'):
        image = og['content'].strip()
    else:
        tw = soup.find('meta', attrs={'name':'twitter:image'})
        if tw and tw.get('content'):
            image = tw['content'].strip()

    # corpo (whitelist)
    body_html = pick_content_html(html)
    plain_text = BeautifulSoup(body_html, 'lxml').get_text(' ', strip=True)

    return {
        'title': title,
        'image': image,
        'text': plain_text,   # pode ser vazio, mas body_html vem
        'html': body_html
    }
