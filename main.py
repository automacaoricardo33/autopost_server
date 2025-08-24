import os, re, json, time, logging, hashlib
from datetime import datetime, timezone
from urllib.parse import urlparse, urljoin, parse_qs

import requests
from flask import Flask, request, jsonify, send_from_directory, abort
from bs4 import BeautifulSoup

# =========================
# CONFIG
# =========================
PORT = int(os.getenv("PORT", "10000"))
TEXTSYNTH_KEY = os.getenv("TEXTSYNTH_KEY", "").strip()
TEXTSYNTH_ENGINE = os.getenv("TEXTSYNTH_ENGINE", "gptj_6B").strip()

# Publicar mesmo se o texto for curtinho (1 parágrafo / 80 chars)
FORCE_MIN = os.getenv("FORCE_MIN", "1").strip() == "1"
MIN_PARAGRAPHS = int(os.getenv("MIN_PARAGRAPHS", "1"))
MIN_CHARS = int(os.getenv("MIN_CHARS", "80"))

# Espera (em segundos) entre pegar o link do GNews e resolver destino
RESOLVE_WAIT = int(os.getenv("RESOLVE_WAIT", "5"))

SITE_NAME = os.getenv("SITE_NAME", "Voz do Litoral")
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "artigos")
OUTPUT_BASENAME = "ultimo.json"

# User-Agent decente para reduzir 403
UA = os.getenv("SCRAPER_UA", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                             "AppleWebKit/537.36 (KHTML, like Gecko) "
                             "Chrome/124.0 Safari/537.36 RS-Autoposter")

# =========================
# FLASK
# =========================
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("main")

# Garante pasta de saída
os.makedirs(OUTPUT_DIR, exist_ok=True)

# =========================
# UTILS
# =========================

def http_get(url: str, timeout: int = 20) -> requests.Response:
    """GET robusto com headers e follow redirects."""
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    return requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)

def clean_html_keep_paragraphs(html: str) -> str:
    """Remove ruídos mas mantém parágrafos básicos."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")

    # Remove elementos ruidosos
    for tag in soup.find_all(["script", "style", "aside", "nav", "header", "footer", "form"]):
        tag.decompose()

    # mata blocos de publicidade / CTA comuns
    KILL_PAT = re.compile(
        r"(leia também|veja também|compartilhe|publicidade|anúncio|anuncio|"
        r"assine|assista também|vídeo relacionado|video relacionado|"
        r"whatsapp|telegram|siga nosso canal|newsletter)",
        flags=re.I
    )
    for el in soup.find_all(text=KILL_PAT):
        try:
            el_parent = el.parent
            if el_parent: el_parent.decompose()
        except Exception:
            pass

    # mantém só parágrafos e headings mais prováveis
    keep = []
    for blk in soup.find_all(["article", "main", "section", "div"]):
        text_len = len(blk.get_text(strip=True))
        p_count = len(blk.find_all("p"))
        if p_count >= 2 and text_len > 200:
            keep.append(blk)

    # fallback: usa body
    if not keep:
        keep = [soup.body or soup]

    # escolhe o bloco com mais "pontos"
    def score(b):
        return len(b.find_all("p")) * 10 + len(b.get_text(strip=True))

    best = sorted(keep, key=score, reverse=True)[0]
    # remove anchors mantendo texto
    for a in best.find_all("a"):
        a.replace_with(a.get_text(" ", strip=True))
    # remove figures pesadas internas (corpo fica mais limpo)
    for fig in best.find_all("figure"):
        fig.decompose()

    # devolve HTML interno do melhor bloco
    return "".join(str(x) for x in best.contents).strip()

def extract_title_and_image(html: str, url: str) -> tuple[str, str]:
    """Extrai <title> e og:image se houver."""
    title = ""
    image = ""
    if not html:
        return title, image
    soup = BeautifulSoup(html, "html.parser")
    if soup.title and soup.title.string:
        title = soup.title.string.strip()

    # og:title pode ser melhor
    ogt = soup.find("meta", attrs={"property": "og:title"})
    if ogt and ogt.get("content"):
        title = ogt["content"].strip()

    # imagem
    for sel in [
        {"property": "og:image"},
        {"name": "twitter:image"},
        {"rel": "image_src"},
    ]:
        tag = soup.find("meta", attrs=sel) if "meta" in "meta" else None
        if not tag and "rel" in sel:
            link = soup.find("link", attrs=sel)
            if link and link.get("href"):
                image = link["href"].strip(); break
        if tag and tag.get("content"):
            image = tag["content"].strip(); break

    # normaliza URL absoluta
    if image and not image.lower().startswith(("http://", "https://")):
        try:
            image = urljoin(url, image)
        except Exception:
            pass

    return title, image

def to_plain_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    return soup.get_text("\n", strip=True)

def paragraph_count(html: str) -> int:
    soup = BeautifulSoup(html or "", "html.parser")
    return len(soup.find_all("p"))

def ensure_minimum(html: str) -> bool:
    if FORCE_MIN:
        return True
    p = paragraph_count(html)
    t = len(to_plain_text(html))
    return (p >= MIN_PARAGRAPHS) and (t >= MIN_CHARS)

def format_datetime_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()

# =========================
# IA: TEXTSYNTH (GPT‑J)
# =========================
def textsynth_generate(prompt: str, max_tokens: int = 700, temperature: float = 0.5) -> str:
    if not TEXTSYNTH_KEY:
        return ""
    url = f"https://api.textsynth.com/v1/engines/{TEXTSYNTH_ENGINE}/completions"
    payload = {
        "prompt": prompt,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stop": None
    }
    headers = {
        "Authorization": f"Bearer {TEXTSYNTH_KEY}",
        "Content-Type": "application/json"
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=45)
        r.raise_for_status()
        data = r.json()
        # API costuma devolver { "text": "..."} ou { "choices": [{"text":"..."}]}
        if isinstance(data, dict):
            if "text" in data and isinstance(data["text"], str):
                return data["text"]
            if "choices" in data and data["choices"]:
                return data["choices"][0].get("text", "")
        return ""
    except Exception as e:
        log.error(f"[IA] TextSynth falhou: {e}")
        return ""

def build_editorial_prompt(raw_title: str, raw_plain: str, source_host: str) -> str:
    # PROMPT exato que você pediu
    return f"""
PERSONA
Você é um jornalista digital e especialista em SEO, responsável por redigir notícias para o portal "Voz do Litoral". Seu público são os moradores das cidades de Caraguatatuba, São Sebastião, Ilhabela e Ubatuba. Seu objetivo é pegar uma notícia de uma fonte externa e reescrevê-la de forma clara, objetiva e otimizada, sempre conectando o assunto à realidade e ao interesse local.

TAREFA PRINCIPAL
Sua tarefa é receber uma URL de uma notícia e transformá-la em um texto otimizado para SEO, seguindo RIGOROSAMENTE a estrutura de saída abaixo. Você deve extrair a informação principal do link e reescrevê-la com suas próprias palavras, no tom e estilo do portal.

CONTEÚDO BRUTO (título + texto):
TÍTULO: {raw_title.strip()}
TEXTO:
{raw_plain.strip()}

ESTRUTURA DE SAÍDA (OBRIGATÓRIA)
Você deve gerar a resposta exatamente neste formato, sem adicionar ou remover nenhum elemento.

1. Título (Headline)
Formato: ### [Seu Título Otimizado]

2. Corpo do Texto
- 4 a 5 parágrafos
- Primeiro parágrafo deve ser o lide (resumo direto)
- Texto claro, objetivo, jornalístico e ORIGINAL (reescrito)

3. Fonte
Formato: Fonte: {source_host}

4. Linha Separadora
Formato: ---

5. Meta Descrição
Formato: **Meta descrição:** [máximo 160 caracteres, com palavras-chave principais]

6. Linha Separadora
Formato: ---

7. Tags
Formato: **Tags:** [5 a 10 palavras-chave, minúsculas, separadas por vírgula; incluir cidades do Litoral Norte quando pertinente]

AGORA ESCREVA APENAS O TEXTO FINAL NESSE FORMATO, NADA ALÉM.
""".strip()

def assemble_content_html(ia_text: str) -> tuple[str, str, list[str], str]:
    """
    Recebe o texto final da IA (no formato especificado) e extrai:
    - title
    - content_html (com <p> etc.)
    - tags (lista)
    - meta_description
    """
    text = ia_text.strip()

    # Título: linha que começa com ### 
    title = ""
    m = re.search(r"^###\s*(.+?)\s*$", text, flags=re.M)
    if m:
        title = m.group(1).strip()

    # Fonte:
    fonte_host = ""
    msrc = re.search(r"^\s*Fonte:\s*(.+?)\s*$", text, flags=re.M | re.I)
    if msrc:
        fonte_host = msrc.group(1).strip()

    # Meta descrição:
    meta_desc = ""
    md = re.search(r"^\s*\*\*Meta descrição:\*\*\s*(.+?)\s*$", text, flags=re.M | re.I)
    if md:
        meta_desc = md.group(1).strip()
        # corta com segurança
        if len(meta_desc) > 160:
            meta_desc = meta_desc[:157].rstrip() + "..."

    # Tags:
    tags = []
    tg = re.search(r"^\s*\*\*Tags:\*\*\s*(.+?)\s*$", text, flags=re.M | re.I)
    if tg:
        tags_line = tg.group(1)
        tags = [t.strip().lower() for t in tags_line.split(",") if t.strip()]
        tags = tags[:10]

    # Corpo = tudo entre o título e a linha "Fonte:" (em parágrafos)
    body_section = text
    if m:
        body_section = text[m.end():]  # depois do título
    if msrc:
        body_section = body_section[:msrc.start() - (m.end() if m else 0)]

    # Converte linhas em <p>, preservando blocos
    # Limpa marcações do prompt
    body_section = re.sub(r"^\s*2\.\s*Corpo do Texto\s*$", "", body_section, flags=re.M)
    body_section = body_section.strip()

    # Quebra em parágrafos por linhas vazias ou quebras duplas
    parts = [p.strip() for p in re.split(r"\n\s*\n", body_section) if p.strip()]
    p_html = "\n".join(f"<p>{BeautifulSoup(p, 'html.parser').get_text()}</p>" for p in parts)

    # Monta content_html final com separadores/meta/tags
    content_html = []
    if title:
        content_html.append(f"<h2>{title}</h2>")
    if p_html:
        content_html.append(p_html)
    if fonte_host:
        content_html.append(f"<p><strong>Fonte:</strong> {fonte_host}</p>")
    content_html.append("<hr/>")
    if meta_desc:
        content_html.append(f"<p><strong>Meta descrição:</strong> {meta_desc}</p>")
    content_html.append("<hr/>")
    if tags:
        content_html.append(f"<p><strong>Tags:</strong> {', '.join(tags)}</p>")

    return title or "", "\n".join(content_html), tags, meta_desc

def save_ultimo_json(payload: dict) -> str:
    """Grava ./artigos/ultimo.json"""
    path = os.path.join(OUTPUT_DIR, OUTPUT_BASENAME)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path

# =========================
# PIPELINE
# =========================

def process_url(input_url: str) -> dict:
    """
    1) baixa página
    2) extrai html limpo, título e imagem
    3) valida mínimo
    4) reescreve com IA no formato solicitado
    5) salva artigos/ultimo.json
    """
    log.info(f"[INGEST] url={input_url}")

    # 1) GET
    try:
        r = http_get(input_url, timeout=25)
        r.raise_for_status()
    except Exception as e:
        log.error(f"[INGEST] GET falhou: {e}")
        raise

    html = r.text or ""
    src_host = urlparse(r.url).netloc

    # 2) extrai
    title_guess, img = extract_title_and_image(html, r.url)
    body_html = clean_html_keep_paragraphs(html)
    plain = to_plain_text(body_html)

    # 3) mínimo
    if not ensure_minimum(body_html):
        log.warning(f"[INGEST] Conteúdo curto (p={paragraph_count(body_html)}, chars={len(plain)}); FORCE_MIN={FORCE_MIN}")
        if not FORCE_MIN:
            raise ValueError("Conteúdo insuficiente")

    # 4) IA
    prompt = build_editorial_prompt(title_guess, plain, src_host or "Fonte")
    ia_text = textsynth_generate(prompt, max_tokens=800, temperature=0.4)

    if not ia_text.strip():
        # fallback: usa texto limpo mesmo
        log.warning("[INGEST] IA vazia, usando fallback ORIGINAL LIMPO")
        # monta um conteúdo simples dentro do padrão
        fallback_title = title_guess or f"Atualização – {SITE_NAME}"
        city_tags = ["caraguatatuba", "são sebastião", "ilhabela", "ubatuba"]
        fallback_tags = city_tags[:4]
        fallback_meta = (plain[:157] + "...") if len(plain) > 160 else plain
        content_html = (
            f"<h2>{fallback_title}</h2>\n"
            f"{body_html}\n"
            f"<p><strong>Fonte:</strong> {src_host or 'Fonte'}</p>\n"
            f"<hr/>\n"
            f"<p><strong>Meta descrição:</strong> {BeautifulSoup(fallback_meta,'html.parser').get_text()}</p>\n"
            f"<hr/>\n"
            f"<p><strong>Tags:</strong> {', '.join(fallback_tags)}</p>"
        )
        final_title = fallback_title
        final_tags = fallback_tags
        final_meta = BeautifulSoup(fallback_meta,'html.parser').get_text()
    else:
        final_title, content_html, final_tags, final_meta = assemble_content_html(ia_text)

        # Se IA veio sem corpo, cai no fallback do corpo
        if not content_html or len(to_plain_text(content_html)) < 40:
            log.warning("[INGEST] IA sem corpo útil, aplicando fallback de corpo")
            if not final_title:
                final_title = title_guess or f"Atualização – {SITE_NAME}"
            content_html = (
                f"<h2>{final_title}</h2>\n{body_html}\n"
                f"<p><strong>Fonte:</strong> {src_host or 'Fonte'}</p>"
            )
            if not final_meta:
                pm = plain[:157] + "..." if len(plain) > 160 else plain
                final_meta = BeautifulSoup(pm, "html.parser").get_text()
            if not final_tags:
                final_tags = ["litoral norte", "notícia"]

    # 5) JSON final
    now = datetime.now(timezone.utc)
    payload = {
        "title": final_title,
        "content_html": content_html,
        "meta_description": final_meta,
        "tags": final_tags,
        "source": r.url,
        "image": img,
        "site": SITE_NAME,
        "created_at": format_datetime_iso(now),
        "hash": hashlib.md5((final_title + r.url).encode("utf-8")).hexdigest()
    }
    save_ultimo_json(payload)
    return payload

# =========================
# ROUTES
# =========================

@app.get("/")
def home():
    return jsonify({
        "ok": True,
        "service": "autopost-server",
        "site": SITE_NAME,
        "endpoints": {
            "health": "/health",
            "ultimo": "/artigos/ultimo.json",
            "ingest": "/ingest?url=https://exemplo.com/materia.html"
        },
        "force_min": FORCE_MIN,
        "min_chars": MIN_CHARS,
        "min_paragraphs": MIN_PARAGRAPHS,
        "resolve_wait": RESOLVE_WAIT
    })

@app.get("/health")
def health():
    path = os.path.join(OUTPUT_DIR, OUTPUT_BASENAME)
    return jsonify({
        "ok": True,
        "has_last": os.path.exists(path),
        "time": datetime.now(timezone.utc).isoformat()
    })

@app.get("/artigos/ultimo.json")
def ultimo_json():
    path = os.path.join(OUTPUT_DIR, OUTPUT_BASENAME)
    if not os.path.exists(path):
        # devolve JSON vazio padrão
        return jsonify({"ok": False, "reason": "no_content"}), 200
    # serve arquivo
    return send_from_directory(OUTPUT_DIR, OUTPUT_BASENAME, mimetype="application/json")

@app.get("/ingest")
def ingest():
    url = request.args.get("url", "").strip()
    if not url:
        abort(400, "Parâmetro ?url= é obrigatório")
    # caso venha um link do Google News, espera e tenta resolver
    parsed = urlparse(url)
    if "news.google.com" in parsed.netloc:
        log.info(f"[GNEWS] aguardando {RESOLVE_WAIT} s: {url}")
        time.sleep(max(0, RESOLVE_WAIT))
        # requests com allow_redirects já resolve o final lá no process_url
    try:
        payload = process_url(url)
        return jsonify({"ok": True, "saved": f"/artigos/{OUTPUT_BASENAME}", "data": payload})
    except Exception as e:
        log.error(f"[INGEST] erro: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

# =========================
# MAIN
# =========================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
