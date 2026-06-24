"""
Parser para reportes Excel de Placetopay.

Esquema de salida normalizado (10 columnas, índices 0-9):
  [0] identification      ← Documento sin letra inicial
  [1] payment_date        ← DD-MM-YYYY
  [2] transaction_code_1  ← Referencia
  [3] transaction_code_2  ← Código autorización
  [4] email               ← Email
  [5] payment_method      ← franquicia mapeada (ej. 'Placetopay PSE')
  [6] program             ← Programa (si existe)
  [7] phone               ← Telefono (si existe)
  [8] payment_amount      ← float
  [9] matching_key        ← referencia  (único por transacción en Placetopay)
"""

import io
import re
import logging

import openpyxl

log = logging.getLogger(__name__)

HEADERS = [
    'identification', 'payment_date', 'transaction_code_1', 'transaction_code_2',
    'email', 'payment_method', 'program', 'phone', 'payment_amount', 'matching_key',
]

def _map_franchise(raw: str) -> str:
    f = raw.lower()
    if 'pse' in f:
        return 'Placetopay (PSE)'
    if 'mastercard' in f:
        return 'Placetopay (Mastercard)'
    if 'visa' in f:
        return 'Placetopay (Visa)'
    if 'american express' in f or 'amex' in f:
        return 'Placetopay (AmericanExpress)'
    return f'Placetopay ({raw})' if raw else 'PLACETOPAY'


def _find_col(headers: list[str], *names: str) -> int | None:
    for name in names:
        for i, h in enumerate(headers):
            if name in h:
                return i
    return None


def _get(row, headers: list[str], *names: str) -> str:
    idx = _find_col(headers, *names)
    if idx is None or idx >= len(row):
        return ''
    v = row[idx]
    return str(v).strip() if v is not None else ''


def parse_file(buf: io.BytesIO, filename: str = '') -> list[dict]:
    wb = openpyxl.load_workbook(buf, read_only=True, data_only=True)
    ws = wb.active
    all_rows = list(ws.iter_rows(values_only=True))
    wb.close()

    # Busca la fila de encabezado (la que tenga una celda == 'Fecha')
    header_idx = None
    for i, row in enumerate(all_rows):
        if any(str(c).strip().lower() == 'fecha' for c in row if c is not None):
            header_idx = i
            break
    if header_idx is None:
        log.warning('Placetopay: no se encontró fila de encabezado')
        return []

    headers = [str(c).strip().lower() if c is not None else '' for c in all_rows[header_idx]]
    results = []

    for row in all_rows[header_idx + 1:]:
        fecha_raw = _get(row, headers, 'fecha')
        if not fecha_raw or not re.match(r'^\d{4}-\d{2}-\d{2}', fecha_raw):
            continue

        yyyy, mm, dd = fecha_raw[:10].split('-')
        payment_date = f'{dd}-{mm}-{yyyy}'

        referencia = _get(row, headers, 'referencia')
        if not referencia:
            continue

        doc_raw = _get(row, headers, 'documento')
        identification = re.sub(r'^[A-Za-z]+\s*', '', doc_raw)

        monto_str = _get(row, headers, 'valor', 'monto', 'amount')
        try:
            monto = float(str(monto_str).replace(',', ''))
        except ValueError:
            continue
        if monto <= 0:
            continue

        franchise      = _get(row, headers, 'franquicia', 'franchise', 'medio')
        payment_method = _map_franchise(franchise)
        cod_autorizacion = _get(row, headers, 'autorizaci', 'autorización', 'autorizacion', 'cod')

        results.append({
            'identification':  identification,
            'payment_date':    payment_date,
            'referencia':      referencia,
            'cod_autorizacion': cod_autorizacion,
            'email':           _get(row, headers, 'email', 'correo'),
            'payment_method':  payment_method,
            'program':         _get(row, headers, 'programa', 'program') or None,
            'phone':           _get(row, headers, 'telefono', 'teléfono', 'phone') or None,
            'monto':           monto,
        })

    log.info('Placetopay: %d filas parseadas', len(results))
    return results


def normalize(raw_rows: list[dict]) -> list[list]:
    return [
        [
            r['identification'],        # [0]
            r['payment_date'],          # [1]
            r['referencia'],            # [2] transaction_code_1
            r['cod_autorizacion'],      # [3] transaction_code_2 = código autorización
            r['email'],                 # [4]
            r['payment_method'],        # [5]
            r.get('program', ''),       # [6]
            r.get('phone', ''),         # [7]
            r['monto'],                 # [8]
            r['referencia'],            # [9] matching_key = referencia
        ]
        for r in raw_rows
    ]


def cheque_logic(normalized_rows, _pendientes_raw):
    """Placetopay no maneja cheques."""
    return normalized_rows, [], [], []
