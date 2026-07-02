#!/opt/matching-test/venv/bin/python3
"""
cruzar.py — calcula el cruce de cartera sobre consolidated_transactions.

Implementado hasta ahora (columnas 10-11 del diseño de 20 columnas):
  - INCP:      identification vs cartera_inscrip.numero_id → id_inscripcion
  - CORREO(2): email vs la hoja "Ingresos PSE y PAYU" correspondiente al banco
               (BANCOLOMBIA 2576 / WOMPI / STRIPE_USA), primera coincidencia
               (replica BUSCARV de Excel: la primera fila que matchea gana).

Las columnas 12-19 (CRUCE, NOMBRE, ...) todavía no están definidas y quedan NULL.
Requiere haber corrido sync_cartera.py antes (o el mismo día) para que las tablas
mirror estén al día.
"""

import logging
import os
import sys

from dotenv import load_dotenv

from utils.supabase import select_all, upsert_cruce

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%S',
    stream=sys.stdout,
)
log = logging.getLogger(__name__)


def _build_lookup(rows: list[dict], key_field: str, value_field: str, lower: bool = False) -> dict:
    """Primera coincidencia gana (replica BUSCARV de Excel)."""
    lookup = {}
    for row in rows:
        key = str(row.get(key_field) or '').strip()
        if lower:
            key = key.lower()
        if not key or key in lookup:
            continue
        lookup[key] = row.get(value_field) or ''
    return lookup


def main():
    load_dotenv()

    supabase_url = os.environ.get('SUPABASE_URL', '')
    srk          = os.environ.get('SUPABASE_SERVICE_ROLE_KEY', '')
    if not supabase_url or not srk:
        log.error('SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY no configurados.')
        sys.exit(1)

    log.info('Cargando tablas de referencia...')
    inscrip_rows = select_all(supabase_url, srk, 'cartera_inscrip',
                               select='numero_id,id_inscripcion')
    bc2576_rows  = select_all(supabase_url, srk, 'cartera_ingresos_bancolombia_2576',
                               select='referencia_1,incp')
    wompi_rows   = select_all(supabase_url, srk, 'cartera_ingresos_wompi',
                               select='email,inscrip')
    stripe_rows  = select_all(supabase_url, srk, 'cartera_ingresos_stripe_usa',
                               select='email_cliente,incp')

    lookup_inscrip = _build_lookup(inscrip_rows, 'numero_id', 'id_inscripcion')
    lookup_bc2576  = _build_lookup(bc2576_rows, 'referencia_1', 'incp')
    lookup_wompi   = _build_lookup(wompi_rows, 'email', 'inscrip', lower=True)
    lookup_stripe  = _build_lookup(stripe_rows, 'email_cliente', 'incp', lower=True)

    log.info('Referencias cargadas: inscrip=%d, bc2576=%d, wompi=%d, stripe=%d',
              len(lookup_inscrip), len(lookup_bc2576), len(lookup_wompi), len(lookup_stripe))

    log.info('Cargando consolidated_transactions...')
    transacciones = select_all(
        supabase_url, srk, 'consolidated_transactions',
        select='identification,payment_date,transaction_code_1,transaction_code_2,'
               'email,payment_method,program,phone,payment_amount,matching_key',
    )
    log.info('%d transacciones a cruzar.', len(transacciones))

    resultado = []
    for t in transacciones:
        identification = str(t.get('identification') or '').strip()
        email          = str(t.get('email') or '').strip()
        payment_method = str(t.get('payment_method') or '').upper()

        incp = lookup_inscrip.get(identification, '')

        correo_2 = ''
        if payment_method == 'BANCOLOMBIA':
            correo_2 = lookup_bc2576.get(email, '')
        elif payment_method.startswith('WOMPI'):
            correo_2 = lookup_wompi.get(email.lower(), '')
        elif payment_method == 'STRIPE_USA':
            correo_2 = lookup_stripe.get(email.lower(), '')

        resultado.append({
            'matching_key':       t.get('matching_key'),
            'identification':     t.get('identification'),
            'payment_date':       t.get('payment_date'),
            'transaction_code_1': t.get('transaction_code_1'),
            'transaction_code_2': t.get('transaction_code_2'),
            'email':              t.get('email'),
            'payment_method':     t.get('payment_method'),
            'program':            t.get('program'),
            'phone':              t.get('phone'),
            'payment_amount':     t.get('payment_amount'),
            'incp':               incp or None,
            'correo_2':           correo_2 or None,
        })

    if not resultado:
        log.info('Sin transacciones para cruzar.')
        return

    batch_size = 500
    for i in range(0, len(resultado), batch_size):
        upsert_cruce(supabase_url, srk, resultado[i:i + batch_size])

    con_incp    = sum(1 for r in resultado if r['incp'])
    con_correo2 = sum(1 for r in resultado if r['correo_2'])
    log.info('cruzar.py completado: %d filas | INCP=%d | CORREO(2)=%d',
              len(resultado), con_incp, con_correo2)


if __name__ == '__main__':
    main()
