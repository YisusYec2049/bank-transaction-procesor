#!/home/y1sus/Documents/Dev/matching-test/venv/bin/python3
"""
sync_cartera.py — sincroniza los Excel de referencia del cruce de cartera a Supabase.

Lee de rutas locales (PAYU_UC_XLSX_PATH, INGRESOS_PSE_PAYU_XLSX_PATH) y reemplaza
por completo el contenido de las tablas mirror en Supabase:
  - cartera_inscrip                    ← Payu UC.xlsx > Inscrip
  - cartera_ingresos_bancolombia_2576  ← Ingresos PSE y PAYU.xlsx > BANCOLOMBIA 2576
  - cartera_ingresos_wompi             ← Ingresos PSE y PAYU.xlsx > WOMPI
  - cartera_ingresos_stripe_usa        ← Ingresos PSE y PAYU.xlsx > STRIPE_USA

Se corre manualmente cada vez que el equipo actualiza los Excel (o antes de cruzar.py).
"""

import logging
import os
import sys

from dotenv import load_dotenv

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


def main():
    load_dotenv()

    payu_uc_path  = os.environ.get('PAYU_UC_XLSX_PATH', '')
    ingresos_path = os.environ.get('INGRESOS_PSE_PAYU_XLSX_PATH', '')
    supabase_url  = os.environ.get('SUPABASE_URL', '')
    srk           = os.environ.get('SUPABASE_SERVICE_ROLE_KEY', '')

    faltantes = [
        n for n, v in [
            ('PAYU_UC_XLSX_PATH', payu_uc_path),
            ('INGRESOS_PSE_PAYU_XLSX_PATH', ingresos_path),
            ('SUPABASE_URL', supabase_url),
            ('SUPABASE_SERVICE_ROLE_KEY', srk),
        ] if not v
    ]
    if faltantes:
        log.error('Variables faltantes en .env: %s', ', '.join(faltantes))
        sys.exit(1)

    if not os.path.isfile(payu_uc_path):
        log.error('No existe el archivo: %s', payu_uc_path)
        sys.exit(1)
    if not os.path.isfile(ingresos_path):
        log.error('No existe el archivo: %s', ingresos_path)
        sys.exit(1)

    log.info('Leyendo %s ...', payu_uc_path)
    inscrip_rows = read_inscrip(payu_uc_path)

    log.info('Leyendo %s ...', ingresos_path)
    bc2576_rows = read_bancolombia_2576(ingresos_path)
    wompi_rows  = read_wompi(ingresos_path)
    stripe_rows = read_stripe_usa(ingresos_path)

    replace_table(supabase_url, srk, 'cartera_inscrip', inscrip_rows)
    replace_table(supabase_url, srk, 'cartera_ingresos_bancolombia_2576', bc2576_rows)
    replace_table(supabase_url, srk, 'cartera_ingresos_wompi', wompi_rows)
    replace_table(supabase_url, srk, 'cartera_ingresos_stripe_usa', stripe_rows)

    log.info('sync_cartera.py completado.')


if __name__ == '__main__':
    main()
