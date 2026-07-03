#!/opt/matching-test/venv/bin/python3
"""
trigger_server.py — expone un endpoint HTTP para disparar sync_cartera.py +
cruzar.py bajo demanda, en vez de esperar al cron.

Pensado para el botón "Cruce de Cartera" de financial-platform: al hacer clic,
el frontend llama a este servicio (vía una API route de Next.js que agrega el
token, nunca expuesto al navegador) en lugar de correr los scripts a mano.

Corre como servicio aparte (gunicorn/systemd), escuchando solo en localhost —
la exposición pública (dominio + HTTPS) la maneja el reverse proxy del VPS.

Endpoints:
  POST /trigger/cruce         → dispara sync_cartera.py y luego cruzar.py en
                                 background. 202 si arrancó, 409 si ya había
                                 una corrida en curso.
  GET  /trigger/cruce/status  → estado de la última corrida.

Ambos requieren header 'Authorization: Bearer <CRUCE_TRIGGER_TOKEN>'.
"""

import logging
import os
import subprocess
import sys
import threading
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import Flask, jsonify, request

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%S',
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PYTHON   = os.path.join(BASE_DIR, 'venv', 'bin', 'python3')
TOKEN    = os.environ.get('CRUCE_TRIGGER_TOKEN', '')

app = Flask(__name__)

_lock  = threading.Lock()
_state = {'status': 'idle', 'started_at': None, 'finished_at': None, 'detail': ''}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check_auth() -> bool:
    if not TOKEN:
        log.error('CRUCE_TRIGGER_TOKEN no configurado en .env — rechazando todo.')
        return False
    return request.headers.get('Authorization', '') == f'Bearer {TOKEN}'


def _run_pipeline():
    for name in ('sync_cartera.py', 'cruzar.py'):
        log.info('Ejecutando %s ...', name)
        result = subprocess.run(
            [PYTHON, os.path.join(BASE_DIR, name)],
            cwd=BASE_DIR, capture_output=True, text=True,
        )
        if result.returncode != 0:
            log.error('%s falló (exit %d): %s', name, result.returncode, result.stderr[-4000:])
            _state.update(status='error', finished_at=_now(),
                           detail=f'{name} falló: {result.stderr[-2000:]}')
            return
        log.info('%s OK.', name)
    _state.update(status='done', finished_at=_now(), detail='OK')


def _run_and_release():
    try:
        _run_pipeline()
    finally:
        _lock.release()


@app.post('/trigger/cruce')
def trigger_cruce():
    if not _check_auth():
        return jsonify(error='no autorizado'), 401

    if not _lock.acquire(blocking=False):
        return jsonify(status='running', detail='ya hay una corrida en curso'), 409

    _state.update(status='running', started_at=_now(), finished_at=None, detail='')
    threading.Thread(target=_run_and_release, daemon=True).start()
    return jsonify(status='started'), 202


@app.get('/trigger/cruce/status')
def trigger_status():
    if not _check_auth():
        return jsonify(error='no autorizado'), 401
    return jsonify(_state)


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=int(os.environ.get('TRIGGER_PORT', '8787')))
