#!/opt/matching-test/venv/bin/python3
"""
trigger_server.py — dispara operaciones bajo demanda vía HTTP.

  - POST /trigger/cruce      → sync_cartera.py && cruzar.py &&
                               cruzar_cartera_preventiva.py
  - POST /trigger/reproceso  → cruzar.py && cruzar_cartera_preventiva.py
    (mismo carril que /trigger/cruce, sin volver a bajar los Excel de Drive)
  - POST /trigger/cartera/activar → activar_cartera.py (Spec C, el swap
    manual de versión de Cartera Preventiva, botón "Cargar Cartera")

Pensado para que `financial-platform` lo llame justo después de escribir una
corrección manual (corregir una cédula, marcar matrícula, asociar un pago,
cerrar una cuota) y así no esperar al próximo tick del cron.

Dos reglas de diseño que importan:

1. **Un solo carril para el pipeline.** `/trigger/cruce` y `/trigger/reproceso`
   corren los mismos scripts sobre las mismas tablas, así que comparten lock y
   estado: dos corridas simultáneas de `cruzar.py` se pisarían entre sí. El
   swap de cartera sí va aparte (otro script, otras tablas).
2. **Las peticiones se encolan, no se descartan.** Si llega un disparo mientras
   hay una corrida en curso, se marca una re-corrida pendiente y se ejecuta al
   terminar (varias peticiones se colapsan en una sola). Antes esto devolvía
   409 y el frontend lo tragaba con `.catch(() => null)` — con cada botón
   disparando, eso significaba perder cambios en silencio hasta el cron. Si lo
   encolado incluye un pedido con sync, la re-corrida lo incluye.

Protegido por token compartido (TRIGGER_TOKEN en .env) — no hay otra
autenticación, así que este servicio NUNCA debe quedar expuesto sin proxy/
Funnel delante y sin el token configurado.

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
# Re-corrida encolada mientras hay una en curso: None = nada pendiente,
# True/False = hay pendiente y ese valor dice si debe incluir sync_cartera.py.
_pendiente = None

# Swap de versión de cartera (Spec C, 21 de julio) — endpoint y estado
# separados del cruce de arriba: son operaciones independientes, no deben
# bloquearse entre sí ni compartir el mismo _lock/_state.
_lock_cartera = threading.Lock()
_state_cartera = {
    "status": "idle",  # idle | running | done | error
    "started_at": None,
    "finished_at": None,
    "exit_code": None,
    "log_tail": "",
}


def _correr_cadena(sync: bool):
    """Corre los scripts en orden, cortando en el primero que falle.
    Devuelve (exit_code, log). El orden importa: `cruzar.py` deja
    `cruce_cartera` al día y `cruzar_cartera_preventiva.py` lee de ahí."""
    scripts = ["sync_cartera.py"] if sync else []
    scripts += ["cruzar.py", "cruzar_cartera_preventiva.py"]

    log = ""
    for script in scripts:
        result = subprocess.run(
            [PYTHON, script],
            cwd=REPO_DIR, capture_output=True, text=True, timeout=600,
        )
        log += result.stdout + result.stderr
        if result.returncode != 0:
            return result.returncode, log
    return 0, log


def _run_pipeline(sync: bool):
    """Carril único del pipeline. Al terminar revisa si se encoló otra
    petición mientras corría y, si la hay, vuelve a correr sin soltar el
    estado a `done` — así el frontend que está haciendo polling ve una sola
    operación continua en vez de un hueco en `idle`."""
    global _pendiente
    with _lock:
        _state.update(
            status="running",
            started_at=datetime.now(timezone.utc).isoformat(),
            finished_at=None,
            exit_code=None,
            log_tail="",
        )
    while True:
        try:
            returncode, log = _correr_cadena(sync)
        except Exception as exc:
            returncode, log = -1, str(exc)

        with _lock:
            if _pendiente is not None:
                # Alguien disparó mientras corríamos: volver a correr en vez
                # de perder ese cambio. Varias peticiones encoladas se
                # colapsan en esta única re-corrida.
                sync = _pendiente
                _pendiente = None
                continue
            _state.update(
                status="done" if returncode == 0 else "error",
                finished_at=datetime.now(timezone.utc).isoformat(),
                exit_code=returncode,
                log_tail=log[-4000:],
            )
            return


def _run_activar_cartera():
    """Corre activar_cartera.py (el swap de versión, Spec C) en background.
    Deliberadamente NO encadena sync_cartera.py ni cruzar.py antes/después:
    el botón "Cargar Cartera" solo dispara el swap — es responsabilidad del
    usuario haber revisado staging antes de apretarlo, y el próximo tick del
    cron (o un POST /trigger/cruce aparte) ya recalcula lo que siga."""
    with _lock_cartera:
        _state_cartera.update(
            status="running",
            started_at=datetime.now(timezone.utc).isoformat(),
            finished_at=None,
            exit_code=None,
            log_tail="",
        )
    try:
        result = subprocess.run(
            [PYTHON, "activar_cartera.py"],
            cwd=REPO_DIR, capture_output=True, text=True, timeout=600,
        )
        with _lock_cartera:
            _state_cartera.update(
                status="done" if result.returncode == 0 else "error",
                finished_at=datetime.now(timezone.utc).isoformat(),
                exit_code=result.returncode,
                log_tail=(result.stdout + result.stderr)[-4000:],
            )
    except Exception as exc:
        with _lock_cartera:
            _state_cartera.update(
                status="error",
                finished_at=datetime.now(timezone.utc).isoformat(),
                exit_code=-1,
                log_tail=str(exc),
            )


def _autorizado() -> bool:
    return request.headers.get("Authorization") == f"Bearer {TRIGGER_TOKEN}"


@app.post("/trigger/cartera/activar")
def trigger_activar_cartera():
    if not _autorizado():
        return jsonify(error="unauthorized"), 401
    with _lock_cartera:
        if _state_cartera["status"] == "running":
            return jsonify({**_state_cartera, "status": "already_running"}), 409
    threading.Thread(target=_run_activar_cartera, daemon=True).start()
    return jsonify(status="started"), 202


@app.get("/trigger/cartera/activar/status")
def trigger_activar_cartera_status():
    if not _autorizado():
        return jsonify(error="unauthorized"), 401
    with _lock_cartera:
        return jsonify(**_state_cartera)


def _disparar(sync: bool):
    """Arranca el pipeline, o encola una re-corrida si ya hay una en curso.
    Nunca descarta la petición: el llamador siempre puede asumir que su
    cambio va a reprocesarse."""
    global _pendiente
    with _lock:
        if _state["status"] == "running":
            # Si ya había algo encolado, gana el alcance más amplio (con sync).
            _pendiente = sync or bool(_pendiente)
            return jsonify({**_state, "status": "queued"}), 202
    threading.Thread(target=_run_pipeline, args=(sync,), daemon=True).start()
    return jsonify(status="started"), 202


@app.post("/trigger/cruce")
def trigger_cruce():
    """Actualización completa: vuelve a bajar los Excel de referencia de Drive
    antes de cruzar. Es el botón explícito "Actualizar cruce"."""
    if not _autorizado():
        return jsonify(error="unauthorized"), 401
    return _disparar(sync=True)


@app.post("/trigger/reproceso")
def trigger_reproceso():
    """Reproceso tras una acción manual en la UI (corregir documento, marcar
    matrícula/cesantías, asociar un pago, cerrar una cuota). No baja nada de
    Drive: los archivos de referencia no cambiaron por apretar un botón, y
    `sync_cartera.py` es la parte lenta de la cadena."""
    if not _autorizado():
        return jsonify(error="unauthorized"), 401
    return _disparar(sync=False)


@app.get("/trigger/reproceso/status")
def trigger_reproceso_status():
    # Mismo carril que /trigger/cruce, así que mismo estado. Existe como alias
    # para que el frontend no tenga que saber cuál de los dos disparó.
    if not _autorizado():
        return jsonify(error="unauthorized"), 401
    with _lock:
        return jsonify(**_state)


@app.get("/trigger/cruce/status")
def trigger_status():
    if not _autorizado():
        return jsonify(error="unauthorized"), 401
    with _lock:
        return jsonify(**_state)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001)
