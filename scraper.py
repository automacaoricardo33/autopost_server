import os, re, sys, time, requests, feedparser
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urljoin, parse_qs

TIMEOUT = int(os.getenv('TIMEOUT_SECONDS', '60'))

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/119 Safari/537.36'
HDRS = {
    'User-Agent': UA,
    'Accept-Language': 'pt-BR,pt;q=0.9',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
}

def is_gnews(url: str) -> bool:
    try:
        return 'news.google.' in urlparse(url).netloc.lower()
    except Exception:
        return False

# ---------------- Feeds ----------------
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

# ---------------- Helpers de extração de URL real ----------------
def _publisher_from_entry(e) -> str:
    """
    Tenta extrair o link DIRETO do veículo a partir do RSS do Google News.
    1) <source url="...">
    2) links[] com ?url=...
    3) link com ?url=...
    """
    # 1) <source url="...">
    try:
        # feedparser expõe e.source com .href
        src = getattr(e, 'source', None)
        if src:
            href = getattr(src, 'href', None)
            if not href and isinstance(src, dict):
                href = src.get('href')
            if href:
                return href
    except Exception:
        pass

    # 2) links[] com ?url=
    try:
        links = getattr(e, 'links', []) or []
        for L in links:
            href = getattr(L, 'href', None) or (isinstance(L, dict) and L.get('href'))
            if not href:
                continue
            q = parse_qs(urlparse(href).query)
            if 'url' in q and q['url']:
                return q['url'][0]
    except Exception:
        pass

    # 3) link com ?url=
    try:
        href = e.get('link') or ''
        q = parse_qs(urlparse(href).query)
        if 'url' in q and q['url']:
            return q['url'][0]
    except Exception:
        pass

    # Se nada deu certo, retorna vazio (vamos usar o próprio link depois)
    return ''

def normalize_candidate_url(u: str, entry=None) -> str:
    """
    Se for Google News, tenta pegar o link do veículo direto do RSS (sem abrir a página do Google).
    Caso não consiga, devolve o próprio u (último recurso).
    """
    if entry is not None:
        pub = _publisher_from_entry(entry)
        if pub:
            return pub
    # ainda tenta ?url= diretamente no u
    if is_gnews(u):
        q = parse_qs(urlparse(u).query)
        if 'url' in q and q['url']:
            return q['url'][0]
    return u

# ---------------- Limpeza / reconstrução ----------------
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
        r'<(div|section)[^>]+class="[^"]*(ads|advert|adunit|banner|sponsor|share|sharing|sidebar|related|relacionadas|outbrain|taboola|cookie|gdpr|newsletter|comments?)[^"]*"[^>]*>.*?</\1>',
    ]
    for p in patterns:
        html = re.sub(p, '', html, flags=re.I|re.S)
    html = re.sub(r'<a[^>]*>(.*?)</a>', r'\1', html, flags=re.I|re.S)
    return html

def pick_content_html(page_html: str) -> str:
    """
    Extrai o corpo a partir de article|main|divs comuns e reconstrói whitelist p/h2/li.
    """
    if not page_html:
        return ''
    page_html = clean_noise_blocks(page_html)
    soup = BeautifulSoup(page_html, 'lxml')

    candidates = []
    node = soup.find('article')
    if node: candidates.append(node)
    node = soup.find('main')
    if node: candidates.append(node)

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
        parts = []
        for el in c.find_all(['p','h2','li']):
            txt = el.get_text(' ', strip=True)
            if not txt:
                continue
            if is_bad_line(txt):
                continue
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
        body = re.sub(r'(?:\s*<li>.*?</li>\s*){2,}', r'<ul>\g<0></ul>', body, flags=re.S|re.I)
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
            for e in fp.entries[:6]:
                gnews_link = e.get('link') or ''
                title = e.get('title') or ''

                # Prioriza link do veículo:
                real = normalize_candidate_url(gnews_link, entry=e)
                if not real:
                    real = gnews_link

                items.append({'url': real, 'title': title})
                if len(items) >= limit:
                    return items
    except Exception as ex:
        print(f"[candidates] erro: {ex}", file=sys.stderr)
    return items

def fetch_and_extract(url: str, timeout=TIMEOUT):
    """
    Baixa a página do VEÍCULO e extrai corpo limpo (sem abrir Google News).
    """
    real_url = url  # já normalizado em get_fresh_article_candidates

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
        'text': plain_text,
        'html': body_html
    }
