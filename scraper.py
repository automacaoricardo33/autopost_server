# -*- coding: utf-8 -*-
"""
scraper.py — busca itens no RSS do Google News (search)
"""

from __future__ import annotations

import re
from time import mktime
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Tuple
from urllib.parse import quote_plus

import feedparser

GNEWS_BASE = "https://news.google.com"


def _strip_html(text: str) -> str:
    if not text:
        return ""
    # remove tags simples
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p\s*>", "\n\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    # colapsa espaços
    text = re.sub(r"[ \t]+", " ", text).strip()
    return text


def build_search_url(
    query: str,
    hl: str = "pt-BR",
    gl: str = "BR",
    ceid: str = "BR:pt-419",
) -> str:
    """
    Monta URL válida para o feed de busca do Google News, com encode correto.
    Ex.: /rss/search?q=litoral+norte+de+sao+paulo&hl=pt-BR&gl=BR&ceid=BR:pt-419
    """
    q = quote_plus((query or "").strip())
    return f"{GNEWS_BASE}/rss/search?q={q}&hl={hl}&gl={gl}&ceid={ceid}"


def fetch_rss(
    query: str,
    limit: int = 10,
    hl: str = "pt-BR",
    gl: str = "BR",
    ceid: str = "BR:pt-419",
) -> Tuple[List[Dict], str]:
    """
    Busca itens do RSS de busca do Google News para uma 'query'.
    Retorna (items, url_usada).
    """
    url = build_search_url(query, hl=hl, gl=gl, ceid=ceid)
    feed = feedparser.parse(url)

    items: List[Dict] = []
    for entry in feed.entries[: max(0, int(limit))]:
        summary = entry.get("summary", "") or entry.get("description", "")
        summary = _strip_html(summary)

        # data/hora (pode não vir)
        published = entry.get("published", "")
        published_parsed = entry.get("published_parsed")
        if published_parsed:
            dt = datetime.fromtimestamp(mktime(published_parsed), tz=timezone.utc)
            published_iso = dt.isoformat()
        else:
            published_iso = ""

        items.append(
            {
                "title": entry.get("title", "") or "",
                "link": entry.get("link", "") or "",
                "summary": summary,
                "published": published,
                "published_iso": published_iso,
            }
        )

    return items, url


def is_recent(published_iso: str, max_hours: int) -> bool:
    """
    Verifica se um item é recente (<= max_hours) considerando UTC.
    Se não houver data, considera 'recente' para não descartar injustamente.
    """
    if not published_iso:
        return True
    try:
        dt = datetime.fromisoformat(published_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return now - dt <= timedelta(hours=max(0, int(max_hours)))
    except Exception:
        return True  # em dúvida, não bloqueia
