#!/opt/matching-test/venv/bin/python3
"""
trigger_server.py — dispara operaciones bajo demanda vía HTTP.

  - POST /trigger/cruce      → sync_cartera.py && cruzar.py &&
                               cruzar_cartera_preventiva.py
  - POST /trigger/reproceso  → cruzar.py && cruzar_cartera_preventiva.py
    (mismo carril que /trigger/cruce, sin volver a bajar los Excel de Drive)
  - POST /trigger/sync       → sync_cartera.py y nada más (botón "Buscar
    archivos nuevos": solo mira Drive, no recalcula)
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
"""

import hmac
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


def _correr_cadena(sync: bool, solo: str | None = None, solo_sync: bool = False):
    """Corre los scripts en orden, cortando en el primero que falle.
    Devuelve (exit_code, log). El orden importa: `cruzar.py` deja
    `cruce_cartera` al día y `cruzar_cartera_preventiva.py` lee de ahí.

    Con `solo`, `cruzar.py` trabaja únicamente ese pago: trae de cada tabla lo
    que ese pago necesita en vez de leerlas enteras, y omite los pases
    globales. `cruzar_cartera_preventiva.py` todavía no tiene modo puntual, así
    que corre completo — es lo que queda por hacer para que un botón responda
    en segundos."""
    if solo_sync:
        # Solo bajar archivos de Drive y refrescar las tablas de referencia. No
        # se recalcula nada: la cartera nueva queda EN ESPERA hasta que alguien
        # aprete "Cargar Cartera", así que no hay nada que recalcular todavía.
        scripts = [["sync_cartera.py"]]
    else:
        scripts = [["sync_cartera.py"]] if sync else []
        scripts += [["cruzar.py"] + (["--solo", solo] if solo else []),
                    ["cruzar_cartera_preventiva.py"]]

    log = ""
    for script in scripts:
        result = subprocess.run(
            [PYTHON, *script],
            cwd=REPO_DIR, capture_output=True, text=True, timeout=600,
        )
        log += result.stdout + result.stderr
        if result.returncode != 0:
            return result.returncode, log
    return 0, log


def _run_pipeline(sync: bool, solo: str | None = None, solo_sync: bool = False):
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
            returncode, log = _correr_cadena(sync, solo, solo_sync)
        except Exception as exc:
            returncode, log = -1, str(exc)

        with _lock:
            if _pendiente is not None:
                # Alguien disparó mientras corríamos: volver a correr en vez
                # de perder ese cambio. Varias peticiones encoladas se
                # colapsan en esta única re-corrida.
                sync = _pendiente["sync"]
                solo = _pendiente["solo"]
                solo_sync = _pendiente["solo_sync"]
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
    """Compara el token en tiempo constante.

    `==` sobre cadenas corta en el primer carácter distinto, así que **cuánto
    tarda en responder depende de cuántos caracteres acertó** quien pregunta.
    Con suficientes intentos, eso permite adivinar el token carácter por
    carácter sin conocerlo. `compare_digest` siempre recorre todo.

    Este servicio está expuesto a internet por Tailscale Funnel y sus endpoints
    corren el pipeline entero, así que el token es la única puerta.
    """
    recibido = request.headers.get("Authorization", "")
    return hmac.compare_digest(recibido, f"Bearer {TRIGGER_TOKEN}")


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


def _disparar(sync: bool, solo: str | None = None, solo_sync: bool = False):
    """Arranca el pipeline, o encola una re-corrida si ya hay una en curso.
    Nunca descarta la petición: el llamador siempre puede asumir que su
    cambio va a reprocesarse.

    Al colapsar varias peticiones encoladas **gana siempre el alcance más
    amplio**: si se encolan dos pagos distintos, la re-corrida los cubre a los
    dos corriendo completa. Preferir uno de los dos dejaría el otro cambio sin
    aplicar, que es exactamente lo que esta cola existe para evitar."""
    global _pendiente
    with _lock:
        if _state["status"] == "running":
            if _pendiente is None:
                _pendiente = {"sync": sync, "solo": solo, "solo_sync": solo_sync}
            else:
                _pendiente = {
                    "sync": sync or _pendiente["sync"],
                    "solo": solo if solo == _pendiente["solo"] else None,
                    # Solo sigue siendo "solo sync" si TODO lo encolado lo era.
                    # Si alguien pidió también un recálculo, hay que hacerlo.
                    "solo_sync": solo_sync and _pendiente["solo_sync"],
                }
            return jsonify({**_state, "status": "queued"}), 202
    threading.Thread(target=_run_pipeline, args=(sync, solo, solo_sync), daemon=True).start()
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
    `sync_cartera.py` es la parte lenta de la cadena.

    Acepta `matching_key` (en el cuerpo JSON o como parámetro) para reprocesar
    **solo ese pago**: es lo que corresponde cuando el botón corrigió una fila
    concreta. Sin él, se recalcula todo, como antes."""
    if not _autorizado():
        return jsonify(error="unauthorized"), 401
    cuerpo = request.get_json(silent=True) or {}
    solo = (cuerpo.get("matching_key") or request.args.get("matching_key") or "").strip()
    return _disparar(sync=False, solo=solo or None)


@app.post("/trigger/sync")
def trigger_sync():
    """Solo trae archivos de Drive: NO recalcula nada.

    Es lo que necesita el botón "Buscar archivos nuevos" de Cartera Preventiva.
    Ese botón usaba `/trigger/cruce`, que además cruza y reparte pagos — minutos
    de trabajo para responder una pregunta que se contesta en segundos: ¿llegó
    una cartera nueva?

    No hace falta recalcular después, y esa es la razón de que pueda ser solo
    sync: la cartera nueva queda **en espera**, y solo entra en vivo cuando
    alguien aprieta "Cargar Cartera" (que sí dispara su propio reproceso).

    Comparte el carril del pipeline a propósito: `sync_cartera.py` reemplaza las
    tablas de referencia que `cruzar.py` lee, y correrlos a la vez le daría al
    cruce tablas a medio actualizar. Es el mismo motivo por el que el cron los
    encadena en vez de lanzarlos en paralelo.
    """
    if not _autorizado():
        return jsonify(error="unauthorized"), 401
    return _disparar(sync=True, solo_sync=True)


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
