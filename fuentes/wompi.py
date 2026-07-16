"""
Parser para reportes CSV de WOMPI.

Columnas esperadas (headers en minúsculas):
  id de la transaccion, fecha, referencia, monto, moneda, medio de pago,
  email del pagador, nombre del pagador, telefono del pagador,
  id conciliacion, documento del pagador, tipo de documento, ref. 2

  [0] VAL                 ← id de la transaccion
  [1] identification      ← documento del pagador
  [2] payment_date        ← DD-MM-YYYY
  [3] transaction_code_1  ← referencia
  [4] transaction_code_2  ← id conciliacion
  [5] email               ← email del pagador
  [6] payment_method      ← 'WOMPI {medio de pago}'
  [7] program             ← nombre del pagador
  [8] phone               ← ref. 2
  [9] payment_amount      ← float
  [10] matching_key       ← id de la transaccion
"""

import csv
import io
import logging

log = logging.getLogger(__name__)

HEADERS = [
    'VAL',
    'identification', 'payment_date', 'transaction_code_1', 'transaction_code_2',
    'email', 'payment_method', 'program', 'phone', 'payment_amount', 'matching_key',
]


def parse_file(buf: io.BytesIO, filename: str = '') -> list[dict]:
    text   = buf.read().decode('utf-8', errors='replace')
    reader = csv.DictReader(io.StringIO(text))
    results = []

    for row in reader:
        r = {k.strip().lower(): str(v).strip() for k, v in row.items()}

        id_tx = r.get('id de la transaccion') or r.get('id de la transacción') or ''
        if not id_tx:
            continue

        fecha_raw = r.get('fecha', '')[:10]
        if not fecha_raw:
            continue
        # WOMPI fecha: YYYY-MM-DD HH:MM:SS
        try:
            yyyy, mm, dd = fecha_raw.split('-')
            payment_date = f'{dd}-{mm}-{yyyy}'
        except ValueError:
            continue

        monto_str = r.get('monto', '0').replace(',', '')
        try:
            monto = float(monto_str)
        except ValueError:
            continue
        if monto <= 0:
            continue

        medio = r.get('medio de pago', '')
        results.append({
            'id_tx':           id_tx,
            'payment_date':    payment_date,
            'referencia':      r.get('referencia', ''),
            'id_conciliacion': r.get('id conciliacion', '') or r.get('id conciliación', ''),
            'email':           r.get('email del pagador', ''),
            'medio':           medio,
            'nombre':          r.get('nombre del pagador', ''),
            'ref2':            r.get('ref. 2', '') or r.get('ref 2', ''),
            'documento':       r.get('documento del pagador', ''),
            'monto':           monto,
        })

    log.info('WOMPI: %d filas parseadas', len(results))
    return results


def normalize(raw_rows: list[dict]) -> list[list]:
    return [
        [
            r['id_tx'],                        # [0]  VAL
            r['documento'],                    # [1]  identification
            r['payment_date'],                 # [2]
            r['referencia'],                   # [3]  transaction_code_1
            r['id_conciliacion'],              # [4]  transaction_code_2
            r['email'],                        # [5]
            f"WOMPI {r['medio']}".strip(),     # [6]  payment_method
            r['nombre'],                       # [7]  program
            r['ref2'],                         # [8]  phone
            r['monto'],                        # [9]  payment_amount
            r['id_tx'],                        # [10] matching_key
        ]
        for r in raw_rows
    ]


def cheque_logic(normalized_rows: list[list]) -> tuple[list, list]:
    """WOMPI no maneja cheques."""
    return normalized_rows, []
