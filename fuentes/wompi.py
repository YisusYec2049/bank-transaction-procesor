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
  [7] program             ← vacío (Fase 9.5, 16 de julio; antes: nombre del
                              pagador — bug desde el 26 de junio, ver más
                              abajo). Solo cruzar.py lo llena, con el
                              "Proyecto" de ReportePagosWompi, y solo para
                              filas WOMPI LINK (9.2) — el resto queda vacío.
  [8] phone               ← nombre del pagador (Fase 9.5; antes: ref. 2)
  [9] payment_amount      ← float
  [10] matching_key       ← id de la transaccion

Fase 9.5 del rediseño (16 de julio): `program` y `phone` estaban
intercambiados desde el 26 de junio (línea 1 de "Cambios para Consolidado"):
`program` traía el nombre del pagador en vez del programa, y de paso eso fue
lo que rompió la regla original del 13 de julio de WOMPI automático/manual
(asumía "program vacío = automático", pero nunca estaba vacío). `program`
queda vacío a propósito hasta que cruzar.py lo llena desde el reporte —
`ref. 2` deja de usarse (no tenía otro destino en el diseño de columnas
15-25).
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
            '',                                # [7]  program (Fase 9.5: lo llena cruzar.py)
            r['nombre'],                       # [8]  phone (Fase 9.5: nombre del pagador)
            r['monto'],                        # [9]  payment_amount
            r['id_tx'],                        # [10] matching_key
        ]
        for r in raw_rows
    ]


def cheque_logic(normalized_rows: list[list]) -> tuple[list, list]:
    """WOMPI no maneja cheques."""
    return normalized_rows, []
