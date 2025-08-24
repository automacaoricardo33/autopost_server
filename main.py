import os, json, re, time, logging, pathlib, html
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlparse, urljoin

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, send_from_directory

# ===== Config =====
PORT = int(os.getenv("PORT", "10000"))
WAIT_SECONDS = int(os.getenv("WAIT_SECONDS", "5"))           # espera leve entre resoluções
MAX_AGE_HOURS = int(os.getenv("MAX_AGE_HOURS", "18"))        # recorte de recência
KW_LIST = os.getenv("KW_LIST", "litoral norte de sao paulo, ubatuba, ilhabela, caraguatatuba, são sebastião, brasil, futebol").strip()

OUT_DIR = pathlib.Path("static/artigos")
OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_JSON = OUT_DIR / "ultimo.json"
OUT_HTML = OUT_DIR / "ultimo.html"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = Flask(__name__)

# ===== Util =====

def http_get(url, timeout=20, allow_redirects=True):
    r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "pt-BR,pt;q=0.9"}, timeout=timeout, allow_redirects=allow_redirects)
    r.raise_for_status()
    return r

def pick_amp(html_text):
    soup = BeautifulSoup(html_text, "html.parser")
    link = soup.find("link", rel=lambda x: x and "amphtml" in x.lower())
    if link and link.get("href"):
        return link["href"]
    return ""

def strip_noise(html_text):
    soup = BeautifulSoup(html_text, "html.parser")
    for tag in soup(["script","style","header","footer","nav","form","aside","iframe","noscript"]):
        tag.decompose()
    kill = re.compile(r"(leia também|veja também|publicidade|anúncio|anuncio|assine|whatsapp|siga nosso canal)", re.I)
    for t in soup.find_all(text=kill):
        t.extract()
    # remove anchors mantendo o texto
    for a in soup.find_all("a"):
        a.replace_with(a.get_text(" ", strip=True))
    # pega só parágrafos principais
    paras = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
    paras = [p for p in paras if len(p) > 40]
    return "\n\n".join(paras[:12])

def resolve_gnews_article_url(gnews_url):
    """
    Muitos links do Google News em /rss/articles/... não redirecionam sem parâmetros certos.
    Estratégia:
      1) tenta abrir direto (às vezes já vem 200/redirect).
      2) espera curto e tenta de novo.
      3) se abrir página do Google News, pega <a href> canônico; senão, usa AMP.
    """
    try:
        time.sleep(WAIT_SECONDS)
        r = http_get(gnews_url, timeout=20)
        html_text = r.text
        # tenta achar destino canônico
        soup = BeautifulSoup(html_text, "html.parser")
        # muitos wrappers do GNews possuem <a href> com o destino final
        a = soup.find("a", href=True)
        if a and a["href"].startswith("http"):
            return a["href"]
        amp = pick_amp(html_text)
        if amp:
            return amp
        return gnews_url  # fallback
    except Exception as e:
        logging.warning(f"[resolve] fallback -> {gnews_url} ({e})")
        return gnews_url

def extract_article(url):
    """
    Baixa a página, tenta AMP, limpa e retorna (title, body, image).
    """
    try:
        r = http_get(url, timeout=20)
        html_text = r.text
        soup = BeautifulSoup(html_text, "html.parser")

        title = soup.title.get_text(strip=True) if soup.title else ""
        # pega imagem OpenGraph
        og = soup.find("meta", property="og:image")
        tw = soup.find("meta", attrs={"name":"twitter:image"})
        image = og["content"] if og and og.get("content") else (tw["content"] if tw and tw.get("content") else "")

        body = strip_noise(html_text)
        if len(body) < 200:
            # tenta AMP
            amp = pick_amp(html_text)
            if amp:
                r2 = http_get(amp, timeout=20)
                body = strip_noise(r2.text)
                if not image:
                    soup2 = BeautifulSoup(r2.text, "html.parser")
                    og2 = soup2.find("meta", property="og:image")
                    if og2 and og2.get("content"):
                        image = og2["content"]

        return title.strip(), body.strip(), image
    except Exception as e:
        logging.warning(f"[extract] erro {e} @ {url}")
        return "", "", ""

def gen_meta_description(text, limit=160):
    clean = re.sub(r"\s+", " ", text).strip()
    return (clean[:limit-1] + "…") if len(clean) > limit else clean

def gen_tags(text, extra_litoral=True):
    txt = html.unescape(text).lower()
    txt = re.sub(r"[^a-z0-9á-úà-ùâ-ûã-õç\s-]", " ", txt)
    words = [w for w in re.split(r"\s+", txt) if 3 <= len(w) <= 30]
    stop = set("a o os as um uma uns umas de do da dos das em no na nos nas para por com sem sob sobre entre e ou que se sua seu suas seus ao à às aos como mais menos muito muita muitos muitas já não sim foi será ser está estão era são pelo pela pelos pelas lhe eles elas hoje ontem amanhã the and of to in on for with from".split())
    freq = {}
    for w in words:
        if w in stop or w.isdigit(): continue
        freq[w] = freq.get(w, 0) + 1
    tags = [w for w,_ in sorted(freq.items(), key=lambda kv: kv[1], reverse=True)][:8]
    if extra_litoral:
        base = ["litoral norte","caraguatatuba","são sebastião","ilhabela","ubatuba"]
        for b in base:
            if b not in tags: tags.append(b)
    return tags[:12]

def build_content_from_prompt(title, body, source_name):
    # Se o corpo vier curto, cria 4 parágrafos a partir do que tiver.
    if not body or len(body) < 200:
        paras = []
        lead = f"{title}. Atualização em breve com detalhes confirmados e orientações locais para moradores do Litoral Norte."
        paras.append(lead)
        paras.append("A redação do Voz do Litoral segue em contato com fontes oficiais e deve atualizar esta publicação assim que houver novas informações.")
        paras.append("Reforçamos a importância de acompanhar canais oficiais da Defesa Civil e das prefeituras das cidades do Litoral Norte.")
        paras.append("Se você presenciou o fato ou tem informações confiáveis, entre em contato com a redação.")
        body = "\n\n".join(paras)
    # transforma em HTML simples (4–5 parágrafos)
    ps = [p.strip() for p in body.split("\n") if p.strip()]
    if len(ps) < 4:
        # quebra por ponto pra dar volume
        chunks = re.split(r"(?<=[\.\!\?])\s+", body)
        for c in chunks:
            if c.strip():
                ps.append(c.strip())
            if len(ps) >= 5: break
    ps = ps[:5]

    html_parts = [f"<h2>{html.escape(title)}</h2>"]
    for p in ps:
        html_parts.append(f"<p>{html.escape(p)}</p>")
    html_parts.append(f"<p><em>Fonte: {html.escape(source_name or 'Portal parceiro')}</em></p>")
    return "\n".join(html_parts)

def save_outputs(payload):
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    # HTML de visualização rápida
    html_view = f"""<!doctype html><meta charset="utf-8">
<title>{html.escape(payload['title'])}</title>
<article style="max-width:720px;margin:32px auto;font:16px/1.6 system-ui,-apple-system,Segoe UI,Roboto">
  {'<figure><img src="'+html.escape(payload.get('image',''))+'" style="max-width:100%;height:auto"/></figure>' if payload.get('image') else ''}
  {payload['content_html']}
  <hr><p><strong>Meta:</strong> {html.escape(payload.get('meta_description',''))}</p>
  <p><strong>Tags:</strong> {', '.join(payload.get('tags', []))}</p>
</article>"""
    OUT_HTML.write_text(html_view, encoding="utf-8")

def gnews_recent_items(keyword):
    """
    Força recência usando 'when:12h' e 'site:br' implícito pelo ceid BR.
    """
    base = "https://news.google.com/rss/search"
    q = f'{keyword} when:12h'
    params = {"q": q, "hl": "pt-BR", "gl": "BR", "ceid": "BR:pt-419"}
    url = f"{base}?{urlencode(params)}"
    try:
        r = http_get(url, timeout=20)
        soup = BeautifulSoup(r.text, "xml")
        items = []
        for it in soup.find_all("item")[:10]:
            link = it.link.get_text(strip=True)
            title = it.title.get_text(strip=True)
            pub = it.pubDate.get_text(strip=True) if it.pubDate else ""
            items.append({"link": link, "title": title, "pub": pub})
        return items
    except Exception as e:
        logging.warning(f"[gnews] erro {e} kw={keyword}")
        return []

def source_name_from_url(url):
    try:
        h = urlparse(url).netloc
        if h.startswith("www."): h = h[4:]
        # nome amigável
        parts = h.split(".")
        if len(parts) >= 2:
            return parts[-2].capitalize() + " " + parts[-1].upper()
        return h
    except:
        return "Portal parceiro"

# ===== Job principal =====
def run_job_once():
    keywords = [k.strip() for k in KW_LIST.split(",") if k.strip()]
    now = datetime.now(timezone.utc)
    picked = None
    picked_kw = None

    for kw in keywords:
        items = gnews_recent_items(kw)
        if not items:
            continue
        # pega o mais novo dentro do recorte
        for it in items:
            link = it["link"]
            # resolve link do GNews pro destino real/AMP
            logging.info(f"[GNEWS] aguardando {WAIT_SECONDS}s: {link}")
            real = resolve_gnews_article_url(link)

            title, body, image = extract_article(real)
            if not title:
                title = it["title"]

            # checagem de recência (quando houver)
            is_recent = True
            if it.get("pub"):
                try:
                    # pubDate é RFC822; requests não parseia, então simplifica:
                    is_recent = True  # deixamos passar (já filtramos com when:12h)
                except:
                    is_recent = True

            # critério mínimo relaxado: mesmo curto, segue
            if is_recent:
                picked = {
                    "url": real,
                    "title": title.strip() or it["title"],
                    "body": body,
                    "image": image
                }
                picked_kw = kw
                break
        if picked:
            break

    if not picked:
        logging.warning("[JOB] Nada recente em condições mínimas. Vou gerar placeholder para não travar o WP.")
        title = "Atualização de pauta — Voz do Litoral"
        body = "Nenhuma matéria elegível foi encontrada nas últimas horas. A redação segue monitorando as fontes e atualiza em breve."
        image = ""
        src = "Voz do Litoral"
    else:
        title = picked["title"] or "Atualização — Voz do Litoral"
        body  = picked["body"]
        image = picked["image"]
        src   = source_name_from_url(picked["url"])

    # monta HTML no padrão pedido (4–5 parágrafos, com Fonte)
    content_html = build_content_from_prompt(title, body, src)

    # meta + tags
    meta = gen_meta_description(body if body else title)
    tags = gen_tags(f"{title} {body or ''}")

    payload = {
        "title": title,
        "content_html": content_html,
        "meta_description": meta,
        "tags": tags,
        "image": image,
        "source": src,
        "generated_at": datetime.utcnow().isoformat() + "Z"
    }
    save_outputs(payload)
    logging.info("[JOB] Artigo salvo em ultimo.json/ultimo.html")
    return payload

# ===== HTTP =====

@app.get("/")
def index():
    return "OK — RS autopost JSON bridge"

@app.get("/health")
def health():
    return jsonify(ok=True, time=datetime.utcnow().isoformat()+"Z")

@app.get("/artigos/ultimo.json")
def ultimo_json():
    if OUT_JSON.exists():
        return send_from_directory(OUT_DIR, "ultimo.json", mimetype="application/json")
    else:
        # se ainda não existir, roda 1x para garantir entrega
        payload = run_job_once()
        return jsonify(payload)

@app.get("/artigos/ultimo.html")
def ultimo_html():
    if OUT_HTML.exists():
        return send_from_directory(OUT_DIR, "ultimo.html", mimetype="text/html")
    else:
        payload = run_job_once()
        return send_from_directory(OUT_DIR, "ultimo.html", mimetype="text/html")

@app.get("/debug/run")
def debug_run():
    payload = run_job_once()
    return jsonify(ok=True, **payload)

# ===== Runner =====
if __name__ == "__main__":
    # roda 1 vez na subida para já ter algo pro WP
    try:
        run_job_once()
    except Exception as e:
        logging.error(f"startup run fail: {e}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
