import os, time, json, threading, re
from datetime import datetime, timezone
import requests
from bs4 import BeautifulSoup
from readability import Document
from flask import Flask, jsonify, request

# ===== Config =====
PORT = int(os.environ.get("PORT", "10000"))
TEXTSYNTH_KEY = os.environ.get("TEXTSYNTH_KEY", "")  # opcional: sua chave TextSynth
SCRAPE_INTERVAL = int(os.environ.get("SCRAPE_INTERVAL", "300"))  # segundos (5min)
WAIT_GNEWS = int(os.environ.get("WAIT_GNEWS", "20"))  # espera para links news.google.com
CITY_CATEGORY = {"caraguatatuba": 116, "são sebastião": 118, "sao sebastiao":118, "ilhabela":117, "ubatuba":119}

app = Flask(__name__)

SOURCES = []        # lista de sites (uma por linha via /sources/update)
LAST_ARTICLE = {}   # cache do ultimo.json em memória
DATA_DIR = "/tmp/autopost-data"
os.makedirs(DATA_DIR, exist_ok=True)
LAST_PATH = os.path.join(DATA_DIR, "ultimo.json")

def log(*a): print(*a, flush=True)

def http_get(url, timeout=20, headers=None, allow_redirects=True):
    h = {"User-Agent":"Mozilla/5.0 (AutoPost Server)","Accept":"text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
    if headers: h.update(headers)
    r = requests.get(url, timeout=timeout, allow_redirects=allow_redirects, headers=h)
    r.raise_for_status()
    return r

def resolve_gnews(url):
    if "news.google.com" in url:
        log("[resolve_gnews] waiting", WAIT_GNEWS, "s before resolving")
        time.sleep(WAIT_GNEWS)
        try:
            r = http_get(url, timeout=30, allow_redirects=True, headers={"Accept":"text/html"})
            return r.url
        except Exception as e:
            log("[resolve_gnews] fallback ->", url)
            return url
    return url

def clean_html(html):
    if not html: return ""
    for tag in ["script","style","nav","aside","footer","form","noscript"]:
        html = re.sub(fr"<{tag}\b[^>]*>.*?</{tag}>", "", html, flags=re.I|re.S)
    kill = r"(leia também|veja também|publicidade|anúncio|anuncio|assista também|vídeo relacionado|video relacionado)"
    html = re.sub(rf"<h\d[^>]*>\s*{kill}\s*</h\d>", "", html, flags=re.I)
    html = re.sub(rf"<p[^>]*>\s*{kill}.*?</p>", "", html, flags=re.I|re.S)
    html = re.sub(r"(\s*\n\s*){3,}", "\n\n", html)
    return html

def extract_article(url):
    try:
        url2 = resolve_gnews(url)
        r = http_get(url2, timeout=30)
        doc = Document(r.text)
        title = doc.short_title() or ""
        content_html = clean_html(doc.summary(html_partial=True))
        if not content_html or len(BeautifulSoup(content_html, "lxml").get_text(strip=True)) < 300:
            soup = BeautifulSoup(r.text, "lxml")
            art = soup.find("article")
            if art:
                content_html = clean_html(str(art))
                if not title:
                    h1 = art.find(["h1","h2"])
                    if h1: title = h1.get_text(strip=True)
            if not content_html:
                best,score = None,0
                for div in soup.find_all(["div","main","section"]):
                    pcount = len(div.find_all("p"))
                    tlen = len(div.get_text(" ", strip=True))
                    sc = pcount*10 + tlen
                    if sc>score: best,score = div,sc
                if best: content_html = clean_html(str(best))
        img = ""
        try:
            soup_head = BeautifulSoup(r.text, "lxml")
            og = soup_head.find("meta", attrs={"property":"og:image"})
            tw = soup_head.find("meta", attrs={"name":"twitter:image"})
            if og and og.get("content"): img = og["content"]
            elif tw and tw.get("content"): img = tw["content"]
        except: pass
        return title.strip(), content_html.strip(), img.strip(), url2
    except Exception as e:
        log("[extract_article] error:", e)
        return "","", "", url

def extract_plain(html):
    soup = BeautifulSoup(html, "lxml")
    return soup.get_text(" ", strip=True)

def textsynth_rewrite(title, plain):
    if not TEXTSYNTH_KEY:
        return title, f"<p>{plain}</p>", ""
    prompt = f"""
Você é um jornalista do Litoral Norte de SP. Reescreva jornalisticamente o texto abaixo em HTML limpo (apenas <p>, <h2>, <ul><li>, <strong>, <em>). 4-7 parágrafos. Sem publicidade nem 'leia também'. Gere meta descrição (160 chars).

TÍTULO ORIGINAL: {title}

TEXTO ORIGINAL:
{plain}
"""
    try:
        r = requests.post(
            "https://api.textsynth.com/v1/engines/gptj_6B/completions",
            headers={"Authorization": f"Bearer {TEXTSYNTH_KEY}", "Content-Type":"application/json"},
            json={"prompt": prompt, "max_tokens": 900, "temperature": 0.6, "stop": ["</html>","</body>"]},
            timeout=40
        )
        r.raise_for_status()
        data = r.json()
        out = (data.get("text") or "").strip()
        out = re.sub(r"</?(html|body|head)[^>]*>", "", out, flags=re.I)
        meta = ""
        m = re.search(r"meta descrição[:\-]\s*(.+)$", out, flags=re.I|re.M)
        if m: meta = m.group(1).strip()[:160]
        return (title or ""), out, meta
    except Exception as e:
        log("[textsynth] error:", e)
        return title, f"<p>{plain}</p>", ""

def guess_category(text):
    t = text.lower()
    for k,v in CITY_CATEGORY.items():
        if k in t: return v
    return 1

def generate_tags(title, plain):
    txt = f"{title} {plain}".lower()
    words = re.findall(r"[a-zá-úà-ùâ-ûã-õç0-9]{3,}", txt, flags=re.I)
    stop = set("a o os as de do da dos das em no na nos nas para por com sem sobre entre e ou que sua seu suas seus já não sim foi são será ser está estão era pelo pela pelos pelas lhe eles elas dia ano hoje ontem amanhã the and of to in on for with from".split())
    freq = {}
    for w in words:
        if w in stop: continue
        if w.isdigit(): continue
        freq[w] = freq.get(w,0)+1
    return [w for w,_ in sorted(freq.items(), key=lambda x:-x[1])][:10]

def build_json(title, html, img, source):
    plain = extract_plain(html)
    cat = guess_category(plain + " " + title)
    tags = generate_tags(title, plain)
    meta = (plain[:157] + "...") if len(plain)>160 else plain
    return {
        "title": title,
        "content_html": html,
        "meta_description": meta,
        "tags": tags,
        "category": cat,
        "image": img,
        "source": source,
        "generated_at": datetime.now(timezone.utc).isoformat()
    }

def scrape_once():
    global LAST_ARTICLE
    if not SOURCES:
        log("[JOB] Sem fontes configuradas.")
        return
    for src in list(SOURCES):
        title, content_html, img, final_url = extract_article(src)
        plain = extract_plain(content_html)
        if len(plain) < 400:
            continue
        new_title, rewritten_html, meta = textsynth_rewrite(title, plain)
        if rewritten_html and len(extract_plain(rewritten_html)) >= 400:
            data = build_json(new_title or title, rewritten_html, img, final_url)
            LAST_ARTICLE = data
            with open(LAST_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            log("[JOB] Artigo atualizado em ultimo.json:", data["title"][:70])
            return
    log("[JOB] Nenhuma fonte retornou conteúdo suficiente.")

def scheduler_loop():
    while True:
        try:
            scrape_once()
        except Exception as e:
            log("[JOB] erro:", e)
        time.sleep(SCRAPE_INTERVAL)

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
    # sem token, simples
    try:
        payload = request.get_json(force=True, silent=True) or {}
        sources = payload.get("sources") or []
        replace = bool(payload.get("replace", True))
        urls = []
        for s in sources:
            s = str(s).strip()
            if s: urls.append(s)
        global SOURCES
        if replace: SOURCES = urls
        else: SOURCES.extend([u for u in urls if u not in SOURCES])
        return jsonify({"ok": True, "count": len(SOURCES)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

@app.route("/artigos/ultimo.json")
def ultimo_json():
    if os.path.exists(LAST_PATH):
        try:
            with open(LAST_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return jsonify(data)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify(LAST_ARTICLE or {"ok": False, "error":"vazio"}), 200

@app.route("/job/run")
def job_run():
    scrape_once()
    return jsonify({"ok": True})

if __name__ == "__main__":
    th = threading.Thread(target=scheduler_loop, daemon=True)
    th.start()
    app.run(host="0.0.0.0", port=PORT)
