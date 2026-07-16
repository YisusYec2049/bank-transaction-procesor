"""
Parser para extractos CSV (punto y coma) de Colpatria.

Filtros:
  - Naturaleza == 'C'
  - Identificación no contiene NIT_UNIVERSIDAD (901032802)

  [0] identification      ← Identificación
  [1] payment_date        ← DD-MM-YYYY
  [2] transaction_code_1  ← Descripción motivo
  [3] transaction_code_2  ← ''
  [4] email               ← ''
  [5] payment_method      ← 'COLPATRIA'
  [6] program             ← ''
  [7] phone               ← ''
  [8] payment_amount      ← Valor Crédito (float)
  [9] matching_key        ← {DD/MM/YYYY}_{valor_js}_{identificacion}

Los cheques se detectan por 'CHEQUE' en la descripción (transaction_code_1) y
se apartan del proceso por completo — nunca entran a consolidated_transactions
ni al cruce (ver pagos_apartados en procesar_todos.py).
"""

import csv
import io
import logging
import re

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
    return str(row[idx]).strip()


def parse_file(buf: io.BytesIO, filename: str = '') -> list[dict]:
    text   = buf.read().decode('latin-1', errors='replace')
    reader = csv.reader(io.StringIO(text), delimiter=';')
    rows   = list(reader)
    if not rows:
        return []

    # Busca encabezado (fila que tenga 'fecha')
    header_idx = 0
    for i, row in enumerate(rows):
        if any('fecha' in str(c).lower() for c in row):
            header_idx = i
            break

    headers = [c.strip().lower() for c in rows[header_idx]]
    results = []

    for row in rows[header_idx + 1:]:
        if len(row) < 3:
            continue

        naturaleza = _get(row, headers, 'naturaleza').upper()
        if naturaleza != 'C':
            continue

        identificacion = _get(row, headers, 'identificaci')  # cubre "identificación"
        if NIT_UNIVERSIDAD in identificacion:
            continue

        fecha_raw = _get(row, headers, 'fecha')
        fecha = _parse_fecha(fecha_raw)
        if not fecha:
            continue

        valor_raw = _get(row, headers, 'valor crédito', 'valor credito', 'crédito', 'credito', 'valor')
        valor = parse_valor(valor_raw)
        if valor is None or valor <= 0:
            continue

        descripcion = _get(row, headers, 'descripci', 'concepto', 'descripcion')
        referencia  = _get(row, headers, 'referencia', 'ref')
        is_cheque   = 'CHEQUE' in descripcion.upper()

        # fecha en matching_key con formato DD/MM/YYYY (barra, igual que Bancolombia)
        dd, mm, yyyy = fecha.split('-')
        fecha_slash  = f'{dd}/{mm}/{yyyy}'
        matching_key = f'{fecha_slash}_{valor_str(valor)}_{identificacion}'

        results.append({
            'identification': identificacion,
            'payment_date':   fecha,
            'descripcion':    descripcion,
            'referencia':     referencia,
            'valor':          valor,
            'matching_key':   matching_key,
            'is_cheque':      is_cheque,
        })

    log.info('Colpatria: %d filas parseadas', len(results))
    return results


def _parse_fecha(s: str) -> str | None:
    """Convierte DD/MM/YYYY, DD-MM-YYYY o YYYY-MM-DD a DD-MM-YYYY."""
    s = s.strip()
    for sep in ('/', '-'):
        parts = s.split(sep)
        if len(parts) == 3:
            a, b, c = parts
            if len(a) == 4:  # YYYY-MM-DD
                return f'{c}-{b}-{a}'
            if len(c) == 4:  # DD/MM/YYYY
                return f'{a}-{b}-{c}'
    return None


def normalize(raw_rows: list[dict]) -> list[list]:
    return [
        [
            '',                   # [0]  VAL
            r['identification'],  # [1]
            r['payment_date'],    # [2]
            r['descripcion'],     # [3]  transaction_code_1 = Descripción motivo
            '',                   # [4]
            '',                   # [5]
            'COLPATRIA',          # [6]
            '',                   # [7]
            '',                   # [8]
            r['valor'],           # [9]
            r['matching_key'],    # [10]
        ]
        for r in raw_rows
    ]


def cheque_logic(normalized_rows: list[list]) -> tuple[list, list]:
    """Separa cheques del resto. Los cheques se apartan del proceso por
    completo (nunca entran al consolidado ni al cruce — ver pagos_apartados
    en procesar_todos.py). Antes se comparaba por error contra payment_date
    (índice 2) en vez de transaction_code_1 (índice 3, la descripción), así
    que ningún cheque se detectaba nunca (bug de Fase 1.1)."""
    normales = [r for r in normalized_rows if 'CHEQUE' not in str(r[3]).upper()]
    cheques  = [r for r in normalized_rows if 'CHEQUE'     in str(r[3]).upper()]
    return normales, cheques
