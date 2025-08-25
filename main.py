# ... imports existentes ...
from scraper import fetch_rss, is_recent, TOPSTORIES_URL, synthesize_summary_from_page, domain_name, post_signature, SeenStore
# ----------------------------------------------

# CHANGE: parâmetros padrão voltados a volume (pode ajustar por ENV)
FETCH_WAIT_SECONDS = getenv_int("FETCH_WAIT_SECONDS", 10)  # espera menor
WAIT_GNEWS = getenv_int("WAIT_GNEWS", 2)
RUN_INTERVAL_MIN = getenv_int("RUN_INTERVAL_MIN", 3)

# Limites/filtros mais permissivos
MIN_CHARS = getenv_int("MIN_CHARS", 120)          # antes era 220
MIN_PARAGRAPHS = getenv_int("MIN_PARAGRAPHS", 1)  # aceita 1 parágrafo
RECENT_HOURS = getenv_int("RECENT_HOURS", 12)     # janela maior

# Volume por tier
MAX_PER_RUN_PRIMARY = getenv_int("MAX_PER_RUN_PRIMARY", 3)
MAX_PER_RUN_SECONDARY = getenv_int("MAX_PER_RUN_SECONDARY", 5)

# Permite aceitar item sem summary e montar a partir da página
ALLOW_SUMMARY_FALLBACK = getenv_str("ALLOW_SUMMARY_FALLBACK", "true").lower() == "true"

# CHANGE: duas listas — primária (litoral) e secundária (gerais)
KEYWORDS_PRIMARY = [
    "litoral norte de sao paulo",
    "ilhabela",
    "sao sebastiao",
    "caraguatatuba",
    "ubatuba",
]

KEYWORDS_SECONDARY = [
    "futebol",
    "formula 1",
    "f1",
    "governo do estado de são paulo",
    "regata",
    "surf",
    "vôlei",
    "brasil",
    "mundo",
]

# ADD: store de dedupe
SEEN = SeenStore(path=os.getenv("SEEN_PATH", "seen.json"))


def _has_sufficient_content(summary: str) -> bool:
    if not summary:
        return False
    if len(summary) < max(0, MIN_CHARS):
        return False
    # parágrafos: 1 já passa no modo de volume
    parts = [p for p in summary.replace("\r", "").split("\n\n") if p.strip()]
    if len(parts) < max(1, MIN_PARAGRAPHS):
        return False
    return True


def _format_payload(item: dict) -> dict:
    """
    Mantém padrão editorial:
    - título (original)
    - resumo enxuto (1–2 frases)
    - fonte (domínio)
    - link
    """
    title = item.get("title", "").strip()
    link = item.get("link", "").strip()
    summary = item.get("summary", "").strip()

    fonte = domain_name(link)
    resumo = summary

    # se estiver longo demais, aparar (garante padrão)
    if len(resumo) > 350:
        cut = resumo[:350]
        dot = cut.rfind(".")
        resumo = cut[: dot + 1] if dot >= 120 else cut + "…"

    return {
        "regiao": REGIAO,
        "keyword": item.get("keyword", ""),
        "title": title,
        "summary": resumo,
        "fonte": fonte,
        "link": link,
        "published_iso": item.get("published_iso", ""),
    }


def _process_keyword(kw: str, per_run_limit: int):
    try:
        items, url = fetch_rss(kw, limit=per_run_limit)
    except Exception as e:
        logger.exception(f"[JOB] Erro em '{kw}': {e}")
        return

    if not items:
        logger.info(f"[GNEWS] sem itens: {url}")
        time.sleep(WAIT_GNEWS)
        return

    posted = 0

    for it in items:
        logger.info(f"[GNEWS] aguardando {WAIT_GNEWS}s: {it.get('link') or url}")
        time.sleep(WAIT_GNEWS)

        title = it.get("title", "") or ""
        link = it.get("link", "") or ""
        summary = it.get("summary", "") or ""
        iso = it.get("published_iso", "")

        sig = post_signature(title, link)
        if SEEN.seen(sig):
            logger.info(f"[SKIP] já publicado: {title}")
            continue

        recent_ok = is_recent(iso, RECENT_HOURS)

        # Filtro e fallback de resumo
        if not _has_sufficient_content(summary):
            if ALLOW_SUMMARY_FALLBACK and link:
                fallback = synthesize_summary_from_page(link)
                if fallback and len(fallback) >= MIN_CHARS:
                    summary = fallback

        # Se ainda estiver curto ou não recente, pula
        if not recent_ok or not _has_sufficient_content(summary):
            logger.warning(
                f"[JOB] Conteúdo insuficiente (kw: {kw}) em {(link or url)}"
            )
            continue

        payload = _format_payload(
            {
                "keyword": kw,
                "title": title,
                "summary": summary,
                "link": link,
                "published_iso": iso,
            }
        )

        # publica (adapte para o teu plugin)
        _publish_item(payload)

        SEEN.add(sig)
        SEEN.save()
        posted += 1

        if FETCH_WAIT_SECONDS > 0:
            time.sleep(FETCH_WAIT_SECONDS)

    logger.info(f"[JOB] {kw}: publicados {posted}")


def job_run():
    logger.info("[JOB] start")

    # 1) TIER PRIMÁRIO — litoral
    for kw in KEYWORDS_PRIMARY:
        _process_keyword(kw, MAX_PER_RUN_PRIMARY)

    # 2) TIER SECUNDÁRIO — gerais
    for kw in KEYWORDS_SECONDARY:
        _process_keyword(kw, MAX_PER_RUN_SECONDARY)

    # 3) Fallback absoluto (Top Stories) — se nada passou nos tiers
    #    *Deixa comentado se não quiser usar*
    # logger.info("[FALLBACK] Top Stories")
    # try:
    #     feed = feedparser.parse(TOPSTORIES_URL)
    #     for entry in feed.entries[:MAX_PER_RUN_SECONDARY]:
    #         fake_item = {
    #             "title": entry.get("title", ""),
    #             "link": entry.get("link", ""),
    #             "summary": entry.get("summary", ""),
    #             "published_iso": "",
    #         }
    #         _process_keyword("topstories", 0)  # reaproveita lógica se quiser
    # except Exception as e:
    #     logger.warning(f"[FALLBACK] erro: {e}")

    logger.info("[JOB] done")
