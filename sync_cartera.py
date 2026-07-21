#!/opt/matching-test/venv/bin/python3
"""
sync_cartera.py — sincroniza los Excel de referencia del cruce de cartera a Supabase.

Los 3 archivos (Payu UC, Ingresos PSE y PAYU, Cartera Preventiva) pasan a
vivir cada uno en SU PROPIA carpeta de Drive (Spec C — "Carpetas de Drive +
versión de carga", 21 de julio), con su propia carpeta Histórico:
  - PAYU_UC_FOLDER_ID / PAYU_UC_HIST_FOLDER_ID
  - INGRESOS_FOLDER_ID / INGRESOS_HIST_FOLDER_ID
  - CARTERA_PREV_FOLDER_ID / CARTERA_PREV_HIST_FOLDER_ID
Si alguna de estas variables no está seteada en .env, cae a
CARTERA_DRIVE_FOLDER_ID (la carpeta única de antes) como fallback, para no
romper el VPS mientras el usuario crea las carpetas nuevas.

Los 3 archivos son OPCIONALES: si un archivo no está en su carpeta esta
corrida, no es error — se salta y se mantiene lo que ya se cargó antes.
Ingresos PSE y PAYU en particular solo se sube el primer y el último día de
la semana; su ausencia el resto de los días es normal. Tras cargar un
archivo con éxito, se MUEVE a su carpeta Histórico (nunca se borra) para que
la carpeta de trabajo quede limpia — así "no hay archivo esta corrida" y
"ya se cargó" son indistinguibles por diseño, y detectar si Cartera
Preventiva tiene una versión nueva pendiente de activar se reduce a mirar
`cartera_cargas` (ver más abajo), sin comparar nombres de archivo.

Escrituras por archivo:
  - Payu UC.xlsx            → cartera_inscrip (replace_table)
  - Ingresos PSE y PAYU.xlsx → cartera_ingresos_bancolombia_2576 / _wompi /
                                _stripe_usa (replace_table cada una)
  - CARTERA PREVENTIVA*.xlsx → cartera_preventiva_staging (replace_table) —
    YA NO escribe sobre cartera_preventiva (la tabla VIVA). Cada carga nueva
    marca una fila `cartera_cargas(estado='staged')` — el marcador que
    prende el banner "hay cartera pendiente" en financial-platform. La
    tabla viva solo cambia cuando alguien aprieta el botón "Cargar Cartera"
    (ver activar_cartera.py), nunca por este script.

Se corre por cron (encadenado con cruzar.py, ver crontab del VPS) o
manualmente cada vez que el equipo sube un Excel nuevo.
"""

import io
import logging
import os
import sys
from datetime import datetime

import pytz
from dotenv import load_dotenv

from utils.drive import build_drive_service, find_file_id, find_latest_file, download_pdf as download_file, move_file
from utils.excel_cartera import (
    read_inscrip, read_bancolombia_2576, read_wompi, read_stripe_usa,
    read_cartera_preventiva,
)
from utils.supabase import (replace_table, replace_cartera_preventiva_staging,
                             select_all, delete_by_keys, upsert_cartera_cargas)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%S',
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

PAYU_UC_FILENAME     = 'Payu UC.xlsx'
INGRESOS_FILENAME    = 'Ingresos PSE y PAYU.xlsx'
CARTERA_PREV_PATTERN = 'CARTERA PREVENTIVA'


def _registrar_carga_staged(supabase_url: str, srk: str, filas: int) -> str:
    """Marca en `cartera_cargas` que hay una versión nueva de Cartera
    Preventiva staged, sin activar. Si ya había una carga `staged` de una
    subida anterior (nunca activada), se borra su fila de control — el
    Excel nuevo ya reemplazó por completo el contenido de staging, así que
    esa carga vieja ya no existe en ningún lado y dejar su fila sería un
    'staged' fantasma. Devuelve el `carga_id` nuevo (timestamp ISO Bogotá)."""
    tz_bogota = pytz.timezone('America/Bogota')
    carga_id = datetime.now(tz_bogota).strftime('%Y-%m-%dT%H:%M:%S.%f%z')

    cargas = select_all(supabase_url, srk, 'cartera_cargas', select='carga_id,estado')
    staged_viejas = [c['carga_id'] for c in cargas if c.get('estado') == 'staged']
    if staged_viejas:
        delete_by_keys(supabase_url, srk, 'cartera_cargas', 'carga_id', staged_viejas)

    upsert_cartera_cargas(supabase_url, srk, [{
        'carga_id': carga_id, 'filas': filas, 'estado': 'staged',
    }])
    return carga_id


def _procesar_opcional(drive, nombre: str, folder_id: str, hist_folder_id: str,
                        buscar_archivo, cargar) -> None:
    """Patrón común a los 3 archivos de referencia: buscar en su carpeta →
    si está, descargar + cargar (`cargar` hace el replace_table y devuelve
    True/False según si tocó la tabla) + mover a Histórico; si no está,
    loguear y seguir SIN error — los 3 son opcionales."""
    file_id = buscar_archivo(drive, folder_id)
    if not file_id:
        log.info('%s: no hay archivo en su carpeta (%s) esta corrida, se omite.', nombre, folder_id)
        return

    log.info('Descargando %s ...', nombre)
    ok = cargar(drive, file_id)
    if not ok:
        return  # `cargar` ya logueó por qué no se movió (ej. lectura vacía)

    if hist_folder_id:
        move_file(drive, file_id, hist_folder_id)
        log.info('%s movido a Histórico.', nombre)
    else:
        log.warning('%s cargado, pero no se movió a Histórico: falta el ID de carpeta '
                    '(*_HIST_FOLDER_ID) en .env.', nombre)


def main():
    load_dotenv()

    folder_id_fallback = os.environ.get('CARTERA_DRIVE_FOLDER_ID', '')
    sa_json      = os.environ.get('GOOGLE_SA_JSON', '')
    supabase_url = os.environ.get('SUPABASE_URL', '')
    srk          = os.environ.get('SUPABASE_SERVICE_ROLE_KEY', '')

    faltantes = [
        n for n, v in [
            ('GOOGLE_SA_JSON', sa_json),
            ('SUPABASE_URL', supabase_url),
            ('SUPABASE_SERVICE_ROLE_KEY', srk),
        ] if not v
    ]
    if faltantes:
        log.error('Variables faltantes en .env: %s', ', '.join(faltantes))
        sys.exit(1)

    # Carpeta por archivo (Parte 1) — cae a CARTERA_DRIVE_FOLDER_ID (P4) si la
    # variable específica no está seteada, para no romper el VPS mientras el
    # usuario crea las carpetas nuevas. Histórico no tiene fallback: si no
    # está configurada, simplemente no se mueve el archivo tras cargarlo.
    payu_uc_folder_id      = os.environ.get('PAYU_UC_FOLDER_ID') or folder_id_fallback
    payu_uc_hist_folder_id = os.environ.get('PAYU_UC_HIST_FOLDER_ID', '')
    ingresos_folder_id      = os.environ.get('INGRESOS_FOLDER_ID') or folder_id_fallback
    ingresos_hist_folder_id = os.environ.get('INGRESOS_HIST_FOLDER_ID', '')
    cartera_prev_folder_id      = os.environ.get('CARTERA_PREV_FOLDER_ID') or folder_id_fallback
    cartera_prev_hist_folder_id = os.environ.get('CARTERA_PREV_HIST_FOLDER_ID', '')

    if not any([payu_uc_folder_id, ingresos_folder_id, cartera_prev_folder_id]):
        log.error('Ninguna carpeta de origen configurada (ni las nuevas *_FOLDER_ID ni '
                   'CARTERA_DRIVE_FOLDER_ID como fallback).')
        sys.exit(1)

    drive = build_drive_service(sa_json)

    def _cargar_payu_uc(drive, file_id) -> bool:
        rows = read_inscrip(download_file(drive, file_id))
        if not rows:
            log.warning('Payu UC: 0 filas leídas, se omite la carga (no se toca cartera_inscrip).')
            return False
        replace_table(supabase_url, srk, 'cartera_inscrip', rows)
        return True

    def _cargar_ingresos(drive, file_id) -> bool:
        ingresos_bytes = download_file(drive, file_id).read()
        bc2576_rows = read_bancolombia_2576(io.BytesIO(ingresos_bytes))
        wompi_rows  = read_wompi(io.BytesIO(ingresos_bytes))
        stripe_rows = read_stripe_usa(io.BytesIO(ingresos_bytes))
        if not (bc2576_rows or wompi_rows or stripe_rows):
            log.warning('Ingresos PSE y PAYU: 0 filas leídas en las 3 hojas, se omite la carga '
                        '(no se tocan las tablas cartera_ingresos_*).')
            return False
        replace_table(supabase_url, srk, 'cartera_ingresos_bancolombia_2576', bc2576_rows)
        replace_table(supabase_url, srk, 'cartera_ingresos_wompi', wompi_rows)
        replace_table(supabase_url, srk, 'cartera_ingresos_stripe_usa', stripe_rows)
        return True

    def _cargar_cartera_prev(drive, file_id) -> bool:
        rows = read_cartera_preventiva(download_file(drive, file_id))
        ok = replace_cartera_preventiva_staging(supabase_url, srk, rows)
        if ok:
            carga_id = _registrar_carga_staged(supabase_url, srk, len(rows))
            log.info('Cartera Preventiva: %d fila(s) a staging, carga %s marcada "staged".',
                      len(rows), carga_id)
        return ok

    _procesar_opcional(drive, PAYU_UC_FILENAME, payu_uc_folder_id, payu_uc_hist_folder_id,
                       lambda d, f: find_file_id(d, f, PAYU_UC_FILENAME), _cargar_payu_uc)

    _procesar_opcional(drive, INGRESOS_FILENAME, ingresos_folder_id, ingresos_hist_folder_id,
                       lambda d, f: find_file_id(d, f, INGRESOS_FILENAME), _cargar_ingresos)

    _procesar_opcional(drive, CARTERA_PREV_PATTERN, cartera_prev_folder_id, cartera_prev_hist_folder_id,
                       lambda d, f: find_latest_file(d, f, CARTERA_PREV_PATTERN), _cargar_cartera_prev)

    log.info('sync_cartera.py completado.')


if __name__ == '__main__':
    main()
