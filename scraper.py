import re
from urllib.parse import urlparse, parse_qs, unquote

GNEWS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}

def resolve_gnews_url(gnews_url, session, wait_secs=5, timeout=20):
    """
    Tenta pegar a URL real de um item do Google News.
    1) Extrai ?url= se existir
    2) Faz GET permitindo redirects; aceita 30x da página de tracking
    3) Último fallback: acessa o gnews_url e procura tag <a href> com destino
    """
    try:
        # 1) alguns virão com ...&url=https://site.com/...
        q = urlparse(gnews_url).query
        if q:
            qs = parse_qs(q)
            if "url" in qs and qs["url"]:
                candidate = unquote(qs["url"][0])
                if candidate.startswith("http"):
                    return candidate
        # 2) segue redirects
        r = session.get(gnews_url, headers=GNEWS_HEADERS, allow_redirects=True, timeout=timeout)
        final = r.url
        if "news.google.com" not in urlparse(final).netloc:
            return final

        # 3) último fallback: tenta achar link absoluto no HTML
        html = r.text or ""
        m = re.search(r'href="(https?://[^"]+)"', html)
        if m:
            return m.group(1)
    except Exception:
        pass
    return gnews_url  # devolve o original se não achou nada
