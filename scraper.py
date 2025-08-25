# --- ADD: extras para volume -------------------------------------------------
import hashlib
import json
import os
import re
from urllib.parse import urlparse
from html import unescape

try:
    import requests  # opcional; se faltar, o código ignora o fetch da página
except Exception:
    requests = None


TOPSTORIES_URL = f"{GNEWS_BASE}/rss?hl=pt-BR&gl=BR&ceid=BR:pt-419"


def safe_get(url: str, timeout: int = 10) -> str:
    """GET simples com timeout. Se requests não estiver instalado, retorna ''. """
    if not requests:
        return ""
    try:
        r = requests.get(url, timeout=timeout, headers={
            "User-Agent": "Mozilla/5.0 (compatible; Bot/1.0)"
        })
        if r.status_code == 200:
            return r.text or ""
    except Exception:
        pass
    return ""


def extract_meta_description(html: str) -> str:
    """Tenta extrair <meta name='description'> ou og:description e limpar."""
    if not html:
        return ""
    html = html.replace("\r", " ")
    # og:description
    m = re.search(r'<meta[^>]*property=["\']og:description["\'][^>]*content=["\'](.*?)["\']', html, flags=re.I)
    if not m:
        # name=description
        m = re.search(r'<meta[^>]*name=["\']description["\'][^>]*content=["\'](.*?)["\']', html, flags=re.I)
    if m:
        desc = unescape(m.group(1))
        desc = re.sub(r"\s+", " ", desc).strip()
        return desc
    return ""


def synthesize_summary_from_page(link: str) -> str:
    """Baixa a página e tenta montar um resumo curto e neutro (1–2 frases)."""
    html = safe_get(link)
    if not html:
        return ""
    meta = extract_meta_description(html)
    # filtra lixos comuns
    bad = ["cookies", "aceite", "navegação", "experiência", "javascript", "assine", "newsletter"]
    if any(b in meta.lower() for b in bad):
        meta = ""
    # corta em ~280 caracteres no máximo
    if meta:
        meta = meta.strip()
        if len(meta) > 280:
            # tenta cortar em frase
            cut = meta[:280]
            last_dot = cut.rfind(".")
            if last_dot >= 80:
                meta = cut[: last_dot + 1]
            else:
                meta = cut + "…"
    return meta


def domain_name(url: str) -> str:
    try:
        netloc = urlparse(url).netloc
        return netloc.replace("www.", "")
    except Exception:
        return ""


def post_signature(title: str, link: str) -> str:
    """Assinatura única (hash) para dedupe."""
    base = (title or "") + "|" + (link or "")
    return hashlib.sha1(base.encode("utf-8", "ignore")).hexdigest()


class SeenStore:
    """Dedupe simples baseado em arquivo JSON no disco (stateless entre runs)."""
    def __init__(self, path: str = "seen.json", max_size: int = 5000):
        self.path = path
        self.max_size = max_size
        self._data = set()
        self._load()

    def _load(self):
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    arr = json.load(f)
                    self._data = set(arr[-self.max_size :])
        except Exception:
            self._data = set()

    def save(self):
        try:
            arr = list(self._data)[-self.max_size :]
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(arr, f, ensure_ascii=False)
        except Exception:
            pass

    def seen(self, sig: str) -> bool:
        return sig in self._data

    def add(self, sig: str):
        self._data.add(sig)
