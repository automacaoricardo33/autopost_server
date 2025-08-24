import os, json, time, re, threading
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus
import requests
from flask import Flask, jsonify, send_from_directory, request
from apscheduler.schedulers.background import BackgroundScheduler

from scraper import (
    fetch_html,
    resolve_amp,
    extract_main_article,
    pick_image,
    clean_html_hard,
    text_only,
    make_tags
)

# ----------------------------- Config -----------------------------
PORT                = int(os.getenv("PORT", "10000"))
REQUEST_TIMEOUT     = int(os.getenv("REQUEST_TIMEOUT", "15"))
SLEEP_BEFORE_RESOLVE= float(os.getenv("SLEEP_BEFORE_RESOLVE", "3"))  # segs
KW_SINCE_HOURS      = int(os.getenv("KW_SINCE_HOURS", "12"))
KEYWORDS_ENV        = os.getenv("KEYWORDS", "litoral norte de sao paulo, ilhabela, sao sebastiao, caraguatatuba, ubatuba, futebol, formula 1, brasil")
KEYWORDS            = [k.strip() for k in KEYWORDS_ENV.split(",") if k.strip()]
TEXTSYNTH_KEY       = os.getenv("TEXTSYNTH_KEY", "").strip()

HL = "pt-BR"
GL = "BR"
CEID = "BR:pt-419"

DATA_DIR = "/mnt/data"
os.makedirs(DATA_DIR, exist_ok=True)
LAST_JSON_PATH = os.path.join(DATA_DIR, "ultimo.json")

app = Flask(__name__)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

# ------------------------ Prompt / Reescrita ------------------------
PROMPT_PERSONA = """PERSONA
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
- Deve ter entre 4 e 5 parágrafos.
- O primeiro parágrafo (lide) deve resumir a notícia de forma direta.
- O texto deve ser claro, objetivo e jornalístico.
- Deve ser original, reescrevendo a informação da fonte, não copiando.

3. Fonte
Formato: Fonte: [Nome do Veículo Original]

4. Linha Separadora
Formato: ---

5. Meta Descrição
Formato: **Meta descrição:** [até 160 caracteres]

6. Linha Separadora
Formato: ---

7. Tags
Formato: **Tags:** [5 a 10 palavras, minúsculas, separadas por vírgula; incluir cidades do Litoral Norte quando pertinente]
"""

def call_textsynth_rewrite(source_url: str, src_title: str, src_plain: str, src_site: str) -> str:
    """
    Retorna MARKDOWN no formato exigido pelo prompt.
    Se TEXTSYNTH_KEY não estiver definido, faz um rewrite simples (fallback).
    """
    content = src_plain.strip()
    if not content:
        return ""

    if TEXTSYNTH_KEY:
        # TextSynth GPT-J endpoint
        prompt = f"""{PROMPT_PERSONA}

URL da fonte: {source_url}

TÍTULO ORIGINAL:
{src_title.strip()}

TEXTO BASE (apenas para referência; reescreva com suas palavras):
{content}

GERE APENAS A SAÍDA FINAL NO FORMATO EXATO EXIGIDO (markdown).
"""
        try:
            resp = requests.post(
                "https://api.textsynth.com/v1/engines/gptj_6B/completions",
                headers={"Authorization": f"Bearer {TEXTSYNTH_KEY}"},
                json={
                    "prompt": prompt,
                    "max_tokens": 600,
                    "temperature": 0.5,
                    "stop": None
                },
                timeout=REQUEST_TIMEOUT
            )
            if resp.status_code == 200:
                j = resp.json()
                out = (j.get("text") or "").strip()
                # Pequena sanidade: garantir que comece com ### (Título)
                if not out.startswith("###"):
                    out = "### " + (src_title.strip() or "Notícia no Litoral Norte") + "\n\n" + out
                return out
        except Exception:
            pass

    # --------- Fallback: cria a saída no formato do prompt (simples) ---------
    # Quebra em 4-5 parágrafos
    txt = content
    paras = [p.strip() for p in re.split(r"\n{2,}", txt) if p.strip()]
    if len(paras) < 4:
        # tenta cortar por pontos
        sentences = re.split(r"(?<=[\.\!\?])\s+", txt)
        chunk, current, out = 4, "", []
        for s in sentences:
            if len(current) + len(s) < 350:
                current += (" " if current else "") + s
            else:
                if current:
                    out.append(current.strip())
                current = s
        if current:
            out.append(current.strip())
        paras = out[:5] if out else [txt]

    body = "\n\n".join(paras[:5])
    title_md = f"### {src_title.strip() or 'Atualização no Litoral Norte'}"
    meta_desc = (src_title[:150] + "...") if len(src_title) > 150 else src_title
    tags = make_tags(src_title + " " + src_plain)

    md = (
        f"{title_md}\n"
        f"{body}\n\n"
        f"Fonte: {src_site or 'Fonte externa'}\n\n"
        f"---\n\n"
        f"**Meta descrição:** {meta_desc}\n\n"
        f"---\n\n"
        f"**Tags:** {', '.join(tags)}\n"
    )
    return md

def md_to_html(md: str) -> str:
    # conversor bem simples (headline + parágrafos + negrito + separadores)
    h = md.strip()
    h = re.sub(r"(?m)^###\s+(.*)$", r"<h2>\1</h2>", h)
    h = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", h)
    h = h.replace("\n---\n", "\n<hr/>\n")
    # parágrafos: linhas separadas por \n\n
    parts = [p.strip() for p in re.split(r"\n{2,}", h) if p.strip()]
    html_parts = []
    for p in parts:
        if p.startswith("<h2>") and p.endswith("</h2>"):
            html_parts.append(p)
        elif p == "<hr/>" or p == "<hr/>":
            html_parts.append("<hr/>")
        else:
            html_parts.append(f"<p>{p}</p>")
    return "\n".join(html_parts)

# ------------------------- Google News helpers -------------------------
def gnews_search_feed(keyword: str) -> str:
    # usa filtro de recência when:6h para diminuir velharias
    q = quote_plus(f'{keyword} when:6h')
    return f"https://news.google.com/rss/search?q={q}&hl={HL}&gl={GL}&ceid={CEID}"

def iter_gnews_items(xml_text: str):
    # parse manual simples (sem feedparser pra diminuir deps)
    items = re.split(r"</item>\s*<item>", xml_text, flags=re.I)
    if len(items) == 1:
        items = re.findall(r"<item>.*?</item>", xml_text, flags=re.I|re.S)
    for raw in items:
        # link
        mlink = re.search(r"<link>(.+?)</link>", raw, flags=re.I|re.S)
        link = (mlink.group(1).strip() if mlink else "")
        # title
        mtitle = re.search(r"<title>(.+?)</title>", raw, flags=re.I|re.S)
        t = (mtitle.group(1) if mtitle else "")
        t = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", t, flags=re.S).strip()
        # pubDate
        mdate = re.search(r"<pubDate>(.+?)</pubDate>", raw, flags=re.I|re.S)
        pub = (mdate.group(1).strip() if mdate else "")
        yield {"title": t, "link": link, "pubDate": pub}

def resolve_gnews_link(url: str) -> str:
    # Espera curto, tenta seguir redirects do próprio Google News
