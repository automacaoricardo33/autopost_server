import requests
from typing import Dict, Tuple

# ================== EDITE AQUI ==================
WP_ENDPOINT_URL = "https://jornalvozdolitoral.com/wp-content/rs-auto-publisher.php"  # URL do seu PHP
WP_API_KEY = "3b62b8216593f8593397ed2debb074fc"  # chave que você colou no PHP
# ================================================

TIMEOUT = 25
UA = {"User-Agent": "RS-AutoPublisher/1.0"}

def send_to_wordpress(post: Dict) -> Tuple[bool, str, str | None]:
    """
    Envia para o seu endpoint PHP (RS Auto Publisher).
    Campos enviados:
      - key (string)                -> obrigatória
      - title (string)              -> título
      - content_html (string)       -> HTML completo do corpo
      - image_url (string)          -> URL da imagem destacada
      - source_url (string)         -> URL de origem
      - excerpt (string)            -> resumo
      - category (string)           -> nome da categoria
      - tags (list[str] ou string)  -> tags separadas por vírgula
    O PHP deve:
      - validar a key
      - baixar a image_url (opcional) e setar como imagem destacada
      - criar post no WP com status 'publish'
    """
    try:
        data = {
            "key": WP_API_KEY,
            "title": post.get("title", "").strip(),
            "content_html": post.get("content_html", ""),
            "image_url": post.get("image_url", ""),
            "source_url": post.get("source_url", ""),
            "excerpt": post.get("excerpt", ""),
            "category": post.get("category", ""),
            "tags": ",".join(post.get("tags", [])) if isinstance(post.get("tags"), list) else (post.get("tags") or ""),
        }

        resp = requests.post(WP_ENDPOINT_URL, data=data, timeout=TIMEOUT, headers=UA)
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code} - {resp.text[:300]}", None

        # espera JSON { ok: true, post_url: "...", id: 123 } OU texto "OK"
        try:
            j = resp.json()
            if j.get("ok") is True:
                return True, "OK", j.get("post_url") or None
            # se vier ok em string etc.
            if str(j).lower().find("ok") >= 0:
                return True, "OK", j.get("post_url") or None
            return False, f"Retorno inesperado: {j}", None
        except Exception:
            txt = (resp.text or "").strip()
            if txt.upper().startswith("OK"):
                return True, "OK", None
            return False, f"Texto inesperado: {txt[:300]}", None

    except Exception as e:
        return False, f"Exceção enviando ao WP: {e}", None
