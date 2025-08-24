# main.py
import os, json, time, signal, hashlib, re
from pathlib import Path
from flask import Flask, Response, jsonify, Blueprint, request
from apscheduler.schedulers.background import BackgroundScheduler

from scraper import get_fresh_article_candidates, fetch_and_extract
from textsynth_client import rewrite_with_textsynth
from utils import ensure_dirs, now_iso, pick_first_valid_image

# =========================
# PASTAS/ARQUIVOS
# =========================
BASE = Path(__file__).parent.resolve()
PUBLIC_DIR = BASE / 'public'
DATA_DIR = BASE / 'data'
ART_DIR = PUBLIC_DIR / 'artigos'
HIST_PATH = DATA_DIR / 'historico.json'
LAST_HTML = ART_DIR / 'ultimo.html'
LAST_JSON = ART_DIR / 'ultimo.json'

# =========================
# CONFIG VIA ENV
# =========================
REGIAO = os.getenv('REGIAO', 'Litoral Norte de Sao Paulo')
RUN_INTERVAL_MIN = int(os.getenv('RUN_INTERVAL_MIN', '15'))
FETCH_WAIT_SECONDS = int(os.getenv('FETCH_WAIT_SECONDS', '8'))
MAX_PER_RUN = int(os.getenv('MAX_PER_RUN', '1'))
TIMEOUT_SECONDS = int(os.getenv('TIMEOUT_SECONDS', '60'))

# IDs de categoria (confirmados pelo usuário)
CAT_PADRAO = int(os.getenv('CAT_PADRAO', '1'))
CAT_ILHABELA = int(os.getenv('CAT_ILHABELA', '117'))
CAT_SAO_SEBASTIAO = int(os.getenv('CAT_SAO_SEBASTIAO', '118'))
CAT_CARAGUATATUBA = int(os.getenv('CAT_CARAGUATATUBA', '116'))
CAT_UBATUBA = int(os.getenv('CAT_UBATUBA', '119'))
CAT_BRASIL = int(os.getenv('CAT_BRASIL', '2505'))
CAT_MUNDO = int(os.getenv('CAT_MUNDO', '2506'))

app = Flask(__name__)
scheduler = BackgroundScheduler()

# =========================
# UTIL: histórico simples
# =========================
def load_hist():
    if HIST_PATH.exists():
        try:
            return json.loads(HIST_PATH.read_text(encoding='utf-8'))
        except Exception:
            return {'seen': []}
    return {'seen': []}

def save_hist(h):
    HIST_PATH.write_text(json.dumps(h, ensure_ascii=False, indent=2), encoding='utf-8')

def mark_seen(url, title):
    h = load_hist()
    digest = hashlib.sha256(f"{url}::{title}".encode('utf-8')).hexdigest()
    if digest not in h['seen']:
        h['seen'].append(digest)
        if len(h['seen']) > 5000:
            h['seen'] = h['seen'][-2000:]
        save_hist(h)
        return True
    return False

# =========================
# LIMPEZA / TAGS / META
# =========================
STOPWORDS = set("""
a o os as um uma uns umas de do da dos das em no na nos nas para por com sem sob sobre entre e ou que se sua seu suas seus ao à às aos até como mais menos muito muita muitos muitas já não sim foi será ser está estão era são pelo pela pelos pelas lhe eles elas dia ano anos hoje ontem amanhã the and of to in on for with from
""".strip().split())

def plain_text_from_html(html: str) -> str:
    # remove tags rápidos
    if not html:
        return ""
    txt = re.sub(r'<[^>]+>', ' ', html)
    txt = re.sub(r'\s+', ' ', txt).strip()
    return txt

def gen_meta_description(text: str, limit=160) -> str:
    t = (text or '').strip()
    if not t:
        return ''
    t = re.sub(r'\s+', ' ', t)
    if len(t) <= limit:
        return t
    # corta na última palavra
    cut = t[:limit]
    if ' ' in cut:
        cut = cut[:cut.rfind(' ')]
    return cut

def gen_tags(text: str, title: str, max_tags=12) -> list:
    base = f"{title or ''}. {text or ''}".lower()
    base = re.sub(r'[^a-z0-9á-úà-ùâ-ûã-õç\s\-]', ' ', base, flags=re.I)
    words = [w for w in re.split(r'\s+', base) if len(w) >= 3 and w not in STOPWORDS and not w.isdigit()]
    freq = {}
    for w in words:
        freq[w] = freq.get(w, 0) + 1
    # privilegia palavras do título
    for w in (title or '').lower().split():
        if len(w) >= 3 and w not in STOPWORDS:
            freq[w] = freq.get(w, 0) + 2
    ranked = sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))
    return [w for w,_ in ranked[:max_tags]]

def pick_category(title: str, text: str, source_url: str) -> int:
    blob = f"{title} {text}".lower()
    # cidades
    if any(k in blob for k in ['ilhabela']):
        return CAT_ILHABELA
    if any(k in blob for k in ['são sebastião','sao sebastiao']):
        return CAT_SAO_SEBASTIAO
    if 'caraguatatuba' in blob or 'caraguá' in blob or 'caraguá ' in blob:
        return CAT_CARAGUATATUBA
    if 'ubatuba' in blob:
        return CAT_UBATUBA
    # país (heurística simples pelo domínio)
    try:
        from urllib.parse import urlparse
        host = urlparse(source_url).netloc.lower()
        if host.endswith('.br'):
            return CAT_BRASIL
        # Se mencionar Brasil/Belém/SP/RJ etc, também trata como Brasil
        if any(k in blob for k in ['brasil','sp ','são paulo','sao paulo','brasileiro','belem','belém','rio de janeiro','br']):
            return CAT_BRASIL
        return CAT_MUNDO
    except Exception:
        return CAT_PADRAO or CAT_BRASIL

def render_full_html(payload: dict) -> str:
    """
    Gera o HTML final que o WP pode consumir (igual ao modelo: título, imagem no topo, corpo e fonte).
    """
    title = payload.get('title') or 'Notícia'
    image = payload.get('image') or ''
    body  = payload.get('content_html') or ''
    fonte = payload.get('source') or ''
    meta  = payload.get('meta_description') or ''
    # bloco simples
    img_block = f'<figure style="margin:0 0 16px 0;text-align:center;"><img src="{image}" alt="{title}" style="max-width:100%;height:auto;" loading="lazy" /></figure>' if image else ''
    fonte_block = f'<p><strong>Fonte:</strong> <a href="{fonte}" target="_blank" rel="nofollow noopener">link original</a></p>' if fonte else ''
    head_meta = f'<meta name="description" content="{meta}"/>' if meta else ''
    return f"""<!doctype html>
<html lang="pt-br">
<head>
<meta charset="utf-8"/>{head_meta}
<title>{title}</title>
</head>
<body>
<main>
<h1>{title}</h1>
{img_block}
{body}
{fonte_block}
</main>
</body>
</html>
"""

# =========================
# JOB
# =========================
def job_run():
    app.logger.info('[JOB] Iniciando execucao automatica...')
    ensure_dirs([PUBLIC_DIR, DATA_DIR, ART_DIR])

    candidates = get_fresh_article_candidates()
    app.logger.info(f'[JOB] Candidatos encontrados: {len(candidates)}')

    generated = 0
    for cand in candidates:
        if generated >= MAX_PER_RUN:
            break
        try:
            url = cand['url']
            title_hint = cand.get('title', 'Notícia')
            app.logger.info(f'[JOB] Abrindo: {url}')

            # pequena espera geral para reduzir bloqueios
            time.sleep(FETCH_WAIT_SECONDS)

            raw = fetch_and_extract(url, timeout=TIMEOUT_SECONDS)
            if not raw or (not raw.get('text') and not raw.get('html')):
                app.logger.warning(f'[JOB] Conteudo vazio: {url}')
                continue

            effective_title = (raw.get('title') or '').strip() or title_hint
            if not mark_seen(url, effective_title):
                app.logger.info(f'[JOB] Ignorado (duplicado): {url}')
                continue

            # dados base
            image_url = pick_first_valid_image([raw.get('image')])
            original_text = raw.get('text') or ''
            original_html = raw.get('html') or ''

            # IA (TextSynth) – reescrita
            rewritten_html = rewrite_with_textsynth(
                title=effective_title,
                text=original_text,
                image_url=image_url,
                fonte=url,
                regiao=REGIAO
            ) or original_html

            # campos SEO
            clean_plain = plain_text_from_html(rewritten_html) or plain_text_from_html(original_html)
            meta_desc = gen_meta_description(clean_plain, limit=160)
            tags_list = gen_tags(clean_plain, effective_title, max_tags=12)
            cat_id = pick_category(effective_title, clean_plain, url)

            # pacote JSON completo para o WP
            payload = {
                "title": effective_title,
                "content_html": rewritten_html,
                "meta_description": meta_desc,
                "tags_csv": ", ".join(tags_list),
                "tags": tags_list,             # se preferir array
                "category": cat_id,
                "image": image_url,
                "source": url,
                "regiao": REGIAO,
                "generated_at": now_iso()
            }

            # salva JSON e HTML
            LAST_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
            LAST_HTML.write_text(render_full_html(payload), encoding='utf-8')

            generated += 1
            app.logger.info(f'[JOB] Gerado com sucesso: {url}')

        except Exception as e:
            app.logger.exception(f'[JOB] Falha com {cand}: {e}')
            continue

    app.logger.info(f'[JOB] Execucao finalizada. Novos artigos: {generated}')

# =========================
# ROTAS
# =========================
bp = Blueprint('static_public', __name__, static_folder=str(PUBLIC_DIR), static_url_path='')

@bp.route('/artigos/ultimo.html', methods=['GET'])
def serve_last_html():
    p = LAST_HTML
    if p.exists():
        return Response(p.read_text(encoding='utf-8'), mimetype='text/html; charset=utf-8')
    return Response('<h1>Ainda sem conteudo</h1>', mimetype='text/html; charset=utf-8')

@bp.route('/artigos/ultimo.json', methods=['GET'])
def serve_last_json():
    p = LAST_JSON
    if p.exists():
        return Response(p.read_text(encoding='utf-8'), mimetype='application/json; charset=utf-8')
    return jsonify({"ok": False, "error": "Ainda sem conteudo"})

app.register_blueprint(bp)

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'ok': True, 'time': now_iso(), 'has_last_html': LAST_HTML.exists(), 'has_last_json': LAST_JSON.exists()})

@app.route('/run-once', methods=['POST'])
def run_once():
    job_run()
    return jsonify({'status': 'executed'})

@app.route('/debug/fetch', methods=['GET'])
def debug_fetch():
    u = request.args.get('u','').strip()
    if not u:
        return jsonify({'ok': False, 'error': 'missing u'})
    try:
        raw = fetch_and_extract(u, timeout=TIMEOUT_SECONDS)
        return jsonify({
            'ok': True,
            'title': raw.get('title'),
            'image': raw.get('image'),
            'text_len': len(raw.get('text') or ''),
            'html_len': len(raw.get('html') or ''),
        })
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})

@app.route('/', methods=['GET'])
def index():
    return Response(
        '<h1>Autopost Server</h1>'
        '<ul>'
        '<li><a href="/health">/health</a></li>'
        '<li><a href="/artigos/ultimo.html">/artigos/ultimo.html</a></li>'
        '<li><a href="/artigos/ultimo.json">/artigos/ultimo.json</a></li>'
        '<li><a href="/debug/fetch?u=https://news.google.com/rss/articles/...">/debug/fetch</a></li>'
        '</ul>'
        '<p>Para rodar 1 ciclo: <code>POST /run-once</code></p>',
        mimetype='text/html; charset=utf-8'
    )

# =========================
# SHUTDOWN / BOOT
# =========================
def shutdown_handler(signum, frame):
    try:
        scheduler.shutdown(wait=False)
    except Exception:
        pass
    raise SystemExit(0)

if __name__ == '__main__':
    ensure_dirs([PUBLIC_DIR, DATA_DIR, ART_DIR])
    scheduler.add_job(job_run, 'interval', minutes=max(5, min(RUN_INTERVAL_MIN, 100)),
                      id='runner', replace_existing=True)
    scheduler.start()
    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)
    port = int(os.getenv('PORT', '10000'))
    app.run(host='0.0.0.0', port=port)
