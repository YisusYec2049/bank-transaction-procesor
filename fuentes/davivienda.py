"""
Parser para extractos de Davivienda (CSV con punto y coma o Excel).

Filtros:
  - Transacción == 'Nota Crédito'
  - ID Origen/Destino no contiene NIT_UNIVERSIDAD (901032802)

  [0] identification      ← Documento
  [1] payment_date        ← DD-MM-YYYY
  [2] transaction_code_1  ← Descripción
  [3] transaction_code_2  ← Referencia 1
  [4] email               ← ID Origen/Destino (NIT del originador)
  [5] payment_method      ← 'DAVIVIENDA'
  [6] program             ← ''
  [7] phone               ← ''
  [8] payment_amount      ← Valor (float)
  [9] matching_key        ← {DD/MM/YYYY}_{documento}_{referencia1}

Los cheques se detectan por 'CHEQUE' en la descripción.
"""

import csv
import datetime
import io
import logging

import openpyxl

from utils.parser import parse_valor, valor_str

log = logging.getLogger(__name__)

NIT_UNIVERSIDAD = '901032802'

HEADERS = [
    'VAL',
    'identification', 'payment_date', 'transaction_code_1', 'transaction_code_2',
    'email', 'payment_method', 'program', 'phone', 'payment_amount', 'matching_key',
]


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


def _parse_fecha(raw) -> str | None:
    """Acepta datetime, serial Excel (40000-60000) o strings DD/MM/YYYY / YYYY-MM-DD."""
    if isinstance(raw, (datetime.date, datetime.datetime)):
        return raw.strftime('%d-%m-%Y')
    if isinstance(raw, (int, float)) and 40000 <= raw <= 60000:
        dt = datetime.date(1899, 12, 30) + datetime.timedelta(days=int(raw))
        return dt.strftime('%d-%m-%Y')
    s = str(raw).strip()
    for sep in ('/', '-'):
        parts = s.split(sep)
        if len(parts) == 3:
            a, b, c = parts
            if len(a) == 4:
                return f'{c}-{b}-{a}'   # YYYY-MM-DD → DD-MM-YYYY
            if len(c) == 4:
                return f'{a}-{b}-{c}'   # DD/MM/YYYY → DD-MM-YYYY
    return None


def _parse_rows(rows, headers: list[str]) -> list[dict]:
    results = []
    for row in rows:
        transaccion = _get(row, headers, 'transacci').lower()
        if 'nota cr' not in transaccion:  # 'nota crédito' / 'nota credito'
            continue

        id_origen = _get(row, headers, 'id origen', 'origen/destino', 'origen')
        if NIT_UNIVERSIDAD in id_origen:
            continue

        fecha_idx = _find_col(headers, 'fecha')
        raw_fecha = row[fecha_idx] if fecha_idx is not None and fecha_idx < len(row) else None
        fecha = _parse_fecha(raw_fecha)
        if not fecha:
            continue

        documento   = _get(row, headers, 'documento', 'id documento')
        referencia1 = _get(row, headers, 'referencia 1', 'referencia1', 'ref 1', 'referencia')
        valor_raw   = _get(row, headers, 'valor', 'monto', 'crédito', 'credito')
        valor       = parse_valor(valor_raw)
        if valor is None or valor <= 0:
            continue

        descripcion = _get(row, headers, 'descripci', 'concepto')
        is_cheque   = 'CHEQUE' in descripcion.upper()

        dd, mm, yyyy = fecha.split('-')
        fecha_slash  = f'{dd}/{mm}/{yyyy}'
        matching_key = f'{fecha_slash}_{documento}_{referencia1}'

        results.append({
            'identification': documento,
            'payment_date':   fecha,
            'descripcion':    descripcion,
            'referencia1':    referencia1,
            'id_origen':      id_origen,
            'valor':          valor,
            'matching_key':   matching_key,
            'is_cheque':      is_cheque,
        })
    return results


def parse_file(buf: io.BytesIO, filename: str = '') -> list[dict]:
    if filename.lower().endswith(('.xlsx', '.xls')):
        return _from_excel(buf)
    return _from_csv(buf)


def _from_excel(buf: io.BytesIO) -> list[dict]:
    wb = openpyxl.load_workbook(buf, read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()

    header_idx = None
    for i, row in enumerate(rows):
        if any(str(c).strip().lower() in ('transacción', 'transaccion') for c in row if c is not None):
            header_idx = i
            break
    if header_idx is None:
        log.warning('Davivienda Excel: no se encontró encabezado')
        return []

    headers = [str(c).strip().lower() if c is not None else '' for c in rows[header_idx]]
    result  = _parse_rows(rows[header_idx + 1:], headers)
    log.info('Davivienda Excel: %d filas parseadas', len(result))
    return result


def _from_csv(buf: io.BytesIO) -> list[dict]:
    text   = buf.read().decode('latin-1', errors='replace')
    reader = csv.reader(io.StringIO(text), delimiter=';')
    rows   = list(reader)
    if not rows:
        return []

    header_idx = 0
    for i, row in enumerate(rows):
        if any('transacci' in str(c).lower() for c in row):
            header_idx = i
            break

    headers = [c.strip().lower() for c in rows[header_idx]]
    result  = _parse_rows(rows[header_idx + 1:], headers)
    log.info('Davivienda CSV: %d filas parseadas', len(result))
    return result


def normalize(raw_rows: list[dict]) -> list[list]:
    return [
        [
            '',                   # [0]  VAL
            r['identification'],  # [1]
            r['payment_date'],    # [2]
            r['descripcion'],     # [3]  transaction_code_1 = Descripción
            r['referencia1'],     # [4]  transaction_code_2 = Referencia 1
            r['id_origen'],       # [5]  email = NIT originador
            'DAVIVIENDA',         # [6]
            '',                   # [7]
            '',                   # [8]
            r['valor'],           # [9]
            r['matching_key'],    # [10]
        ]
        for r in raw_rows
    ]


def cheque_logic(normalized_rows, _pendientes_raw):
    """
    Mismo comportamiento que Colpatria: cheques van al consolidado
    Y se registran en Supabase cheques_pendientes.
    """
    normales = [r for r in normalized_rows if 'CHEQUE' not in str(r[2]).upper()]
    cheques  = [r for r in normalized_rows if 'CHEQUE'     in str(r[2]).upper()]
    return normales, [], cheques, []
