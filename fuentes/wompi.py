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
  [7] program             ← `ref. 2` del CSV (28 de julio; antes vacío, ver
                              más abajo). cruzar.py lo pisa con el "Proyecto"
                              de ReportePagosWompi cuando el pago está en ese
                              reporte (9.2), y nunca lo borra si no está.
  [8] phone               ← nombre del pagador (Fase 9.5; antes: ref. 2)
  [9] payment_amount      ← float
  [10] matching_key       ← id de la transaccion

Fase 9.5 del rediseño (16 de julio): `program` y `phone` estaban
intercambiados desde el 26 de junio (línea 1 de "Cambios para Consolidado"):
`program` traía el nombre del pagador en vez del programa, y de paso eso fue
lo que rompió la regla original del 13 de julio de WOMPI automático/manual
(asumía "program vacío = automático", pero nunca estaba vacío). Esa fase dejó
`program` vacío a propósito y descartó `ref. 2` "porque no tenía otro
destino".

28 de julio — `ref. 2` vuelve, ahora a `program`. La fase 9.5 asumió que el
"Proyecto" del ReportePagosWompi cubría el programa de todo WOMPI, y no: ese
reporte solo trae los pagos por link PERSONAL. Los del link PÚBLICO no están
ahí, así que llegaban a Excepciones sin nombre, sin comprobante y sin
programa — y son justo los que alguien tiene que identificar a mano.

Las dos fuentes son complementarias, medido sobre los CSV reales del 17 al 20
de julio: de los pagos por link personal, CERO traen `ref. 2`; de los del link
público, TODOS (56/56 el 17 de julio). Por eso conviven sin pisarse — el
reporte manda donde llega, y `ref. 2` llena el resto.

Ojo con qué es este dato: **texto libre que teclea quien paga** ("Programa y
Detalle Pago" en el formulario del link). Junto a "Diplomado en SARLAFT"
aparece "Inscripción", "Pago mitad cuota" o "PAGO U DE CATALUÑA". Sirve para
identificar el pago, no como nombre oficial del programa.
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
            'ref_2':           r.get('ref. 2', ''),
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
            r['ref_2'],                        # [7]  program (ver docstring, 28 de julio)
            r['nombre'],                       # [8]  phone (Fase 9.5: nombre del pagador)
            r['monto'],                        # [9]  payment_amount
            r['id_tx'],                        # [10] matching_key
        ]
        for r in raw_rows
    ]


def cheque_logic(normalized_rows: list[list]) -> tuple[list, list]:
    """WOMPI no maneja cheques."""
    return normalized_rows, []
