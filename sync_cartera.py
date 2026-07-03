#!/home/y1sus/Documents/Dev/matching-test/venv/bin/python3
"""
sync_cartera.py — sincroniza los Excel de referencia del cruce de cartera a Supabase.

Lee los archivos desde la carpeta de Google Drive CARTERA_DRIVE_FOLDER_ID (por
nombre, no por ID fijo — así una actualización del Excel en Drive no requiere
tocar el .env) y reemplaza por completo el contenido de las tablas mirror:
  - cartera_inscrip                    ← Payu UC.xlsx > Inscrip
  - cartera_ingresos_bancolombia_2576  ← Ingresos PSE y PAYU.xlsx > BANCOLOMBIA 2576
  - cartera_ingresos_wompi             ← Ingresos PSE y PAYU.xlsx > WOMPI
  - cartera_ingresos_stripe_usa        ← Ingresos PSE y PAYU.xlsx > STRIPE_USA

Se corre manualmente cada vez que el equipo actualiza los Excel (o antes de cruzar.py).
"""

import io
import logging
import os
import sys

from dotenv import load_dotenv

from utils.drive import build_drive_service, find_file_id, download_pdf as download_file
from utils.excel_cartera import (
    read_inscrip, read_bancolombia_2576, read_wompi, read_stripe_usa,
)
from utils.supabase import replace_table

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%S',
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

PAYU_UC_FILENAME  = 'Payu UC.xlsx'
INGRESOS_FILENAME = 'Ingresos PSE y PAYU.xlsx'


def main():
    load_dotenv()

    folder_id    = os.environ.get('CARTERA_DRIVE_FOLDER_ID', '')
    sa_json      = os.environ.get('GOOGLE_SA_JSON', '')
    supabase_url = os.environ.get('SUPABASE_URL', '')
    srk          = os.environ.get('SUPABASE_SERVICE_ROLE_KEY', '')

    faltantes = [
        n for n, v in [
            ('CARTERA_DRIVE_FOLDER_ID', folder_id),
            ('GOOGLE_SA_JSON', sa_json),
            ('SUPABASE_URL', supabase_url),
            ('SUPABASE_SERVICE_ROLE_KEY', srk),
        ] if not v
    ]
    if faltantes:
        log.error('Variables faltantes en .env: %s', ', '.join(faltantes))
        sys.exit(1)

    drive = build_drive_service(sa_json)

    payu_uc_id  = find_file_id(drive, folder_id, PAYU_UC_FILENAME)
    ingresos_id = find_file_id(drive, folder_id, INGRESOS_FILENAME)

    faltantes_drive = [
        n for n, v in [(PAYU_UC_FILENAME, payu_uc_id), (INGRESOS_FILENAME, ingresos_id)]
        if not v
    ]
    if faltantes_drive:
        log.error('No se encontraron en la carpeta de Drive (%s): %s',
                   folder_id, ', '.join(faltantes_drive))
        sys.exit(1)

    log.info('Descargando %s ...', PAYU_UC_FILENAME)
    inscrip_rows = read_inscrip(download_file(drive, payu_uc_id))

    log.info('Descargando %s ...', INGRESOS_FILENAME)
    ingresos_bytes = download_file(drive, ingresos_id).read()
    bc2576_rows = read_bancolombia_2576(io.BytesIO(ingresos_bytes))
    wompi_rows  = read_wompi(io.BytesIO(ingresos_bytes))
    stripe_rows = read_stripe_usa(io.BytesIO(ingresos_bytes))

    replace_table(supabase_url, srk, 'cartera_inscrip', inscrip_rows)
    replace_table(supabase_url, srk, 'cartera_ingresos_bancolombia_2576', bc2576_rows)
    replace_table(supabase_url, srk, 'cartera_ingresos_wompi', wompi_rows)
    replace_table(supabase_url, srk, 'cartera_ingresos_stripe_usa', stripe_rows)

    log.info('sync_cartera.py completado.')


if __name__ == '__main__':
    main()
