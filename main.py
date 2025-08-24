import os
import json
import time
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

from flask import Flask, jsonify
from apscheduler.schedulers.background import BackgroundScheduler

# =========================
# CONFIG
# =========================
PORT = int(os.getenv("PORT", "10000"))
KEYWORDS = os.getenv(
    "KEYWORDS",
    "litoral norte de sao paulo, ilhabela, sao sebastiao, caraguatatuba, ubatuba, brasil, futebol, formula 1, f1",
)
GNEWS_CEID = os.getenv("GNEWS_CEID", "BR:pt-419")  # hl=pt-BR&gl=BR&ceid=BR:pt-419
MAX_PER_RUN = int(os.getenv("MAX_PER_RUN", "8"))
RESOLVE_WAIT_SECONDS = int(os.getenv("RESOLVE_WAIT_SECONDS", "3"))  # rápido
TIMEOUT_SECONDS = int(os.getenv("TIMEOUT_SECONDS", "20"))
RECENCY_HOURS = int(os.getenv("RECENCY_HOURS", "24"))
TEXTSYNTH_KEY = os.getenv("TEXTSYNTH_KEY", "").strip()

OUT_DIR = os.path.join(os.getcwd(), "artigos")
OUT_JSON = os.path.join(OUT_DIR, "ultimo.json")

MIN_CHARS = int(os.getenv("MIN_CHARS", "450"))
MIN_P_COUNT = int(os.getenv("MIN_P_COUNT", "3"))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.7,en;q=0.6",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

PROMPT_JORNAL = """PERSONA
Você é um jornalista digital e especialista em SEO, responsável por redigir notícias para o portal "Voz do Litoral". Seu público são os moradores das cidades de Caraguatatuba, São Sebastião, Ilhabela e Ubatuba. Seu objetivo é pegar uma notícia de uma fonte externa e reescrevê-la de forma clara, objetiva e otimizada, sempre conectando o assunto à realidade e ao interesse local.

TAREFA PRINCIPAL
Sua tarefa é receber uma URL de uma notícia e transformá-la em um texto otimizado para SEO, seguindo RIGOROSAMENTE a estrutura de saída abaixo. Você deve extrair a informação principal do link e reescrevê-la com suas próprias palavras, no tom e estilo do portal.

ESTRUTURA DE SAÍDA (OBRIGATÓRIA)
Você deve gerar a resposta exatamente neste formato, sem adicionar ou remover nenhum elemento.

1. Título (Headline)
Formato: ### [Seu Título Otimizado]

Requisitos: Deve ser informativo, cativante e conter as principais palavras-chave. Sempre que possível, deve contextualizar a notícia para o Litoral Norte.

2. Corpo do Texto
Formato: Parágrafos de texto simples.

Requisitos:

Deve ter entre 4 e 5 parágrafos.

O primeiro parágrafo (lide) deve resumir a notícia de forma direta.

O texto deve ser claro, objetivo e jornalístico.

Deve ser original, reescrevendo a informação da fonte, não copiando.

3. Fonte
Formato: Fonte: [Nome do Veículo Original]

Requisitos: Cite o nome do portal de onde a notícia foi extraída (ex: G1, CNN Brasil, Radar Litoral).

4. Linha Separadora
Formato: ---

5. Meta Descrição
Formato: **Meta descrição:** seguido do texto.

Requisitos: Um resumo curto e atraente (máximo de 160 caracteres) para os buscadores (Google). Deve conter as palavras-chave mais importantes.

6. Linha Separadora
Formato: ---

7. Tags
Formato: **Tags:** seguido das palavras, separadas por vírgula.

Requisitos: Uma lista de 5 a 10 palavras-chave relevantes, em letras minúsculas, separadas por vírgula. Inclua nomes de cidades do Litoral Norte sempre que pertinente.
"""

app = Flask(__name__)
_last_article = None
_last_run_info = {"ok": True, "published": 0, "ts": None, "msg": ""}

@app.route("/")
def root():
    return "OK - Autopost server"

@app.route("/health")
def health():
    return jsonify({"ok": True, "time": datetime.now(timezone.utc).isoformat()})

@app.route("/artigos/ultimo.json")
def artigos_ultimo_json():
    global _last_article
    if _last_article is None and os.path.exists(OUT_JSON):
        try:
            with open(OUT_JSON, "r", encoding="utf-8") as f:
                _last_article = json.load(f)
        except Exception:
            pass
    if _last_article is None:
        return jsonify({"ok": True, "has_last": False})
    return jsonify({"ok": True, "has_last": True, **_last_article})

@app.route("/debug/config")
def debug_config():
    return jsonify({
        "KEYWORDS": KEYWORDS,
        "GNEWS_CEID": GNEWS_CEID,
        "MAX_PER_RUN": MAX_PER_RUN,
        "RESOLVE_WAIT_SECONDS": RESOLVE_WAIT_SECONDS,
        "TIMEOUT_SECONDS": TIMEOUT_SECONDS,
        "RECENCY_HOURS": RECENCY_HOURS,
        "MIN_CHARS": MIN_CHARS,
        "MIN_P_COUNT": MIN_P_COUNT,
        "TEXTSYNTH_ON": bool(TEXTSYNTH_KEY),
    })

@app.route("/debug/run")
def debug_run():
    job_run()
    global _last_article, _last_run_info
    return jsonify({
        "run": _last_run_info,
        "last": _last_article if _last_article else None
    })

def http_get(url, timeout=TIMEOUT_SECONDS, allow_redirects=True):
    r = requests.get(url, timeout=timeout, headers=HEADERS, allow_redirects=allow_redirects)
    r.raise_for_status()
    return r

def fetch_feed_for_keyword(kw: str):
    base = "https://news.google.com/rss/search"
    q = {"q": kw, "hl": "pt-BR", "gl": "BR", "ceid": GNEWS_CEID}
    url = f"{base}?{urlencode(q)}"
    r = http_get(url)
    soup = BeautifulSoup(r.content, "xml")
    items = soup.select("item")
    results = []
    for it in items:
        title = it.title.text.strip() if it.title else ""
        link = it.link.text.strip() if it.link else ""
        pubdate = it.pubDate.text.strip() if it.pubDate else ""
        try:
            dt = dateparser.parse(pubdate)
        except Exception:
            dt = None
        results.append({"title": title, "link": link, "pubdate": dt})
    return results

def is_recent(dt):
    if not dt:
        return False
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (now - dt) <= timedelta(hours=RECENCY_HOURS)

def wait_then_resolve_gnews(url: str) -> str:
    w = max(0, RESOLVE_WAIT_SECONDS)
    if w:
        print(f"[GNEWS] aguardando {w}s: {url}", flush=True)
        time.sleep(w)
    try:
        r = http_get(url, allow_redirects=True)
        return r.url
    except Exception as e:
        print(f"[GNEWS] erro ao resolver; fallback -> {url} ({e})", flush=True)
        return url

def extract_og_image(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for sel, attr in [
        ('meta[property="og:image"]', "content"),
        ('meta[name="twitter:image"]', "content"),
        ('link[rel="image_src"]', "href"),
    ]:
        m = soup.select_one(sel)
        if m and m.get(attr):
            return m.get(attr).strip()
    img = soup.find("img")
    if img and img.get("src"):
        return img["src"].strip()
    return ""

def extract_title_text(html: str):
    soup = BeautifulSoup(html, "html.parser")
    title = ""
    if soup.title and soup.title.text:
        title = soup.title.text.strip()

    best = None
    best_score = 0
    for node in soup.find_all(["article", "main", "div", "section"]):
        ps = node.find_all("p")
        text = " ".join(p.get_text(" ", strip=True) for p in ps)
        score = len(ps) * 10 + len(text)
        if score > best_score:
            best = text
            best_score = score

    if not best:
        best = soup.get_text(" ", strip=True)
    best = re.sub(r"\s+", " ", best).strip()

    sentences = [s.strip() for s in re.split(r"(?<=\.)\s+", best) if s.strip()]
    buf = []
    current = ""
    for s in sentences:
        if len(current) + len(s) < 400:
            current = (current + " " + s).strip()
        else:
            if current:
                buf.append(current)
            current = s
    if current:
        buf.append(current)

    if len(buf) < 3:
        ps = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
        ps = [p for p in ps if len(p) > 40]
        if ps:
            buf = ps[:5]

    return title, "\n\n".join(buf[:6])

def host_from_url(u: str) -> str:
    try:
        return re.sub(r"^www\.", "", requests.utils.urlparse(u).netloc)
    except Exception:
        return ""

def generate_with_textsynth(source_url: str, vehicle: str, src_title: str, clean_text: str):
    if not TEXTSYNTH_KEY:
        return None
    prompt = f"""{PROMPT_JORNAL}

URL de origem: {source_url}

TÍTULO DA FONTE:
{src_title}

TEXTO DA FONTE (LIMPO):
{clean_text}

GERE A SAÍDA NESTE FORMATO EXATO:

### [Seu Título Otimizado]
[Parágrafo 1]
[Parágrafo 2]
[Parágrafo 3]
[Parágrafo 4]
[Parágrafo 5 opcional]

Fonte: {vehicle}
---
**Meta descrição:** [até 160 caracteres]
---
**Tags:** [5 a 10 termos em minúsculas, separados por vírgula; incluir cidades se fizer sentido]
"""
    url = "https://api.textsynth.com/v1/engines/gptj_6B/completions"
    headers = {"Authorization": f"Bearer {TEXTSYNTH_KEY}", "Content-Type": "application/json"}
    body = {"prompt": prompt, "max_tokens": 700, "temperature": 0.5, "top_p": 0.95}
    try:
        r = requests.post(url, headers=headers, json=body, timeout=TIMEOUT_SECONDS)
        if 200 <= r.status_code < 300:
            return r.json().get("text", "").strip()
        else:
            print(f"[AI] TextSynth {r.status_code}: {r.text[:300]}", flush=True)
    except Exception as e:
        print(f"[AI] erro TextSynth: {e}", flush=True)
    return None

def build_prompt_output(vehicle: str, title: str, text: str, cities_hint=True):
    ps = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    if len(ps) < 4:
        sentences = [s.strip() for s in re.split(r"(?<=\.)\s+", text) if s.strip()]
        ps = []
        buf = []
        for s in sentences:
            buf.append(s)
            if len(" ".join(buf)) > 220:
                ps.append(" ".join(buf))
                buf = []
        if buf:
            ps.append(" ".join(buf))
    ps = ps[:5]
    if len(ps) < 4 and text:
        ps = ps + [text][:4 - len(ps)]

    title_clean = re.sub(r"\s+", " ", title or "Atualização").strip()
    if len(title_clean) > 120:
        title_clean = title_clean[:117] + "..."

    meta = re.sub(r"\s+", " ", " ".join(ps))[:160]

    base = (title_clean + " " + " ".join(ps)).lower()
    words = re.findall(r"[a-zà-úãõâêîôûç0-9\-]{3,}", base, flags=re.IGNORECASE)
    stop = set("""
a o os as um uma uns umas de do da dos das em no na nos nas para por com sem sob sobre entre e ou que se sua seu suas seus
ao à às aos até como mais menos muito muita muitos muitas já não sim foi será ser está estão era são pelo pela pelos pelas
lhe eles elas dia ano anos hoje ontem amanhã the and of to in on for with from
caraguatatuba são sebastião ilhabela ubatuba litoral norte brasil
""".split())
    freq = {}
    for w in words:
        wl = w.lower()
        if wl not in stop and not wl.isdigit():
            freq[wl] = freq.get(wl, 0) + 1
    tags = sorted(freq.keys(), key=lambda k: -freq[k])[:7]
    if cities_hint:
        for c in ["caraguatatuba", "são sebastião", "ilhabela", "ubatuba", "litoral norte"]:
            if c not in tags and c in base and len(tags) < 10:
                tags.append(c)

    out_lines = []
    out_lines.append(f"### {title_clean}")
    for p in ps[:5]:
        out_lines.append(p)
    out_lines.append(f"\nFonte: {vehicle}\n")
    out_lines.append("---")
    out_lines.append(f"**Meta descrição:** {meta.strip()}")
    out_lines.append("---")
    out_lines.append("**Tags:** " + ", ".join(tags))
    return "\n\n".join(out_lines)

def make_article_object(source_url: str, vehicle: str, final_render: str, image_url: str, published_iso: str):
    return {
        "url": source_url,
        "vehicle": vehicle,
        "render_markdown": final_render,
        "image": image_url,
        "published_at": published_iso,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

def write_last_article(obj: dict):
    global _last_article
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    _last_article = obj

def job_run():
    global _last_run_info
    published = 0
    msgs = []
    try:
        keywords = [k.strip() for k in KEYWORDS.split(",") if k.strip()]
        if not keywords:
            msgs.append("Sem KEYWORDS.")
            _last_run_info = {"ok": True, "published": 0, "ts": datetime.now(timezone.utc).isoformat(), "msg": "; ".join(msgs)}
            return

        for kw in keywords:
            if published >= MAX_PER_RUN:
                break

            items = fetch_feed_for_keyword(kw)
            if not items:
                msgs.append(f"[{kw}] sem itens feed")
                continue

            recent_items = [it for it in items if is_recent(it["pubdate"])]
            if not recent_items:
                msgs.append(f"[{kw}] sem itens RECENTES")
                continue

            for it in recent_items:
                if published >= MAX_PER_RUN:
                    break

                gnews_link = it["link"]
                pub_dt = it["pubdate"] or datetime.now(timezone.utc)
                pub_iso = pub_dt.astimezone(timezone.utc).isoformat()

                final_url = wait_then_resolve_gnews(gnews_link)

                try:
                    r = http_get(final_url, timeout=TIMEOUT_SECONDS, allow_redirects=True)
                except Exception as e:
                    msgs.append(f"[{kw}] GET falhou final; tentando gnews ({e})")
                    try:
                        r = http_get(gnews_link, timeout=TIMEOUT_SECONDS, allow_redirects=True)
                        final_url = r.url
                    except Exception as e2:
                        msgs.append(f"[{kw}] GET falhou gnews ({e2})")
                        continue

                html = r.text or ""
                if len(html) < 500:
                    msgs.append(f"[{kw}] HTML curto: {final_url}")
                    continue

                img = extract_og_image(html)
                src_title, clean_text = extract_title_text(html)

                if len(clean_text) < MIN_CHARS or clean_text.count(".") < MIN_P_COUNT:
                    msgs.append(f"[{kw}] Conteúdo insuficiente.")
                    continue

                vehicle = host_from_url(final_url) or "Fonte original"

                rendered = generate_with_textsynth(final_url, vehicle, src_title, clean_text)
                if not rendered or "###" not in rendered:
                    rendered = build_prompt_output(vehicle, src_title or it["title"] or "Atualização", clean_text)

                obj = make_article_object(final_url, vehicle, rendered, img, pub_iso)
                write_last_article(obj)
                published += 1
                msgs.append(f"[OK:{kw}] {src_title[:80]}")

        _last_run_info = {
            "ok": True,
            "published": published,
            "ts": datetime.now(timezone.utc).isoformat(),
            "msg": " | ".join(msgs) if msgs else "done",
        }

        if published == 0 and not msgs:
            _last_run_info["msg"] = "Nenhuma keyword RECENTE com texto suficiente."

    except Exception as e:
        _last_run_info = {
            "ok": False,
            "published": published,
            "ts": datetime.now(timezone.utc).isoformat(),
            "msg": f"Erro fatal: {e}",
        }

scheduler = BackgroundScheduler(timezone="UTC")
scheduler.add_job(job_run, "interval", minutes=5, id="job_run")
scheduler.start()

try:
    job_run()
except Exception as e:
    print(f"[BOOT] job_run falhou: {e}", flush=True)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
