import os, time, json, threading, re
from datetime import datetime, timezone
from urllib.parse import urlparse, urljoin
import requests
from bs4 import BeautifulSoup
from readability import Document
from flask import Flask, jsonify, request

# ================== CONFIG ==================
PORT = int(os.environ.get("PORT", "10000"))

# IA opcional para reescrita
TEXTSYNTH_KEY = os.environ.get("TEXTSYNTH_KEY", "")

# Agendamento
SCRAPE_INTERVAL = int(os.environ.get("SCRAPE_INTERVAL", "300"))  # 5min
WAIT_GNEWS = int(os.environ.get("WAIT_GNEWS", "20"))             # espera para news.google.com
TIMEOUT = int(os.environ.get("TIMEOUT", "30"))

# Categoria por cidade (ajuste se quiser)
CITY_CATEGORY = {
    "caraguatatuba": 116,
    "ilhabela": 117,
    "são sebastião": 118,
    "sao sebastiao": 118,
    "ubatuba": 119,
}

# Headers para evitar 403
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
BASE_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.google.com/",
}

# Heurísticas para achar links de matérias em homepages
GOOD_PATH_HINTS = [
    "/noticia", "/notícias", "/news", "/politica", "/esportes", "/mundo",
    "/cidade", "/brasil", "/economia", "/blog/", "/2025", "/2024", "/2023"
]
BAD_PATH_HINTS = [
    "/video", "/videos", "/login", "/cadastro", "/assinante", "/tag/", "/tags/",
    "/autor/", "/colun", "/podcast", "javascript:", "#", "/sobre", "/contato"
]
MAX_HOME_LINKS = 30   # quantos links candidatas testar por homepage

# ================== APP/ESTADO ==================
app = Flask(__name__)

SOURCES = []               # lista de URLs (RSS, homepage ou notícia)
LAST_ARTICLE = {}          # cache em memória do ultimo.json
DATA_DIR = "/tmp/autopost-data"
os.makedirs(DATA_DIR, exist_ok=True)
LAST_PATH = os.path.join(DATA_DIR, "ultimo.json")


# ================== UTILS ==================
def log(*a): print(*a, flush=True)

def http_get(url, timeout=TIMEOUT, allow_redirects=True, accept=None):
    headers = dict(BASE_HEADERS)
    if accept:
        headers["Accept"] = accept
    r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=allow_redirects)
    r.raise_for_status()
    return r

def normalize_url(base, href):
    if not href: return ""
    href = href.strip()
    if href.startswith("//"):
        parsed = urlparse(base)
        href = f"{parsed.scheme}:{href}"
    if href.startswith("http://") or href.startswith("https://"):
        return href
    return urljoin(base, href)

def same_domain(a, b):
    pa, pb = urlparse(a), urlparse(b)
    return (pa.netloc.lower() == pb.netloc.lower()) and pa.netloc != ""

def is_rss_url(url: str) -> bool:
    u = url.lower()
    return u.endswith(".xml") or "/rss" in u or "/feed" in u or "format=xml" in u or "rss.xml" in u

def is_homepage(url: str) -> bool:
    p = urlparse(url)
    path = (p.path or "").strip("/")
    # homepage se caminho vazio ou curtíssimo
    return path == "" or path.count("/") <= 0

def looks_like_article(url: str) -> bool:
    u = url.lower()
    if any(bad in u for bad in BAD_PATH_HINTS):
        return False
    return any(h in u for h in GOOD_PATH_HINTS) or u.endswith(".html")

def resolve_google_news(url: str) -> str:
    if "news.google.com" not in url:
        return url
    log("[GNEWS] aguardando", WAIT_GNEWS, "s para resolver:", url)
    time.sleep(WAIT_GNEWS)
    try:
        r = http_get(url, allow_redirects=True)
        return r.url
    except Exception as e:
        log("[GNEWS] fallback sem resolver:", e)
        return url

def clean_html(html: str) -> str:
    if not html:
        return ""
    # remove blocos ruidosos
    for tag in ["script", "style", "nav", "aside", "footer", "form", "noscript"]:
        html = re.sub(fr"<{tag}\b[^>]*>.*?</{tag}>", "", html, flags=re.I | re.S)
    # remove "leia também" etc.
    kill = r"(leia também|veja também|publicidade|anúncio|anuncio|assista também|vídeo relacionado|video relacionado)"
    html = re.sub(rf"<h\d[^>]*>\s*{kill}\s*</h\d>", "", html, flags=re.I)
    html = re.sub(rf"<p[^>]*>\s*{kill}.*?</p>", "", html, flags=re.I | re.S)
    # linhas demais
    html = re.sub(r"(\s*\n\s*){3,}", "\n\n", html)
    return html

def extract_plain(html: str) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    return soup.get_text(" ", strip=True)

def guess_category(text: str) -> int:
    t = (text or "").lower()
    for k, v in CITY_CATEGORY.items():
        if k in t:
            return v
    return 1

def generate_tags(title: str, plain: str):
    txt = f"{title} {plain}".lower()
    words = re.findall(r"[a-zá-úà-ùâ-ûã-õç0-9]{3,}", txt, flags=re.I)
    stop = set("""a o os as de do da dos das em no na nos nas para por com sem sobre entre e ou que sua seu suas seus
                  já não sim foi são será ser está estão era pelo pela pelos pelas lhe eles elas dia ano hoje ontem amanhã
                  the and of to in on for with from""".split())
    freq = {}
    for w in words:
        if w in stop: continue
        if w.isdigit(): continue
        freq[w] = freq.get(w, 0) + 1
    return [w for w, _ in sorted(freq.items(), key=lambda x: -x[1])][:10]

def build_json(title: str, html: str, img: str, source: str):
    plain = extract_plain(html)
    cat = guess_category(f"{plain} {title}")
    tags = generate_tags(title, plain)
    meta = (plain[:157] + "...") if len(plain) > 160 else plain
    return {
        "title": title.strip(),
        "content_html": html.strip(),
        "meta_description": meta,
        "tags": tags,
        "category": cat,
        "image": (img or "").strip(),
        "source": (source or "").strip(),
        "generated_at": datetime.now(timezone.utc).isoformat()
    }

# ================== EXTRAÇÃO ==================
def extract_from_article_url(url: str):
    """
    Extrai título, corpo e imagem de uma URL de notícia (ou GNews resolvido).
    Retorna (title, content_html, image_url, final_url)
    """
    try:
        final = resolve_google_news(url)
        r = http_get(final, timeout=TIMEOUT)
        html = r.text

        # readability
        doc = Document(html)
        title = (doc.short_title() or "").strip()
        content_html = clean_html(doc.summary(html_partial=True) or "")

        # fallback se curto
        if len(extract_plain(content_html)) < 300:
            soup = BeautifulSoup(html, "lxml")
            art = soup.find("article")
            if art:
                content_html = clean_html(str(art))
                if not title:
                    h = art.find(["h1", "h2"])
                    if h: title = h.get_text(strip=True)
            if len(extract_plain(content_html)) < 300:
                best, score = None, 0
                for div in soup.find_all(["div", "main", "section"]):
                    pcount = len(div.find_all("p"))
                    tlen = len(div.get_text(" ", strip=True))
                    sc = pcount * 10 + tlen
                    if sc > score:
                        best, score = div, sc
                if best:
                    content_html = clean_html(str(best))

        # imagem (og/twitter)
        img = ""
        try:
            soup2 = BeautifulSoup(html, "lxml")
            og = soup2.find("meta", attrs={"property": "og:image"})
            tw = soup2.find("meta", attrs={"name": "twitter:image"})
            if og and og.get("content"): img = og["content"]
            elif tw and tw.get("content"): img = tw["content"]
        except:
            pass

        return title, content_html, img, final
    except Exception as e:
        log("[extract_from_article_url] erro:", e, "| url=", url)
        return "", "", "", url

def collect_article_links_from_home(home_url: str):
    """
    Lê a homepage e tenta descobrir links de matérias do mesmo domínio.
    Retorna lista de URLs (no máximo MAX_HOME_LINKS)
    """
    try:
        r = http_get(home_url, timeout=TIMEOUT)
        soup = BeautifulSoup(r.text, "lxml")
        links = []
        seen = set()
        for a in soup.find_all("a", href=True):
            href = normalize_url(home_url, a["href"])
            if not href: continue
            if href in seen: continue
            seen.add(href)
            if not same_domain(home_url, href): continue
            if any(bad in href.lower() for bad in BAD_PATH_HINTS): continue
            if looks_like_article(href):
                links.append(href)
            if len(links) >= MAX_HOME_LINKS:
                break
        return links
    except Exception as e:
        log("[collect_article_links_from_home] erro:", e, "| home=", home_url)
        return []

def extract_from_rss_url(rss_url: str):
    """
    Lê feed RSS e tenta a primeira matéria válida.
    Retorna (title, content_html, image_url, final_url)
    """
    try:
        r = http_get(rss_url, timeout=TIMEOUT, accept="application/rss+xml,application/xml,text/xml,text/html")
        soup = BeautifulSoup(r.content, "xml")
        items = soup.find_all(["item", "entry"])
        for it in items:
            link = ""
            link_tag = it.find("link")
            if link_tag:
                link = link_tag.get("href") or (link_tag.text or "").strip()
            if not link:
                guid = it.find("guid")
                if guid and guid.text: link = guid.text.strip()
            if not link: continue

            title, html, img, final = extract_from_article_url(link)
            if len(extract_plain(html)) >= 400:
                return title, html, img, final
        return "", "", "", rss_url
    except Exception as e:
        log("[extract_from_rss_url] erro:", e, "| rss=", rss_url)
        return "", "", "", rss_url


# ================== IA (TextSynth opcional) ==================
def textsynth_rewrite(title: str, plain: str):
    if not TEXTSYNTH_KEY:
        return title, f"<p>{plain}</p>", ""
    prompt = f"""
Você é um jornalista do Litoral Norte de SP. Reescreva jornalisticamente o texto abaixo em HTML limpo (apenas <p>, <h2>, <ul><li>, <strong>, <em>). 4-7 parágrafos. Sem publicidade nem 'leia também'. Gere meta descrição (160 caracteres) ao final.

TÍTULO ORIGINAL: {title}

TEXTO ORIGINAL:
{plain}
"""
    try:
        r = requests.post(
            "https://api.textsynth.com/v1/engines/gptj_6B/completions",
            headers={"Authorization": f"Bearer {TEXTSYNTH_KEY}", "Content-Type": "application/json"},
            json={"prompt": prompt, "max_tokens": 900, "temperature": 0.6, "stop": ["</html>", "</body>"]},
            timeout=60
        )
        r.raise_for_status()
        data = r.json()
        out = (data.get("text") or "").strip()
        out = re.sub(r"</?(html|body|head)[^>]*>", "", out, flags=re.I)
        meta = ""
        m = re.search(r"meta descrição[:\-]\s*(.+)$", out, flags=re.I | re.M)
        if m: meta = m.group(1).strip()[:160]
        return title or "", out, meta
    except Exception as e:
        log("[TextSynth] erro:", e)
        return title, f"<p>{plain}</p>", ""


# ================== PIPELINE ==================
def extract_one(url: str):
    """
    Decide como tratar a URL: RSS, homepage ou artigo.
    Retorna dicionário JSON pronto (ou None)
    """
    url = url.strip()
    if not url: return None

    if is_rss_url(url):
        title, content_html, img, final = extract_from_rss_url(url)
    elif is_homepage(url):
        # caça links de matéria na homepage
        for cand in collect_article_links_from_home(url):
            t, h, img, fin = extract_from_article_url(cand)
            if len(extract_plain(h)) >= 400:
                title, content_html, final = t, h, fin
                break
        else:
            return None
    else:
        title, content_html, img, final = extract_from_article_url(url)

    plain = extract_plain(content_html)
    if len(plain) < 400:
        return None

    new_title, rewritten_html, meta = textsynth_rewrite(title, plain)
    if len(extract_plain(rewritten_html)) < 400:
        # usa o limpo se reescrita ficou curta
        rewritten_html = f"<p>{plain}</p>"

    data = build_json(new_title or title, rewritten_html, img, final)
    return data

def scrape_once():
    """
    Percorre SOURCES; para cada uma, tenta extrair 1 matéria e salvar ultimo.json
    """
    global LAST_ARTICLE
    if not SOURCES:
        log("[JOB] Sem fontes configuradas.")
        return

    for src in list(SOURCES):
        data = extract_one(src)
        if data:
            LAST_ARTICLE = data
            with open(LAST_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            log("[JOB] Artigo atualizado em ultimo.json:", data["title"][:90])
            return

    log("[JOB] Nenhuma fonte retornou conteúdo suficiente.")

def scheduler_loop():
    while True:
        try:
            scrape_once()
        except Exception as e:
            log("[JOB] erro inesperado:", e)
        time.sleep(SCRAPE_INTERVAL)


# ================== ROTAS ==================
@app.route("/")
def idx():
    return "AutoPost Render Server OK", 200

@app.route("/health")
def health():
    return jsonify({"ok": True, "time": datetime.utcnow().isoformat(), "sources": len(SOURCES)})

@app.route("/sources", methods=["GET"])
def get_sources():
    return jsonify({"sources": SOURCES})

@app.route("/sources/update", methods=["POST"])
def set_sources():
    """
    JSON:
    {
      "sources": ["https://site.com/", "https://site.com/feed", "https://site.com/noticia/123.html"],
      "replace": true
    }
    """
    try:
        payload = request.get_json(force=True, silent=True) or {}
        sources = payload.get("sources") or []
        replace = bool(payload.get("replace", True))
        urls = []
        for s in sources:
            s = str(s).strip()
            if s: urls.append(s)
        global SOURCES
        if replace:
            SOURCES = urls
        else:
            seen = set(SOURCES)
            for u in urls:
                if u not in seen:
                    SOURCES.append(u)
                    seen.add(u)
        return jsonify({"ok": True, "count": len(SOURCES), "sources": SOURCES})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

@app.route("/job/run")
def job_run():
    scrape_once()
    return jsonify({"ok": True})

@app.route("/extract", methods=["POST"])
def extract_endpoint():
    """
    JSON: {"url": "https://site.com/qualquer-coisa"}
    - pode ser homepage, RSS ou link de notícia
    - retorna o JSON da matéria (não salva)
    """
    try:
        payload = request.get_json(force=True, silent=True) or {}
        url = (payload.get("url") or "").strip()
        if not url:
            return jsonify({"ok": False, "error": "url vazia"}), 400
        data = extract_one(url)
        if not data:
            return jsonify({"ok": False, "error": "nenhum conteúdo suficiente"}), 404
        return jsonify(data)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/extract_and_save", methods=["POST"])
def extract_and_save():
    """
    JSON: {"url": "https://site.com/qualquer-coisa"}
    - faz a extração e SALVA em /artigos/ultimo.json
    """
    try:
        payload = request.get_json(force=True, silent=True) or {}
        url = (payload.get("url") or "").strip()
        if not url:
            return jsonify({"ok": False, "error": "url vazia"}), 400
        data = extract_one(url)
        if not data:
            return jsonify({"ok": False, "error": "nenhum conteúdo suficiente"}), 404
        global LAST_ARTICLE
        LAST_ARTICLE = data
        with open(LAST_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        return jsonify({"ok": True, "saved": True, "title": data.get("title","")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/artigos/ultimo.json")
def ultimo_json():
    if os.path.exists(LAST_PATH):
        try:
            with open(LAST_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return jsonify(data)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify(LAST_ARTICLE or {"ok": False, "error": "vazio"}), 200


# ================== MAIN ==================
if __name__ == "__main__":
    th = threading.Thread(target=scheduler_loop, daemon=True)
    th.start()
    app.run(host="0.0.0.0", port=PORT)
