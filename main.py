import os, json, time, signal, hashlib
from pathlib import Path
from flask import Flask, Response, jsonify, Blueprint, request
from apscheduler.schedulers.background import BackgroundScheduler
from scraper import get_fresh_article_candidates, fetch_and_extract
from textsynth_client import rewrite_with_textsynth
from utils import ensure_dirs, render_html, sanitize_html, now_iso, pick_first_valid_image

# --- Paths & files
BASE = Path(__file__).parent.resolve()
PUBLIC_DIR = BASE / 'public'
DATA_DIR = BASE / 'data'
ART_DIR = PUBLIC_DIR / 'artigos'
HIST_PATH = DATA_DIR / 'historico.json'
LAST_HTML = ART_DIR / 'ultimo.html'

# --- Config (env)
REGIAO = os.getenv('REGIAO', 'Litoral Norte de Sao Paulo')
RUN_INTERVAL_MIN = int(os.getenv('RUN_INTERVAL_MIN', '15'))
# tempo de “descanso” entre candidatos (antibloqueio)
FETCH_WAIT_SECONDS = int(os.getenv('FETCH_WAIT_SECONDS', '8'))
MAX_PER_RUN = int(os.getenv('MAX_PER_RUN', '1'))
TIMEOUT_SECONDS = int(os.getenv('TIMEOUT_SECONDS', '60'))

app = Flask(__name__)
scheduler = BackgroundScheduler()

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
            title_hint = cand.get('title', 'Noticia')
            app.logger.info(f'[JOB] Abrindo: {url}')

            # pequena espera geral para reduzir bloqueios
            time.sleep(FETCH_WAIT_SECONDS)

            raw = fetch_and_extract(url, timeout=TIMEOUT_SECONDS)
            if not raw or (not raw.get('text') and not raw.get('html')):
                app.logger.warning(f'[JOB] Conteudo vazio: {url}')
                continue

            app.logger.info(f"[JOB] Resolvido OK: {url}")

            effective_title = (raw.get('title') or '').strip() or title_hint
            if not mark_seen(url, effective_title):
                app.logger.info(f'[JOB] Ignorado (duplicado): {url}')
                continue

            image_url = pick_first_valid_image([raw.get('image')])
            original_html = render_html(
                title=effective_title,
                image_url=image_url,
                body_html=sanitize_html(raw.get('html') or ''),
                fonte=url,
                regiao=REGIAO
            )

            rewritten_html = rewrite_with_textsynth(
                title=effective_title,
                text=raw.get('text') or '',
                image_url=image_url,
                fonte=url,
                regiao=REGIAO
            ) or original_html

            LAST_HTML.write_text(rewritten_html, encoding='utf-8')
            generated += 1
            app.logger.info(f'[JOB] Gerado com sucesso: {url}')

        except Exception as e:
            app.logger.exception(f'[JOB] Falha com {cand}: {e}')
            continue

    app.logger.info(f'[JOB] Execucao finalizada. Novos artigos: {generated}')

# --- Static blueprint para servir /artigos/ultimo.html
bp = Blueprint('static_public', __name__, static_folder=str(PUBLIC_DIR), static_url_path='')

@bp.route('/artigos/ultimo.html', methods=['GET'])
def serve_last():
    p = LAST_HTML
    if p.exists():
        return Response(p.read_text(encoding='utf-8'), mimetype='text/html; charset=utf-8')
    return Response('<h1>Ainda sem conteudo</h1>', mimetype='text/html; charset=utf-8')

app.register_blueprint(bp)

# --- Health
@app.route('/health', methods=['GET'])
def health():
    return jsonify({'ok': True, 'time': now_iso(), 'has_last': LAST_HTML.exists()})

# --- Execução manual 1 ciclo
@app.route('/run-once', methods=['POST'])
def run_once():
    job_run()
    return jsonify({'status': 'executed'})

# --- Debug opcional: ver tamanho extraído
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

# --- Página inicial com links úteis
@app.route('/', methods=['GET'])
def index():
    return Response(
        '<h1>Autopost Server</h1>'
        '<ul>'
        '<li><a href="/health">/health</a></li>'
        '<li><a href="/artigos/ultimo.html">/artigos/ultimo.html</a></li>'
        '<li><a href="/debug/fetch?u=https://news.google.com/rss/articles/...">/debug/fetch</a></li>'
        '</ul>'
        '<p>Para rodar 1 ciclo: <code>POST /run-once</code></p>',
        mimetype='text/html; charset=utf-8'
    )

# --- Shutdown signals
def shutdown_handler(signum, frame):
    try:
        scheduler.shutdown(wait=False)
    except Exception:
        pass
    raise SystemExit(0)

if __name__ == '__main__':
    ensure_dirs([PUBLIC_DIR, DATA_DIR, ART_DIR])
    # Agenda o job
    scheduler.add_job(job_run, 'interval', minutes=max(5, min(RUN_INTERVAL_MIN, 100)),
                      id='runner', replace_existing=True)
    scheduler.start()
    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)
    port = int(os.getenv('PORT', '10000'))
    app.run(host='0.0.0.0', port=port)
