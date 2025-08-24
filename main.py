import os, time, json, threading, re
from datetime import datetime, timezone
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup
from readability import Document
from flask import Flask, jsonify, request

# ================== CONFIGURAÇÕES ==================
PORT = int(os.environ.get("PORT", "10000"))

# Coloque sua chave TextSynth (opcional: se vazio, publica texto limpo)
TEXTSYNTH_KEY = os.environ.get("TEXTSYNTH_KEY", "")

# Intervalo do agendador em segundos (5 min padrão)
SCRAPE_INTERVAL = int(os.environ.get("SCRAPE_INTERVAL", "300"))

# Quanto esperar para links do Google News resolverem (em segundos)
WAIT_GNEWS = int(os.environ.get("WAIT_GNEWS", "20"))

# Timeout de requisições HTTP
TIMEOUT = int(os.environ.get("TIMEOUT", "30"))

# Mapeamento simples de categorias por cidade (ajuste como quiser)
CITY_CATEGORY = {
    "caraguatatuba": 116,
    "ilhabela": 117,
    "são sebastião": 118,
    "sao sebastiao": 118,
    "ubatuba": 119,
}

# User-Agent para evitar 403
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "\
     "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"

# ================== APP/ESTADO ==================
app = Flask(__name__)

SOURCES = []              # lista de URLs (RSS e/ou artigos)
LAST_ARTICLE = {}         # cache em memória do ultimo.json
DATA_DIR = "/tmp/autopost-data"
os.makedirs(DATA_DIR, exist_ok=True)
LAST_PATH = os.path.join(DATA_DIR, "ultimo.json")


# ================== UTILS ==================
def log(*a): print(*a, flush=True)

def http_get(url, timeout=TIMEOUT, allow_redirects=True, accept="text/html"):
    headers = {
        "User-Agent": UA,
        "Accept": accept,
    }
    r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=allow_redirects)
    r.raise_for_status()
    return r

def is_rss_url(url: str) -> bool:
    u = url.lower()
    return u.endswith(".xml") or "/rss" in u or "/feed" in u or "format=xml" in u

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
    # remove tags ruidosas
    for tag in ["script", "style", "nav", "aside", "footer", "form", "noscript"]:
        html = re.sub(fr"<{tag}\b[^>]*>.*?</{tag}>", "", html, flags=re.I | re.S)
    # remove chamados típicos
    kill = r"(leia também|veja também|publicidade|anúncio|anuncio|assista também|vídeo relacionado|video relacionado)"
    html = re.sub(rf"<h\d[^>]*>\s*{kill}\s*</h\d>", "", html, flags=re.I)
    html = re.sub(rf"<p[^>]*>\s*{kill}.*?</p>", "", html, flags=re.I | re.S)
    # quebra de linhas excessivas
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
    Recebe URL de notícia (ou GNews resolvido) e tenta:
    - readability
    - <article>
    - maior bloco por contagem de <p>
    Retorna (title, content_html, image_url, final_url)
    """
    try:
        final = resolve_google_news(url)
        r = http_get(final, timeout=TIMEOUT)
        html = r.text

        # título + conteúdo com readability
        doc = Document(html)
        title = (doc.short_title() or "").strip()
        content_html = clean_html(doc.summary(html_partial=True) or "")

        # fallback se conteúdo muito curto
        if len(extract_plain(content_html)) < 300:
            soup = BeautifulSoup(html, "lxml")
            # tenta <article>
            art = soup.find("article")
            if art:
                content_html = clean_html(str(art))
                if not title:
                    h = art.find(["h1", "h2"])
                    if h:
                        title = h.get_text(strip=True)
            # maior bloco por <p>
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
            if og and og.get("content"):
                img = og["content"]
            elif tw and tw.get("content"):
                img = tw["content"]
        except:
            pass

        return title, content_html, img, final
    except Exception as e:
        log("[extract_from_article_url] erro:", e, "| url=", url)
        return "", "", "", url

def extract_from_rss_url(rss_url: str):
    """
    Lê um feed RSS e tenta pegar o primeiro item válido (com link).
    Retorna: (title, content_html, image_url, final_url) do artigo extraído
    """
    try:
        r = http_get(rss_url, timeout=TIMEOUT, accept="application/xml,text/xml,application/rss+xml,text/html")
        soup = BeautifulSoup(r.content, "xml")
        items = soup.find_all(["item", "entry"])
        for it in items:
            link_tag = it.find("link")
            link = ""
            if link_tag:
                # alguns RSS usam <link href="...">
                link = link_tag.get("href") or (link_tag.text or "").strip()
            if not link:
                guid = it.find("guid")
                if guid and guid.text:
                    link = guid.text.strip()
            if not link:
                continue

            # tenta extrair da notícia
            title, html, img, final = extract_from_article_url(link)
            if len(extract_plain(html)) >= 400:
                return title, html, img, final
        # nenhum item OK
        return "", "", "", rss_url
    except Exception as e:
        log("[extract_from_rss_url] erro:", e, "| rss=", rss_url)
        return "", "", "", rss_url


# ================== IA (TextSynth opcional) ==================
def textsynth_rewrite(title: str, plain: str):
    """
    Se TEXTSYNTH_KEY for fornecida, reescreve o texto em HTML limpo.
    Caso contrário, devolve o plain em <p>.
    """
    if not TEXTSYNTH_KEY:
        # Sem chave -> devolve original limpo
        html = f"<p>{plain}</p>"
        return title, html, ""
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
        # tenta capturar "meta descrição: ..."
        meta = ""
        m = re.search(r"meta descrição[:\-]\s*(.+)$", out, flags=re.I | re.M)
        if m:
            meta = m.group(1).strip()[:160]
        return title or "", out, meta
    except Exception as e:
        log("[TextSynth] erro:", e)
        return title, f"<p>{plain}</p>", ""


# ================== LÓGICA PRINCIPAL ==================
def scrape_once():
    """
    - Para cada SOURCE:
      * se for RSS → pega primeiro item válido e extrai artigo
      * se for artigo → extrai direto
    - Reescreve (se TEXTSYNTH_KEY setada) ou usa limpo
    - Salva em /tmp/autopost-data/ultimo.json
    """
    global LAST_ARTICLE
    if not SOURCES:
        log("[JOB] Sem fontes configuradas.")
        return

    for src in list(SOURCES):
        src = src.strip()
        if not src:
            continue

        if is_rss_url(src):
            title, content_html, img, final = extract_from_rss_url(src)
        else:
            title, content_html, img, final = extract_from_article_url(src)

        plain = extract_plain(content_html)
        if len(plain) < 400:
            log("[JOB] Conteúdo curto/insuficiente:", src)
            continue

        # reescrita opcional
        new_title, rewritten_html, meta = textsynth_rewrite(title, plain)
        if len(extract_plain(rewritten_html)) < 400:
            log("[JOB] Reescrito ficou curto, pulando:", src)
            continue

        data = build_json(new_title or title, rewritten_html, img, final)
        LAST_ARTICLE = data
        with open(LAST_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        log("[JOB] Artigo atualizado em ultimo.json:", data["title"][:80])
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
      "sources": ["https://g1.globo.com/rss/g1/vale-do-paraiba-regiao/rss.xml",
                  "https://www.site.com/noticia/123.html"],
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
            if s:
                urls.append(s)
        global SOURCES
        if replace:
            SOURCES = urls
        else:
            # append sem duplicar
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
    # inicia o agendador em background
    th = threading.Thread(target=scheduler_loop, daemon=True)
    th.start()
    app.run(host="0.0.0.0", port=PORT)
