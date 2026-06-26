"""
Parser para reportes de PayU (requiere DOS archivos).

Archivo 1 — PayU:     TSV (tab-sep) — filas donde DESCRIPCION empieza con 'SALES'
  Columnas: FECHA, DOCUMENTO, DESCRIPCION, CREDITOS, DEBITOS, NUEVO SALDO, ...
  Patrón: SALES [programa_docId_timestamp]

Archivo 2 — Moneda:   CSV punto y coma — filas donde Estado == 'APPROVED'
  Columnas incluyen: Id, Referencia, Estado, Email del comprador,
                     Documento del comprador, Moneda transacción, Valor transacción

JOIN: PayU.DOCUMENTO == Moneda.Id

  [0] identification      ← Segundo segmento de SALES [prog_docId_ts] (PayU)
  [1] payment_date        ← DD-MM-YYYY (de FECHA en PayU)
  [2] transaction_code_1  ← DOCUMENTO UUID (PayU)
  [3] transaction_code_2  ← '{Valor procesamiento} {Moneda procesamiento}' (Moneda)
  [4] email               ← Email del comprador (Moneda)
  [5] payment_method      ← 'PAYU'
  [6] program             ← primer segmento de Referencia (ej. 'DSARLAFT')
  [7] phone               ← ''
  [8] payment_amount      ← Valor procesamiento (Moneda)
  [9] matching_key        ← DOCUMENTO UUID (PayU)
"""

import csv
import datetime
import io
import logging
import re

from utils.parser import parse_valor

log = logging.getLogger(__name__)

HEADERS = [
    'VAL',
    'identification', 'payment_date', 'transaction_code_1', 'transaction_code_2',
    'email', 'payment_method', 'program', 'phone', 'payment_amount', 'matching_key',
]

_SALES_RE = re.compile(r'SALES\s*\[([^\]]+)\]', re.IGNORECASE)


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


def _parse_fecha(s: str) -> str | None:
    s = s.strip()[:10]
    try:
        yyyy, mm, dd = s.split('-')
        return f'{dd}-{mm}-{yyyy}'
    except ValueError:
        return None


# ── Lectura de archivos ────────────────────────────────────────────────────────

def _read_tsv(buf: io.BytesIO) -> tuple[list[str], list[list[str]]]:
    """Lee un TSV, busca el encabezado FECHA/DOCUMENTO y retorna (headers, data_rows)."""
    buf.seek(0)
    text = buf.read().decode('latin-1', errors='replace')
    reader = csv.reader(io.StringIO(text), delimiter='\t')
    rows   = list(reader)

    header_idx = None
    for i, row in enumerate(rows):
        if row and 'FECHA' in str(row[0]).upper() and len(row) > 2:
            header_idx = i
            break
    if header_idx is None:
        return [], []

    headers = [c.strip().upper() for c in rows[header_idx]]
    return headers, rows[header_idx + 1:]


def _read_moneda_csv(buf: io.BytesIO) -> tuple[list[str], list[list[str]]]:
    """Lee el CSV punto-y-coma de Moneda."""
    buf.seek(0)
    text   = buf.read().decode('latin-1', errors='replace')
    reader = csv.reader(io.StringIO(text), delimiter=';')
    rows   = list(reader)
    if not rows:
        return [], []
    # Primera fila = cabecera
    headers = [c.strip().lower() for c in rows[0]]
    return headers, rows[1:]


# ── Parser principal ──────────────────────────────────────────────────────────

def parse_file(payu_buf: io.BytesIO, moneda_buf: io.BytesIO,
               payu_filename: str = 'payu.xls',
               moneda_filename: str = 'moneda.csv') -> list[dict]:

    # ── Archivo PayU ──────────────────────────────────────────────────────────
    payu_headers, payu_rows = _read_tsv(payu_buf)
    if not payu_headers:
        log.warning('PayU: no se encontró encabezado TSV.')
        return []

    payu_data: dict[str, dict] = {}  # uuid → datos
    for row in payu_rows:
        if not row or len(row) < 4:
            continue
        descripcion = _get(row, payu_headers, 'DESCRIPCION')
        m = _SALES_RE.search(descripcion)
        if not m:
            continue

        referencia  = m.group(1)          # DSARLAFT_101605721_2026612183440
        partes      = referencia.split('_')
        programa    = partes[0] if len(partes) > 0 else ''
        doc_sales   = partes[1] if len(partes) > 1 else ''  # identificación del pagador

        documento   = _get(row, payu_headers, 'DOCUMENTO')   # UUID
        fecha_raw   = _get(row, payu_headers, 'FECHA')
        fecha       = _parse_fecha(fecha_raw)
        creditos    = parse_valor(_get(row, payu_headers, 'CREDITOS'))

        if not documento or not fecha or creditos is None or creditos <= 0:
            continue

        payu_data[documento] = {
            'referencia':   referencia,
            'programa':     programa,
            'identification': doc_sales,
            'fecha':        fecha,
            'creditos':     creditos,
        }

    log.info('PayU: %d transacciones SALES leídas.', len(payu_data))

    # ── Archivo Moneda ────────────────────────────────────────────────────────
    moneda_headers, moneda_rows = _read_moneda_csv(moneda_buf)

    moneda_data: dict[str, dict] = {}  # uuid → datos
    for row in moneda_rows:
        estado = _get(row, moneda_headers, 'estado').upper()
        if estado != 'APPROVED':
            continue

        uid    = _get(row, moneda_headers, 'id ')  # columna 'Id' con posible espacio
        if not uid:
            # intento por índice (columna 3 = Id en el CSV observado)
            uid = row[3].strip() if len(row) > 3 else ''

        ident       = (_get(row, moneda_headers, 'documento del comprador')
                       or _get(row, moneda_headers, 'tarjeta documento'))
        email       = _get(row, moneda_headers, 'email del comprador')
        valor_proc  = _get(row, moneda_headers, 'valor procesamiento')
        moneda_proc = _get(row, moneda_headers, 'moneda procesamiento')

        if not uid:
            continue

        moneda_data[uid] = {
            'identification': ident,
            'email':          email,
            'code2':          f'{valor_proc} {moneda_proc}'.strip(),
            'monto':          parse_valor(valor_proc),
        }

    log.info('PayU Moneda: %d APPROVED leídos.', len(moneda_data))

    # ── JOIN por UUID ─────────────────────────────────────────────────────────
    results = []
    for uuid, pd in payu_data.items():
        md = moneda_data.get(uuid, {})
        results.append({
            'identification': pd['identification'],
            'payment_date':   pd['fecha'],
            'tx_code_1':      uuid,
            'code2':          md.get('code2', ''),
            'email':          md.get('email', ''),
            'programa':       pd['programa'],
            'monto':          md.get('monto') or pd['creditos'],
            'matching_key':   uuid,
        })

    log.info('PayU: %d transacciones tras JOIN', len(results))
    return results


def normalize(raw_rows: list[dict]) -> list[list]:
    return [
        [
            '',                   # [0]  VAL
            r['identification'],  # [1]
            r['payment_date'],    # [2]
            r['tx_code_1'],       # [3]
            r['code2'],           # [4]
            r['email'],           # [5]
            'PAYU',               # [6]
            r['programa'],        # [7]
            '',                   # [8]
            r['monto'],           # [9]
            r['matching_key'],    # [10]
        ]
        for r in raw_rows
    ]


def cheque_logic(normalized_rows, _pendientes_raw):
    """PayU no maneja cheques."""
    return normalized_rows, [], [], []
