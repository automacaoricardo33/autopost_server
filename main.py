import os, time, json, threading, re
from datetime import datetime, timezone
from urllib.parse import quote_plus
import requests
from bs4 import BeautifulSoup
from readability import Document
from flask import Flask, jsonify

# ================== CONFIG ==================
PORT = int(os.environ.get("PORT", "10000"))

# Palavras‑chave separadas por vírgula
# Ex.: "caraguatatuba, ilhabela, são sebastião, trânsito, futebol, fórmula 1"
KEYWORDS = [k.strip() for k in os.environ.get("KEYWORDS", "").split(",") if k.strip()]

# IA opcional (TextSynth). Se vazio, publica texto limpo.
TEXTSYNTH_KEY = os.environ.get("TEXTSYNTH_KEY", "")

# Agendador
SCRAPE_INTERVAL = int(os.environ.get("SCRAPE_INTERVAL", "300"))  # 5min default
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

# ================== APP/ESTADO ==================
app = Flask(__name__)

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

def extract_plain(html: str) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    return soup.get_text(" ", strip=True)

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

def pick_from_gnews(keyword: str):
    """
    Usa Google News RSS para a keyword e tenta pegar a 1ª matéria válida.
    Retorna (title, content_html, image_url, final_url)
    """
    feed = f"https://news.google.com/rss/search?q={quote_plus(keyword)}&hl=pt-BR&gl=BR&ceid=BR:pt-419"
    try:
        r = http_get(feed, timeout=TIMEOUT, accept="application/rss+xml,application/xml,text/xml,text/html")
        soup = BeautifulSoup(r.content, "xml")
        items = soup.find_all(["item", "entry"])
        for it in items:
            link = ""
            # link pode vir em <link> ou no <guid>
            link_tag = it.find("link")
            if link_tag:
                link = link_tag.get("href") or (link_tag.text or "").strip()
            if not link:
                guid = it.find("guid")
                if guid and guid.text: link = guid.text.strip()
            if not link:
                continue

            # resolve/link final e extrai
            title, html, img, final = extract_from_article_url(link)
            if len(extract_plain(html)) >= 400:
                return title, html, img, final
        return "", "", "", feed
    except Exception as e:
        log("[pick_from_gnews] erro:", e, "| kw=", keyword)
        return "", "", "", feed


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
def scrape_once():
    """
    Para cada KEYWORD:
      - consulta Google News RSS
      - extrai a primeira matéria válida
      - reescreve (se TEXTSYNTH_KEY), salva em /artigos/ultimo.json
    Para o WordPress basta ler /artigos/ultimo.json
    """
    global LAST_ARTICLE
    if not KEYWORDS:
        log("[JOB] Sem KEYWORDS definidas (configure a env KEYWORDS no Render).")
        return

    for kw in KEYWORDS:
        title, content_html, img, final = pick_from_gnews(kw)
        plain = extract_plain(content_html)
        if len(plain) < 400:
            log("[JOB] Conteúdo curto para kw:", kw)
            continue

        new_title, rewritten_html, meta = textsynth_rewrite(title, plain)
        if len(extract_plain(rewritten_html)) < 400:
            # se IA não ajudar, usa o limpo
            rewritten_html = f"<p>{plain}</p>"

        data = build_json(new_title or title, rewritten_html, img, final)
        LAST_ARTICLE = data
        with open(LAST_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        log("[JOB] ultimo.json atualizado com:", data["title"][:90], "| kw:", kw)
        return

    log("[JOB] Nenhuma keyword retornou conteúdo suficiente.")

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
    return "AutoPost Render (keywords→GNews→ultimo.json) OK", 200

@app.route("/health")
def health():
    return jsonify({
        "ok": True,
        "time": datetime.utcnow().isoformat(),
        "keywords": KEYWORDS,
        "interval": SCRAPE_INTERVAL
    })

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
    th = threading.Thread(target=scheduler_loop, daemon=True)
    th.start()
    app.run(host="0.0.0.0", port=PORT)
