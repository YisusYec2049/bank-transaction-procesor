"""Cliente Supabase: upsert a consolidated_transactions."""

import logging
from datetime import datetime

import pytz
import requests as http

log = logging.getLogger(__name__)

_ENDPOINT = '/rest/v1/consolidated_transactions?on_conflict=matching_key'
_PREFER   = 'return=minimal,resolution=merge-duplicates'


def upsert(supabase_url: str, service_role_key: str, rows: list[list]) -> None:
    """
    rows: filas normalizadas [identification, payment_date(DD-MM-YYYY), ...]
    registration_date se agrega aquí como la fecha de hoy en Bogotá.
    """
    tz_bogota = pytz.timezone('America/Bogota')
    today_iso = datetime.now(tz_bogota).strftime('%Y-%m-%d')

    payload = []
    for r in rows:
        dd, mm, yyyy = str(r[2]).split('-')
        payload.append({
            'registration_date':  today_iso,
            'identification':     r[1],
            'payment_date':       f'{yyyy}-{mm}-{dd}',
            'transaction_code_1': r[3],
            'transaction_code_2': r[4],
            'email':              r[5],
            'payment_method':     r[6],
            'program':            r[7],
            'phone':              r[8],
            'payment_amount':     r[9],
            'matching_key':       r[10],
        })

    hdrs = {
        'apikey':        service_role_key,
        'Authorization': f'Bearer {service_role_key}',
        'Content-Type':  'application/json',
        'Prefer':        _PREFER,
    }
    resp = http.post(
        f'{supabase_url}{_ENDPOINT}',
        json=payload,
        headers=hdrs,
        timeout=30,
    )
    resp.raise_for_status()
    log.info('Upsert Supabase OK: %d registros, HTTP %s.', len(payload), resp.status_code)


def upsert_cheque(supabase_url: str, service_role_key: str, banco: str, row: list) -> None:
    """
    Inserta un cheque en cheques_pendientes si no existe ya uno PENDIENTE igual.
    row: fila normalizada [identification, payment_date(DD-MM-YYYY), ..., payment_amount, matching_key]
    """
    dd, mm, yyyy = str(row[2]).split('-')
    payload = {
        'banco':          banco,
        'identification': row[1],
        'payment_amount': row[9],
        'payment_date':   f'{yyyy}-{mm}-{dd}',
        'raw_row':        {
            'transaction_code_1': row[3],
            'transaction_code_2': row[4],
            'payment_method':     row[6],
            'matching_key':       row[10],
        },
        'estado': 'PENDIENTE',
    }
    hdrs = {
        'apikey':        service_role_key,
        'Authorization': f'Bearer {service_role_key}',
        'Content-Type':  'application/json',
        'Prefer':        'return=minimal',
    }
    resp = http.post(
        f'{supabase_url}/rest/v1/cheques_pendientes',
        json=payload,
        headers=hdrs,
        timeout=30,
    )
    if resp.status_code == 409:
        return  # ya existe
    resp.raise_for_status()
    log.info('Cheque PENDIENTE registrado: %s / %s / %s', banco, row[0], row[8])
