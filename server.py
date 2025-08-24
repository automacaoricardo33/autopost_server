# server.py
import os
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from main import LATEST, job_run, start_scheduler_if_needed

app = FastAPI(title="Autopost Server", version="1.0.0")

_scheduler = None

@app.on_event("startup")
async def on_startup():
    global _scheduler
    # Inicia o scheduler dentro do web service
    _scheduler = start_scheduler_if_needed()

@app.get("/")
def root():
    return {"ok": True, "keys": list(LATEST.keys())}

@app.get("/artigos/ultimo.json")
def artigos_ultimo():
    """
    Devolve o último artigo por keyword.
    ?kw=palavra  -> retorna só daquela
    """
    from fastapi import Request
    def serialize(d):
        if not d: return {}
        return d
    # pega query param kw se vier
    # (acesso simples sem depender de Request no handler)
    return JSONResponse(LATEST)

@app.get("/artigos/ultimo_por_kw.json")
def ultimo_por_kw(kw: str):
    return JSONResponse(LATEST.get(kw, {}))
