# --- SUBSTITUIR no scraper.py ---

import sys

def _rel_canonical(html, base):
    try:
        soup = BeautifulSoup(html, 'lxml')
        tag = soup.find('link', rel=lambda v: v and 'canonical' in v)
        if tag and tag.get('href'):
            return urljoin(base, tag['href'].strip())
    except Exception:
        pass
    return ''

def _og_url(html, base):
    try:
        soup = BeautifulSoup(html, 'lxml')
        tag = soup.find('meta', property='og:url')
        if tag and tag.get('content'):
            return urljoin(base, tag['content'].strip())
    except Exception:
        pass
    return ''

def resolve_google_news(url: str, timeout=TIMEOUT) -> str:
    """
    Converte links https://news.google.com/rss/articles/... para a URL do veículo.
    Ordem de tentativa:
      1) parâmetro ?url=
      2) meta refresh
      3) rel=canonical
      4) og:url
      5) rel=amphtml
      6) primeiro link externo no HTML
    """
    # 1) ?url=
    q = parse_qs(urlparse(url).query)
    if 'url' in q and q['url']:
        resolved = q['url'][0]
        print(f"[resolve_gnews] via query url= -> {resolved}", file=sys.stderr)
        return resolved

    try:
        r = requests.get(url, headers=HDRS, timeout=timeout, allow_redirects=True)
        r.raise_for_status()
        html = r.text

        # 2) meta refresh
        u = _resolve_meta_refresh(html, r.url)
        if u:
            print(f"[resolve_gnews] via meta-refresh -> {u}", file=sys.stderr)
            return u

        # 3) canonical
        u = _rel_canonical(html, r.url)
        if u and 'news.google.' not in urlparse(u).netloc:
            print(f"[resolve_gnews] via rel=canonical -> {u}", file=sys.stderr)
            return u

        # 4) og:url
        u = _og_url(html, r.url)
        if u and 'news.google.' not in urlparse(u).netloc:
            print(f"[resolve_gnews] via og:url -> {u}", file=sys.stderr)
            return u

        # 5) amphtml
        u = _amp_link(html, r.url)
        if u:
            print(f"[resolve_gnews] via amphtml -> {u}", file=sys.stderr)
            return u

        # 6) 1º link externo
        u = _first_external_link(html)
        if u:
            print(f"[resolve_gnews] via first external link -> {u}", file=sys.stderr)
            return u

    except Exception as e:
        print(f"[resolve_gnews] erro: {e}", file=sys.stderr)

    print(f"[resolve_gnews] fallback -> {url}", file=sys.stderr)
    return url
