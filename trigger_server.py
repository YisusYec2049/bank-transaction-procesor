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

import contextlib
import fcntl
import hmac
import os
import subprocess
import threading
import time
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import Flask, jsonify, request

load_dotenv()

TRIGGER_TOKEN = os.environ["TRIGGER_TOKEN"]
REPO_DIR = os.path.dirname(os.path.abspath(__file__))
PYTHON = os.path.join(REPO_DIR, "venv", "bin", "python3")

# El MISMO archivo que toma el cron con `flock`. Hasta el 2026-09-06 el cron se
# protegía con `/tmp/matching.lock` y este servicio solo con un candado en
# memoria: **no se protegían entre sí**, y encima la unidad de systemd tiene
# `PrivateTmp=true`, así que este proceso ni siquiera veía ese archivo. O sea
# que una corrida del cron y un botón podían repartir plata sobre las mismas
# cuotas al mismo tiempo.
#
# Vive en `logs/` porque es la única carpeta que la unidad deja escribir
# (`ReadWritePaths`) y ya es del usuario `matching`, así que los dos lo ven.
CANDADO = os.path.join(REPO_DIR, "logs", "pipeline.lock")

# Cuánto espera el botón a que termine una corrida del cron antes de rendirse.
# Una cadena completa tarda ~3 minutos; 10 le dan margen de sobra sin dejar el
# hilo colgado para siempre si algo se traba del otro lado.
ESPERA_CANDADO_S = 600

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


@contextlib.contextmanager
def _candado_del_pipeline(espera_s: int = ESPERA_CANDADO_S):
    """Toma el mismo candado que usa el cron, o se rinde avisando.

    El cron lo toma con `flock -n`: si este servicio lo tiene, esa corrida del
    cron se salta el turno y vuelve en el siguiente, que es lo correcto — el
    trabajo es idempotente. Acá al revés se ESPERA, porque detrás de un botón
    hay una persona mirando y su cambio tiene que aplicarse.

    Si no se consigue en `espera_s`, se levanta la mano en vez de correr igual:
    correr en paralelo con el cron es lo que este candado existe para impedir.
    """
    os.makedirs(os.path.dirname(CANDADO), exist_ok=True)
    limite = time.monotonic() + espera_s
    with open(CANDADO, 'w') as f:
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= limite:
                    raise TimeoutError(
                        'Hay otra corrida del pipeline en curso (el cron) y no se liberó '
                        f'en {espera_s}s. No se corre en paralelo a propósito.'
                    ) from None
                time.sleep(5)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _correr_cadena(sync: bool, solo: str | None = None, solo_sync: bool = False,
                   ingesta: bool = False):
    """Corre los scripts en orden, cortando en el primero que falle.
    Devuelve (exit_code, log). El orden importa: `cruzar.py` deja
    `cruce_cartera` al día y `cruzar_cartera_preventiva.py` lee de ahí.

    Con `solo`, `cruzar.py` trabaja únicamente ese pago: trae de cada tabla lo
    que ese pago necesita en vez de leerlas enteras, y omite los pases
    globales. `cruzar_cartera_preventiva.py` todavía no tiene modo puntual, así
    que corre completo — es lo que queda por hacer para que un botón responda
    en segundos."""
    if ingesta:
        # La cadena COMPLETA, empezando por leer los archivos. Es lo que dispara
        # el botón de la pantalla de carga, y desde el 2026-09-06 es la corrida
        # principal del día: el área sube sus archivos y aprieta.
        #
        # 🔴 SIN `--cierre-diario`, a propósito y no negociable: el sello lo
        # pone únicamente la corrida de las 9:30. Un botón no puede cerrarle la
        # puerta a un pago, porque el sello no se deshace desde la pantalla.
        scripts = [["sync_cartera.py"], ["procesar_todos.py"],
                   ["cruzar.py"], ["cruzar_cartera_preventiva.py"]]
    elif solo_sync:
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


def _run_pipeline(sync: bool, solo: str | None = None, solo_sync: bool = False,
                  ingesta: bool = False):
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
            with _candado_del_pipeline():
                returncode, log = _correr_cadena(sync, solo, solo_sync, ingesta)
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
                ingesta = _pendiente["ingesta"]
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


def _disparar(sync: bool, solo: str | None = None, solo_sync: bool = False,
              ingesta: bool = False):
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
                _pendiente = {"sync": sync, "solo": solo, "solo_sync": solo_sync,
                              "ingesta": ingesta}
            else:
                encolado_ingesta = ingesta or _pendiente["ingesta"]
                _pendiente = {
                    "sync": sync or _pendiente["sync"],
                    # Una ingesta abarca todo: pedirla junto con un pago puntual
                    # tiene que correr completa, no solo ese pago.
                    "solo": None if encolado_ingesta
                            else (solo if solo == _pendiente["solo"] else None),
                    # Solo sigue siendo "solo sync" si TODO lo encolado lo era.
                    # Si alguien pidió también un recálculo, hay que hacerlo.
                    "solo_sync": solo_sync and _pendiente["solo_sync"] and not encolado_ingesta,
                    "ingesta": encolado_ingesta,
                }
            return jsonify({**_state, "status": "queued"}), 202
    threading.Thread(target=_run_pipeline, args=(sync, solo, solo_sync, ingesta),
                     daemon=True).start()
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


@app.post("/trigger/ingesta")
def trigger_ingesta():
    """La corrida completa, empezando por LEER LOS ARCHIVOS.

    Es lo que dispara el botón de la pantalla de carga, y desde el 2026-09-06 es
    la corrida principal del día: el área sube sus archivos y aprieta. La del
    cron de las 9:30 pasa a ser la red por si alguien sube y no aprieta.

    Es el único disparador que corre `procesar_todos.py`. Los otros tres
    (cruce, reproceso, sync) trabajan sobre pagos que YA entraron.

    🔴 NO sella. El sello lo pone únicamente la corrida de las 9:30, que es la
    que pasa `--cierre-diario`. Un botón no puede cerrarle la puerta a un pago.
    """
    if not _autorizado():
        return jsonify(error="unauthorized"), 401
    return _disparar(sync=True, ingesta=True)


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
