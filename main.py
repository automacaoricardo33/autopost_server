import os, time, json, threading, re, math
from datetime import datetime, timezone, timedelta
from urllib.parse import quote_plus, urlparse
import email.utils as eut
import requests
from bs4 import BeautifulSoup
from readability import Document
from flask import Flask, jsonify

# ================== CONFIG RÁPIDA ==================
PORT = int(os.environ.get("PORT", "10000"))

# KEYWORDS: aceita vírgula OU ponto-e-vírgula
_raw_kw = os.environ.get("KEYWORDS", "")
KEYWORDS = [k.strip() for k in re.split(r"[;,]", _raw_kw) if k.strip()]

# IA (TextSynth opcional). Se vazio, gera com heurística local.
TEXTSYNTH_KEY = os.environ.get("TEXTSYNTH_KEY", "")

# Tempo/agressividade
RECENT_HOURS    = int(os.environ.get("RECENT_HOURS", "6"))
SCRAPE_INTERVAL = int(os.environ.get("SCRAPE_INTERVAL", "300"))   # 5 min
WAIT_GNEWS      = int(os.environ.get("WAIT_GNEWS", "3"))          # ⚡ 3s
TIMEOUT         = int(os.environ.get("TIMEOUT", "20"))            # ⚡ 20s

# Tolerância de conteúdo (para não travar em notas curtas)
MIN_CHARS       = int(os.environ.get("MIN_CHARS", "120"))
MIN_PARAGRAPHS  = int(os.environ.get("MIN_PARAGRAPHS", "1"))

# Cidades → categoria WP (ajuste se quiser)
CITY_CATEGORY = {
    "caraguatatuba": 116,
    "ilhabela": 117,
    "são sebastião": 118,
    "sao sebastiao": 118,
    "ubatuba": 119,
}
CITIES = ["Caraguatatuba","São Sebastião","Ilhabela","Ubatuba","Litoral Norte"]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/122.0.0.0 Safari/537.36")
BASE_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.google.com/",
}

# ================== APP/ESTADO ==================
app = Flask(__name__)
LAST_ARTICLE = {}
DATA_DIR = "/tmp/autopost-data"
os.makedirs(DATA_DIR, exist_ok=True)
LAST_PATH = os.path.join(DATA_DIR, "ultimo.json")

# ================== UTILS ==================
def log(*a): print(*a, flush=True)

def http_get(url, timeout=TIMEOUT, allow_redirects=True, accept=None):
    headers = dict(BASE_HEADERS)
    if accept: headers["Accept"] = accept
    r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=allow_redirects)
    r.raise_for_status()
    return r

def extract_plain(html: str) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    return soup.get_text(" ", strip=True)

def count_paragraphs(html: str) -> int:
    return len(re.findall(r"<p\b[^>]*>.*?</p>", html or "", flags=re.I | re.S))

def clean_html(html: str) -> str:
    if not html: return ""
    for tag in ["script","style","nav","aside","footer","form","noscript"]:
        html = re.sub(fr"<{tag}\b[^>]*>.*?</{tag}>", "", html, flags=re.I|re.S)
    kill = r"(leia também|veja também|publicidade|anúncio|anuncio|assista também|vídeo relacionado|video relacionado)"
    html = re.sub(rf"<h\d[^>]*>\s*{kill}\s*</h\d>", "", html, flags=re.I)
    html = re.sub(rf"<p[^>]*>\s*{kill}.*?</p>", "", html, flags=re.I|re.S)
    html = re.sub(r"(\s*\n\s*){3,}", "\n\n", html)
    return html

def resolve_google_news(url: str) -> str:
    if "news.google.com" not in url: return url
    # Espera curta p/ o redirecionamento do GNews estabilizar
    if WAIT_GNEWS > 0:
        log("[GNEWS] aguardando", WAIT_GNEWS, "s:", url)
        time.sleep(WAIT_GNEWS)
    try:
        r = http_get(url, allow_redirects=True)
        return r.url
    except Exception as e:
        log("[GNEWS] fallback sem resolver:", e)
        return url

def parse_date_any(dt_text: str):
    if not dt_text: return None
    s = dt_text.strip()
    try:
        return eut.parsedate_to_datetime(s)  # RFC-2822
    except Exception:
        pass
    try:
        if s.endswith("Z"): s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)     # ISO/RFC-3339
    except Exception:
        return None

def is_recent_dt(dt_obj: datetime, now_utc: datetime, max_hours: int) -> bool:
    if not isinstance(dt_obj, datetime): return False
    if dt_obj.tzinfo is None: dt_obj = dt_obj.replace(tzinfo=timezone.utc)
    delta = now_utc - dt_obj.astimezone(timezone.utc)
    return timedelta(0) <= delta <= timedelta(hours=max_hours)

def hostname_to_source_name(url: str) -> str:
    try:
        host = urlparse(url).hostname or ""
        host = host.replace("www.","")
        # mapeamentos simples
        aliases = {
            "g1.globo.com": "G1",
            "globo.com": "G1",
            "cnnbrasil.com.br": "CNN Brasil",
            "uol.com.br": "UOL",
            "folha.uol.com.br": "Folha de S.Paulo",
            "estadao.com.br": "Estadão",
            "costanorte.com.br": "Costa Norte",
            "radarlitoral.com.br": "Radar Litoral",
        }
        return aliases.get(host, host.title() or "Fonte")
    except:
        return "Fonte"

def guess_category(text: str) -> int:
    t = (text or "").lower()
    for k, v in CITY_CATEGORY.items():
        if k in t: return v
    return 1

def generate_tags(title: str, plain: str):
    base = f"{title} {plain}".lower()
    words = re.findall(r"[a-zá-úà-ùâ-ûã-õç0-9]{3,}", base, flags=re.I)
    stop = set("""a o os as de do da dos das em no na nos nas para por com sem sobre entre e ou que sua seu suas seus
                  já não sim foi são será ser está estão era pelo pela pelos pelas lhe eles elas dia ano hoje ontem amanhã
                  the and of to in on for with from""".split())
    freq = {}
    for w in words:
        if w in stop or w.isdigit(): continue
        freq[w] = freq.get(w, 0) + 1
    tags = [w for w,_ in sorted(freq.items(), key=lambda x: -x[1])]
    # Inserir cidades quando fizer sentido
    for c in ["caraguatatuba","são sebastião","ilhabela","ubatuba","litoral norte"]:
        if c not in tags: tags.insert(0, c)
    return [t for t in tags if t][:10]

def soft_split_sentences(text: str):
    # separa em frases simples
    parts = re.split(r"(?<=[\.\!\?])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]

def chunk_into_paragraphs(plain: str, target_paras=4):
    sents = soft_split_sentences(plain)
    if not sents: return []
    # distribuir frases em 4-5 parágrafos
    n = len(sents)
    k = min(max(target_paras, 1), 5)
    per = max(1, math.ceil(n / k))
    paras = []
    for i in range(0, n, per):
        chunk = " ".join(sents[i:i+per]).strip()
        if chunk: paras.append(chunk)
        if len(paras) >= 5: break
    # garantir 4-5
    if len(paras) < 4 and len(sents) >= 2:
        # junta para chegar em pelo menos 4
        while len(paras) < 4 and sents:
            paras.append(sents.pop(0))
    return paras[:5]

# ================== PROMPT DO PORTAL (formatação final) ==================
def format_vozlitoral_output(title_opt: str, paragraphs: list, source_name: str, meta_desc: str, tags_list: list) -> str:
    """
    Retorna UMA STRING no formato exigido:
    ### Título
    <p1>
    <p2>
    <p3>
    <p4>
    Fonte: Nome
    ---
    **Meta descrição:** ...
    ---
    **Tags:** a, b, c
    """
    # Normaliza paragraph count (4-5)
    if len(paragraphs) < 4 and paragraphs:
        # duplica últimos trechos curtos para preencher
        while len(paragraphs) < 4:
            paragraphs.append(paragraphs[-1])
    if len(paragraphs) > 5:
        paragraphs = paragraphs[:5]

    blocks = []
    blocks.append(f"### {title_opt.strip()}")
    for p in paragraphs:
        blocks.append(p.strip())
    blocks.append(f"Fonte: {source_name.strip()}")
    blocks.append("---")
    blocks.append(f"**Meta descrição:** {meta_desc.strip()[:160]}")
    blocks.append("---")
    tags_txt = ", ".join([t.lower() for t in tags_list if t])
    if not tags_txt:
        tags_txt = "litoral norte, caraguatatuba, são sebastião, ilhabela, ubatuba"
    blocks.append(f"**Tags:** {tags_txt}")
    return "\n".join(blocks).strip()

def textsynth_with_prompt(source_url: str, title: str, plain: str):
    """
    Usa TextSynth COM seu prompt fixo para gerar a saída no formato Voz do Litoral.
    """
    if not TEXTSYNTH_KEY:
        return ""
    source_name = hostname_to_source_name(source_url)
    prompt = f"""--- INÍCIO DO PROMPT ---
PERSONA
Você é um jornalista digital e especialista em SEO, responsável por redigir notícias para o portal "Voz do Litoral". Seu público são os moradores das cidades de Caraguatatuba, São Sebastião, Ilhabela e Ubatuba. Seu objetivo é pegar uma notícia de uma fonte externa e reescrevê-la de forma clara, objetiva e otimizada, sempre conectando o assunto à realidade e ao interesse local.

TAREFA PRINCIPAL
Sua tarefa é receber uma URL de uma notícia e transformá-la em um texto otimizado para SEO, seguindo RIGOROSAMENTE a estrutura de saída abaixo. Você deve extrair a informação principal do link e reescrevê-la com suas próprias palavras, no tom e estilo do portal.

ESTRUTURA DE SAÍDA (OBRIGATÓRIA)
Você deve gerar a resposta exatamente neste formato, sem adicionar ou remover nenhum elemento.

1. Título (Headline)
Formato: ### [Seu Título Otimizado]

2. Corpo do Texto
Formato: Parágrafos de texto simples.
Requisitos: 4 a 5 parágrafos; primeiro parágrafo é o lide.

3. Fonte
Formato: Fonte: [Nome do Veículo Original]

4. Linha Separadora
Formato: ---

5. Meta Descrição
Formato: **Meta descrição:** [até 160 caracteres]

6. Linha Separadora
Formato: ---

7. Tags
Formato: **Tags:** [5 a 10 palavras, minúsculas, separadas por vírgula, incluir cidades do Litoral Norte quando fizer sentido]

DADOS DE ENTRADA
URL: {source_url}
Título original: {title}
Texto original (limpo):
{plain}

AGORA GERE A SAÍDA EXATAMENTE NO FORMATO EXIGIDO.
--- FIM DO PROMPT ---"""
    try:
        r = requests.post(
            "https://api.textsynth.com/v1/engines/gptj_6B/completions",
            headers={"Authorization": f"Bearer {TEXTSYNTH_KEY}", "Content-Type": "application/json"},
            json={"prompt": prompt, "max_tokens": 900, "temperature": 0.6},
            timeout=60
        )
        r.raise_for_status()
        data = r.json()
        out = (data.get("text") or "").strip()
        # Sanitização mínima
        out = re.sub(r"</?(html|body|head)[^>]*>", "", out, flags=re.I)
        return out
    except Exception as e:
        log("[TextSynth] erro:", e)
        return ""

# ================== GNEWS ==================
def gnews_query_with_when(keyword: str) -> str:
    when = f"when:{RECENT_HOURS}h" if RECENT_HOURS <= 48 else f"when:{max(1, RECENT_HOURS//24)}d"
    q = f"{keyword} {when}"
    return f"https://news.google.com/rss/search?q={quote_plus(q)}&hl=pt-BR&gl=BR&ceid=BR:pt-419"

def pick_from_gnews(keyword: str):
    feed = gnews_query_with_when(keyword)
    now_utc = datetime.now(timezone.utc)
    try:
        r = http_get(feed, timeout=TIMEOUT, accept="application/rss+xml,application/xml,text/xml,text/html")
        soup = BeautifulSoup(r.content, "xml")
        items = soup.find_all(["item","entry"])

        parsed = []
        for it in items:
            dt = None
            for tag in ("pubDate","updated","published"):
                t = it.find(tag)
                if t and t.text:
                    dt = parse_date_any(t.text); break
            parsed.append((it, dt))
        parsed.sort(key=lambda x: x[1] or datetime(1970,1,1,tzinfo=timezone.utc), reverse=True)

        for it, dt in parsed[:12]:
            if dt and not is_recent_dt(dt, now_utc, RECENT_HOURS):
                continue
            link = ""
            link_tag = it.find("link")
            if link_tag:
                link = link_tag.get("href") or (link_tag.text or "").strip()
            if not link:
                guid = it.find("guid"); 
                if guid and guid.text: link = guid.text.strip()
            if not link: continue

            title = (it.find("title").text.strip() if it.find("title") else "")
            title_ex, html, img, final = extract_from_article_url(link)
            final_title = title_ex or title
            if len(extract_plain(html)) >= MIN_CHARS and count_paragraphs(html) >= MIN_PARAGRAPHS:
                return final_title, html, img, final
        return "", "", "", feed
    except Exception as e:
        log("[pick_from_gnews] erro:", e, "| kw=", keyword)
        return "", "", "", feed

def extract_from_article_url(url: str):
    try:
        final = resolve_google_news(url)
        r = http_get(final, timeout=TIMEOUT)
        html = r.text

        doc = Document(html)
        title = (doc.short_title() or "").strip()
        content_html = clean_html(doc.summary(html_partial=True) or "")

        if len(extract_plain(content_html)) < MIN_CHARS or count_paragraphs(content_html) < MIN_PARAGRAPHS:
            soup = BeautifulSoup(html, "lxml")
            art = soup.find("article")
            if art:
                content_html = clean_html(str(art))
                if not title:
                    h = art.find(["h1","h2"])
                    if h: title = h.get_text(strip=True)
            if len(extract_plain(content_html)) < MIN_CHARS or count_paragraphs(content_html) < MIN_PARAGRAPHS:
                best, score = None, 0
                for div in soup.find_all(["div","main","section"]):
                    pcount = len(div.find_all("p"))
                    tlen = len(div.get_text(" ", strip=True))
                    sc = pcount*10 + tlen
                    if sc > score: best, score = div, sc
                if best:
                    content_html = clean_html(str(best))

        img = ""
        try:
            soup2 = BeautifulSoup(html, "lxml")
            og = soup2.find("meta", attrs={"property":"og:image"})
            tw = soup2.find("meta", attrs={"name":"twitter:image"})
            if og and og.get("content"): img = og["content"]
            elif tw and tw.get("content"): img = tw["content"]
        except:
            pass

        return title, content_html, img, final
    except Exception as e:
        log("[extract_from_article_url] erro:", e, "| url=", url)
        return "", "", "", url

# ================== PIPELINE ==================
def build_json_for_wp(title_display: str, content_formatted: str, img: str, source_url: str):
    # Mesmo JSON de sempre; content_html contém o texto no formato do prompt.
    plain = extract_plain(content_formatted)
    cat = guess_category(f"{plain} {title_display}")
    tags = generate_tags(title_display, plain)
    meta = (plain[:157] + "...") if len(plain) > 160 else plain
    return {
        "title": title_display.strip(),
        "content_html": content_formatted.strip(),
        "meta_description": meta,
        "tags": tags,
        "category": cat,
        "image": (img or "").strip(),
        "source": (source_url or "").strip(),
        "generated_at": datetime.now(timezone.utc).isoformat()
    }

def save_article(data: dict):
    global LAST_ARTICLE
    LAST_ARTICLE = data
    with open(LAST_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

def scrape_once():
    if not KEYWORDS:
        log("[JOB] Sem KEYWORDS definidas.")
        return

    log("[JOB] Keywords ativas:", KEYWORDS)
    for kw in KEYWORDS:
        title, content_html, img, final = pick_from_gnews(kw)
        plain = extract_plain(content_html)

        if len(plain) < MIN_CHARS or count_paragraphs(content_html) < MIN_PARAGRAPHS:
            log("[JOB] Conteúdo insuficiente (kw):", kw)
            continue

        # ====== Geração no formato do prompt ======
        source_name = hostname_to_source_name(final)

        # 1) Tentar TextSynth com o prompt fixo
        output = textsynth_with_prompt(final, title, plain)

        if not output:
            # 2) Fallback local: 4-5 parágrafos a partir do texto limpo
            paras = chunk_into_paragraphs(plain, target_paras=4)
            # Título otimizado simples (mantém original se já existir)
            title_opt = title
            # Meta + tags
            tags_list = generate_tags(title_opt, plain)
            # Meta curta
            meta = (plain[:160]).strip()
            output = format_vozlitoral_output(title_opt, paras, source_name, meta, tags_list)

        data = build_json_for_wp(title, output, img, final)
        save_article(data)
        log("[JOB] ultimo.json atualizado:", data["title"][:100], "| kw:", kw)
        return

    log("[JOB] Nenhuma keyword RECENTE com texto suficiente.")

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
    return "AutoPost Voz do Litoral OK (sem espera longa)", 200

@app.route("/health")
def health():
    return jsonify({
        "ok": True,
        "time": datetime.utcnow().isoformat(),
        "keywords": KEYWORDS,
        "interval": SCRAPE_INTERVAL,
        "recent_hours": RECENT_HOURS,
        "min_chars": MIN_CHARS,
        "min_paragraphs": MIN_PARAGRAPHS,
        "wait_gnews": WAIT_GNEWS,
        "timeout": TIMEOUT
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

@app.route("/gnews/<kw>")
def dbg_gnews(kw):
    feed = gnews_query_with_when(kw)
    out = {"feed": feed, "items": []}
    try:
        r = http_get(feed, timeout=TIMEOUT, accept="application/rss+xml,application/xml,text/xml,text/html")
        soup = BeautifulSoup(r.content, "xml")
        for it in soup.find_all(["item","entry"]):
            title = (it.find("title").text.strip() if it.find("title") else "")
            dt = None
            for tag in ("pubDate","updated","published"):
                t = it.find(tag)
                if t and t.text:
                    dt = parse_date_any(t.text); break
            out["items"].append({
                "title": title,
                "date_raw": (it.find("pubDate").text if it.find("pubDate") else (it.find("updated").text if it.find("updated") else (it.find("published").text if it.find("published") else ""))),
                "date_parsed_utc": dt.astimezone(timezone.utc).isoformat() if dt else None
            })
    except Exception as e:
        out["error"] = str(e)
    return jsonify(out)

if __name__ == "__main__":
    th = threading.Thread(target=scheduler_loop, daemon=True)
    th.start()
    app.run(host="0.0.0.0", port=PORT)
