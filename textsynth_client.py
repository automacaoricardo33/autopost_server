import os, requests
from utils import render_html, sanitize_html

TEXTSYNTH_API_KEY = os.getenv('TEXTSYNTH_API_KEY', '').strip()
ENGINE = os.getenv('TEXTSYNTH_ENGINE', 'gptj_6B')
MAX_TOKENS = int(os.getenv('TEXTSYNTH_MAX_TOKENS', '900'))
TEMPERATURE = float(os.getenv('TEXTSYNTH_TEMPERATURE', '0.7'))

PROMPT_TEMPLATE = (
    'Voce e um editor-chefe do portal Voz do Litoral.\n'
    'Reescreva o texto abaixo com linguagem jornalistica, foco em SEO e hiperlocal para a regiao de {regiao}.\n'
    '- Nao inclua links/URLs no corpo.\n'
    '- Mantenha fatos verificaveis; evite adjetivos exagerados.\n'
    '- Estruture com titulo forte, subtitulos quando fizer sentido, e paragrafos curtos.\n'
    '- Tamanho alvo: 500-800 palavras.\n'
    'TITULO ORIGINAL: "{title}"\n'
    'TEXTO ORIGINAL:\n'
    '{body}\n'
)

def rewrite_with_textsynth(title: str, text: str, image_url: str, fonte: str, regiao: str):
    if not TEXTSYNTH_API_KEY:
        return None
    if not text or len(text) < 200:
        return None
    prompt = PROMPT_TEMPLATE.format(regiao=regiao, title=title, body=text)
    try:
        url = f'https://api.textsynth.com/v1/engines/{ENGINE}/completions'
        headers = {'Authorization': f'Bearer {TEXTSYNTH_API_KEY}'}
        payload = {'prompt': prompt, 'max_tokens': MAX_TOKENS, 'temperature': TEMPERATURE}
        r = requests.post(url, json=payload, headers=headers, timeout=60)
        r.raise_for_status()
        data = r.json()
        completion = (data.get('text') or (data.get('choices') or [{}])[0].get('text') or '').strip()
        if not completion:
            return None
        body_html = '<p>' + completion.replace('\n\n','</p><p>').replace('\n','<br/>') + '</p>'
        return render_html(title=title, image_url=image_url, body_html=sanitize_html(body_html), fonte=fonte, regiao=regiao)
    except Exception:
        return None
