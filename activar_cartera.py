#!/opt/matching-test/venv/bin/python3
"""
activar_cartera.py — swap manual de versión de Cartera Preventiva (Spec C —
"Carpetas de Drive + Versión de carga", 21 de julio). Lo dispara el botón
"Cargar Cartera" de financial-platform vía POST /trigger/cartera/activar en
trigger_server.py — NUNCA por cron, solo tras verificación humana de que lo
que hay en staging está bien.

Modelo (ver Spec C §1): `cartera_preventiva` es SIEMPRE la única versión
ACTIVA (mantiene su `llave` único, del que dependen los joins de
`pago_asociaciones`/`cartera_preventiva_overrides`/`cartera_saldos_favor`).
`cartera_preventiva_staging` es el Excel más reciente subido, todavía sin
activar (lo llena sync_cartera.py). Este script hace el SWAP entre ambas.

Pasos (en este orden, cada uno depende de que el anterior haya terminado):
  1. Verificar que haya algo en staging — si no, abortar sin tocar nada (no
     tiene sentido "activar" una cartera vacía).
  2. Determinar el carga_id de la versión SALIENTE (la que hoy está activa
     en cartera_preventiva): se busca en cartera_cargas la fila
     estado='activa'. Si no existe ninguna (bootstrap: la cartera activa
     hoy es de antes de que existiera este sistema de versiones, sin
     marcador propio), se genera un carga_id nuevo solo para poder
     etiquetar lo que se archiva.
  3. Archivar TODO el estado de la versión saliente — decisión del usuario
     (2026-07-21): archivar, nunca borrar sin rastro. `cartera_preventiva`,
     `pago_asociaciones`, `cartera_saldos_favor` y
     `cartera_preventiva_overrides` se copian completos a sus tablas
     `_archivo` con ese carga_id (sin `id`: cada `_archivo` genera el suyo,
     nunca se reusan ids explícitos entre tablas).
  4. Vaciar las 4 tablas vivas (equivalente a TRUNCATE — la API REST de
     PostgREST no expone TRUNCATE, ver delete_all_rows).
  5. Copiar `cartera_preventiva_staging` -> `cartera_preventiva` (sin `id`,
     para que la tabla viva asigne ids nuevos) y vaciar staging.
  6. Actualizar `cartera_cargas`: la saliente pasa a 'archivada'; la que
     estaba 'staged' pasa a 'activa' (o se crea si no existía — caso borde
     de una base sin ese marcador, ej. si alguien insertó el Excel en
     staging por otra vía).

Tras el swap, `cruzar_cartera_preventiva.py` recalcula la cartera nueva
desde cero en su próxima corrida (pago_asociaciones vacía = arranque
limpio) — no se arrastra ningún saldo a favor, cierre o estado de la
versión anterior a la nueva, por diseño explícito del usuario.
"""

import logging
import os
import sys
from datetime import datetime

import pytz
from dotenv import load_dotenv

from utils.supabase import select_all, insert_rows, delete_all_rows, upsert_cartera_cargas

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%S',
    stream=sys.stdout,
)
log = logging.getLogger(__name__)


def _sin_id(rows: list[dict]) -> list[dict]:
    """Quita la clave 'id' de cada fila — usado al copiar hacia una tabla
    `_archivo` o hacia la viva, para que cada tabla de destino genere sus
    propios ids (bigserial) en vez de reusar los de origen."""
    return [{k: v for k, v in r.items() if k != 'id'} for r in rows]


def _dedup_por_llave(rows: list[dict]) -> list[dict]:
    """Se queda con UNA fila por 'llave' (primera coincidencia gana) — el
    Excel de cartera trae llaves repetidas, pero cartera_preventiva.llave es
    único (idx_cartera_preventiva_llave_unique). Sin esto, el INSERT hacia la
    tabla viva falla con 23505 (duplicate key) y el swap se cae a mitad,
    dejando la cartera viva vacía. Mismo criterio de dedup que
    sync_cartera_preventiva."""
    vistas: set = set()
    out: list[dict] = []
    for r in rows:
        llave = r.get('llave')
        if not llave or llave in vistas:
            continue
        vistas.add(llave)
        out.append(r)
    return out


def main():
    load_dotenv()
    supabase_url = os.environ.get('SUPABASE_URL', '')
    srk          = os.environ.get('SUPABASE_SERVICE_ROLE_KEY', '')
    if not supabase_url or not srk:
        log.error('SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY no configurados.')
        sys.exit(1)

    tz_bogota = pytz.timezone('America/Bogota')
    ahora = datetime.now(tz_bogota).strftime('%Y-%m-%dT%H:%M:%S.%f%z')

    log.info('Verificando que haya una versión staged para activar...')
    staging_rows = select_all(supabase_url, srk, 'cartera_preventiva_staging', select='*')
    if not staging_rows:
        log.error('cartera_preventiva_staging está vacía — no hay ninguna versión nueva '
                  'para activar. Abortando sin tocar nada.')
        sys.exit(1)

    cargas = select_all(supabase_url, srk, 'cartera_cargas', select='carga_id,estado,filas')
    carga_activa = next((c for c in cargas if c.get('estado') == 'activa'), None)
    carga_staged = next((c for c in cargas if c.get('estado') == 'staged'), None)

    carga_saliente_id = carga_activa['carga_id'] if carga_activa else f'bootstrap-{ahora}'
    carga_entrante_id = carga_staged['carga_id'] if carga_staged else ahora

    log.info('Archivando versión saliente (carga_id=%s)...', carga_saliente_id)
    activa_rows       = select_all(supabase_url, srk, 'cartera_preventiva', select='*')
    asociaciones_rows = select_all(supabase_url, srk, 'pago_asociaciones', select='*')
    saldos_favor_rows = select_all(supabase_url, srk, 'cartera_saldos_favor', select='*')
    overrides_rows    = select_all(supabase_url, srk, 'cartera_preventiva_overrides', select='*')

    if activa_rows:
        insert_rows(supabase_url, srk, 'cartera_preventiva_archivo',
                    [{**r, 'carga_id': carga_saliente_id} for r in _sin_id(activa_rows)])
    if asociaciones_rows:
        insert_rows(supabase_url, srk, 'pago_asociaciones_archivo',
                    [{**r, 'carga_id': carga_saliente_id} for r in _sin_id(asociaciones_rows)])
    if saldos_favor_rows:
        insert_rows(supabase_url, srk, 'cartera_saldos_favor_archivo',
                    [{**r, 'carga_id': carga_saliente_id} for r in _sin_id(saldos_favor_rows)])
    if overrides_rows:
        # cartera_preventiva_overrides tiene PK 'llave', no 'id' — nada que quitar.
        insert_rows(supabase_url, srk, 'cartera_preventiva_overrides_archivo',
                    [{**r, 'carga_id': carga_saliente_id} for r in overrides_rows])

    log.info('%d cuota(s), %d asociación(es), %d saldo(s) a favor, %d override(s) archivados.',
              len(activa_rows), len(asociaciones_rows), len(saldos_favor_rows), len(overrides_rows))

    log.info('Vaciando tablas vivas (borrón y cuenta nueva, decisión del usuario)...')
    delete_all_rows(supabase_url, srk, 'cartera_preventiva', 'id')
    delete_all_rows(supabase_url, srk, 'pago_asociaciones', 'id')
    delete_all_rows(supabase_url, srk, 'cartera_saldos_favor', 'id')
    delete_all_rows(supabase_url, srk, 'cartera_preventiva_overrides', 'llave')

    staging_unicas = _dedup_por_llave(staging_rows)
    log.info('Activando la versión staged (%d fila(s) del Excel, %d únicas por llave)...',
             len(staging_rows), len(staging_unicas))
    insert_rows(supabase_url, srk, 'cartera_preventiva', _sin_id(staging_unicas))
    delete_all_rows(supabase_url, srk, 'cartera_preventiva_staging', 'id')

    log.info('Actualizando cartera_cargas...')
    cargas_a_upsert = [{'carga_id': carga_entrante_id, 'estado': 'activa', 'filas': len(staging_unicas)}]
    if activa_rows or carga_activa:
        filas_saliente = carga_activa['filas'] if carga_activa else len(activa_rows)
        cargas_a_upsert.insert(0, {'carga_id': carga_saliente_id, 'estado': 'archivada',
                                    'filas': filas_saliente})
    upsert_cartera_cargas(supabase_url, srk, cargas_a_upsert)

    log.info('activar_cartera.py completado: versión %s archivada, versión %s activada (%d cuotas).',
              carga_saliente_id, carga_entrante_id, len(staging_unicas))


if __name__ == '__main__':
    main()
