import re
from pathlib import Path
from datetime import datetime, timezone

def ensure_dirs(paths):
    for p in paths:
        Path(p).mkdir(parents=True, exist_ok=True)

def now_iso():
    return datetime.now(tz=timezone.utc).isoformat()

def sanitize_html(html: str) -> str:
    html = re.sub(r'(\x00|\x1F)', '', html)
    html = re.sub(r'on\w+=\"[^\"]*\"', '', html)
    return html

def pick_first_valid_image(candidates):
    for c in candidates:
        if c and isinstance(c, str) and c.startswith(('http://','https://')):
            return c
    return ''

def render_html(title: str, image_url: str, body_html: str, fonte: str, regiao: str) -> str:
    img_tag = (f'<img src="{image_url}" alt="" style="width:100%;height:auto;display:block;margin:1rem 0;border-radius:8px;"/>' if image_url else '')
    return (
        '<!DOCTYPE html>'
        '<html lang="pt-BR">'
        '<head>'
        '<meta charset="utf-8"/>'
        '<meta name="viewport" content="width=device-width, initial-scale=1"/>'
        f'<title>{title}</title>'
        f'<meta name="description" content="Noticias do {regiao} — Voz do Litoral"/>'
        '</head>'
        '<body>'
        '<article style="max-width: 860px; margin: 0 auto; font-family: Arial, sans-serif; line-height: 1.6;">'
        f'<h1 style="font-size: 2rem; margin: 1rem 0;">{title}</h1>'
        f'{img_tag}'
        f'<div class="conteudo">{body_html}</div>'
        '<hr style="margin:2rem 0;"/>'
        f'<p style="font-size:0.9rem;color:#555;">Fonte: {fonte}</p>'
        f'<p style="font-size:0.9rem;color:#555;">Cobertura: {regiao}</p>'
        '</article>'
        '</body>'
        '</html>'
    )
