#!/opt/matching-test/venv/bin/python3
"""
trigger_server.py — dispara sync_cartera.py + cruzar.py bajo demanda vía HTTP.

Pensado para que `financial-platform` lo llame justo después de escribir una
corrección manual (ej. upsert a cruce_incp_exclusiones) y así no esperar al
próximo tick del cron. Corre en background (la corrida real toma ~1 minuto)
y expone un endpoint de status para hacer polling desde el frontend.

Protegido por token compartido (TRIGGER_TOKEN en .env) — no hay otra
autenticación, así que este servicio NUNCA debe quedar expuesto sin proxy/
Funnel delante y sin el token configurado.
"""
import os
import subprocess
import threading
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import Flask, jsonify, request

load_dotenv()

TRIGGER_TOKEN = os.environ["TRIGGER_TOKEN"]
REPO_DIR = os.path.dirname(os.path.abspath(__file__))
PYTHON = os.path.join(REPO_DIR, "venv", "bin", "python3")

app = Flask(__name__)

_lock = threading.Lock()
_state = {
    "status": "idle",  # idle | running | done | error
    "started_at": None,
    "finished_at": None,
    "exit_code": None,
    "log_tail": "",
}


def _run_cruce():
    with _lock:
        _state.update(
            status="running",
            started_at=datetime.now(timezone.utc).isoformat(),
            finished_at=None,
            exit_code=None,
            log_tail="",
        )
    try:
        result = subprocess.run(
            [PYTHON, "sync_cartera.py"],
            cwd=REPO_DIR, capture_output=True, text=True, timeout=600,
        )
        log = result.stdout + result.stderr
        if result.returncode == 0:
            result2 = subprocess.run(
                [PYTHON, "cruzar.py"],
                cwd=REPO_DIR, capture_output=True, text=True, timeout=600,
            )
            log += result2.stdout + result2.stderr
            returncode = result2.returncode
        else:
            returncode = result.returncode
        with _lock:
            _state.update(
                status="done" if returncode == 0 else "error",
                finished_at=datetime.now(timezone.utc).isoformat(),
                exit_code=returncode,
                log_tail=log[-4000:],
            )
    except Exception as exc:
        with _lock:
            _state.update(
                status="error",
                finished_at=datetime.now(timezone.utc).isoformat(),
                exit_code=-1,
                log_tail=str(exc),
            )


def _autorizado() -> bool:
    return request.headers.get("Authorization") == f"Bearer {TRIGGER_TOKEN}"


@app.post("/trigger/cruce")
def trigger_cruce():
    if not _autorizado():
        return jsonify(error="unauthorized"), 401
    with _lock:
        if _state["status"] == "running":
            return jsonify(status="already_running", **_state), 409
    threading.Thread(target=_run_cruce, daemon=True).start()
    return jsonify(status="started"), 202


@app.get("/trigger/cruce/status")
def trigger_status():
    if not _autorizado():
        return jsonify(error="unauthorized"), 401
    with _lock:
        return jsonify(**_state)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001)
