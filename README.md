# Autopost Server (TextSynth + Flask) - Litoral Norte

Servidor para gerar artigos jornalisticos automaticamente e expor um link fixo em HTML consumido pelo seu plugin do WordPress.

## Endpoints
- GET /artigos/ultimo.html — retorna o ultimo artigo gerado (HTML completo com imagem no topo).
- GET /health — status do servidor.
- POST /run-once — forca 1 execucao manual da coleta -> reescrita -> geracao do HTML.

## Variaveis de ambiente
- TEXTSYNTH_API_KEY (obrigatorio)
- REGIAO (default: Litoral Norte de Sao Paulo)
- KEYWORDS (default: litoral norte de sao paulo; ilhabela; sao sebastiao; caraguatatuba; ubatuba)
- FEEDS (opcional)
- RUN_INTERVAL_MIN (default: 15)
- FETCH_WAIT_SECONDS (default: 20)
- MAX_PER_RUN (default: 1)
- TIMEOUT_SECONDS (default: 45)
- PORT (default: 10000)

Deploy no Render: Start Command = python main.py
