"""
Parser y normalizador para extractos PDF de Bancolombia cuenta 2833.

Diferencias respecto a 2576:
  - payment_method = 'PREBANCOLOMBIA'
  - _FIJAS incluye IVA COMISION REC CORRESPONSAL, COMISION REC CORRESPONSAL
  - _ABIERTAS incluye PAGO DE PROV FONDO NACIONAL
  - DESCRIPCIONES_ELIMINAR usa 'IVA' amplio en vez de 'IVA BOTON'/'IVA COMISION'
"""

import io
import re
import logging
from collections import defaultdict

import pdfplumber

from utils.parser import parse_valor, valor_str, norm_valor_str

log = logging.getLogger(__name__)

# ── Filtros ───────────────────────────────────────────────────────────────────

DESCRIPCIONES_ELIMINAR = frozenset([
    'ABONO', 'COMISION', 'RTE FUENTE', 'RTE ICA', 'IMPTO GOBIERNO',
    'VALOR IVA', 'IVA', 'RETENCION EN LA FUENTE',
    'TRASL ENTRE FONDOS', 'COMIS TRASLADO', 'PAGO DE PROV BANCOLOMBIA SA',
    'PAGO VIRTUAL PSE', 'BOTON',
])

_FIJAS = [
    'ABONO BRUTO AMEX', 'ABONO BRUTO MASTER', 'ABONO BRUTO VISA',
    'ABONO INTERESES AHORROS',
    'COMISION AMEX', 'COMISION MASTER', 'COMISION VISA',
    'COMISION REC CORRESPONSAL',
    'CONSIG LOCAL REFEREN EFECTIVO', 'CONSIG NAL REFERENCIA CHEQUE',
    'CONSIG LOCAL REFEREN CHEQUE', 'CONSIG LOCAL REFERENCIA CHEQUE',
    'CONSIG NACIONAL REFEREN CHEQUE', 'CONSIG NACIONAL REFERENCIA CHEQUE',
    'IMPTO GOBIERNO 4X1000',
    'IVA COMISION REC CORRESPONSAL',
    'PAGO DE PROV BANCOLOMBIA SA', 'PAGO DE PROV CONSTRUCCIONES',
    'PAGO DE PROV PROTECCION SA',
    'PAGO VIRTUAL PSE', 'RECAUDO SUCURSAL VIRTUAL', 'RETENCION EN LA FUENTE',
    'RTE FUENTE AMEX', 'RTE FUENTE MASTER', 'RTE FUENTE VISA',
    'RTE ICA AMEX', 'RTE ICA MASTER', 'RTE ICA VISA', 'TRANSF BOTON',
    'TRANSFERENCIA CTA SUC VIRTUAL', 'TRANSFERENCIA DESDE NEQUI',
]

_ABIERTAS = [
    'PAGO DE PROV FONDO NACIONAL',
    'PAGO DE TERC', 'PAGO DE PROV', 'PAGO INTERBANC',
    'PAGO LLAVE', 'PAGO QR', 'PAGO PSE',
]

_ABIERTAS_SET    = frozenset(_ABIERTAS)
_TIPOS_ORDENADOS = sorted(_FIJAS + _ABIERTAS, key=len, reverse=True)

# ── Expresiones regulares ─────────────────────────────────────────────────────

_RE_FECHA      = re.compile(r'^\d{4}/\d{2}/\d{2}$')
_RE_FECHA_LINE = re.compile(r'^(\d{4}/\d{2}/\d{2})\s+(.*)')
_RE_VALOR      = re.compile(r'([-]?[\d,]+\.\d{2})$')
_RE_REF        = re.compile(r'\b\d{6,}\b')

# ── Cabeceras de hojas ────────────────────────────────────────────────────────

HEADERS = [
    'identification', 'payment_date', 'transaction_code_1', 'transaction_code_2',
    'email', 'payment_method', 'program', 'phone', 'payment_amount',
    'matching_key',
]

CHEQUES_HEADERS = HEADERS + ['ESTADO']


# ── Funciones internas ────────────────────────────────────────────────────────

def _separar_sucursal_desc(texto: str) -> tuple[str, str, str]:
    upper = texto.upper()
    for tipo in _TIPOS_ORDENADOS:
        idx = upper.find(tipo)
        if idx == -1:
            continue
        sucursal = texto[:idx].strip()
        resto    = texto[idx + len(tipo):]
        if tipo in _ABIERTAS_SET:
            m = _RE_REF.search(resto)
            if m:
                nombre     = resto[:m.start()].strip()
                resto_refs = resto[m.start():].strip()
            else:
                nombre     = resto.strip()
                resto_refs = ''
            desc = f'{tipo} {nombre}'.strip() if nombre else tipo
            return sucursal, desc, resto_refs
        return sucursal, tipo, resto.strip()
    return '', texto.strip().upper(), ''


def _extraer_refs(texto: str) -> tuple[str, str, str]:
    if not texto:
        return '', '', ''
    matches = _RE_REF.findall(texto)
    if not matches:
        parts = texto.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isupper() and parts[0].isalpha():
            return parts[1].strip(), '', ''
        return texto.strip(), '', ''
    return (
        matches[0] if len(matches) > 0 else '',
        matches[1] if len(matches) > 1 else '',
        matches[2] if len(matches) > 2 else '',
    )


def _build_sucursal_lookup(pdf_bytes: io.BytesIO) -> dict:
    lookup: dict = {}
    with pdfplumber.open(pdf_bytes) as pdf:
        for page in pdf.pages:
            lines: dict = defaultdict(list)
            for w in page.extract_words():
                y = round(w['top'] / 2) * 2
                lines[y].append(w)
            for y_words in lines.values():
                fecha_ws = [w for w in y_words if w['x0'] < 72 and _RE_FECHA.match(w['text'])]
                suc_ws   = sorted([w for w in y_words if 240 <= w['x0'] < 329], key=lambda w: w['x0'])
                valor_ws = sorted([w for w in y_words if w['x0'] >= 526], key=lambda w: w['x0'])
                if not fecha_ws or not valor_ws:
                    continue
                try:
                    anio, mes, dia = fecha_ws[0]['text'].split('/')
                    fecha = f'{dia}/{mes}/{anio}'
                except ValueError:
                    continue
                valor_text = ' '.join(w['text'] for w in valor_ws).strip()
                m = _RE_VALOR.search(valor_text)
                if not m:
                    continue
                v = parse_valor(m.group(1))
                if v is None:
                    continue
                if suc_ws:
                    key = (fecha, v)
                    if key not in lookup:
                        lookup[key] = ' '.join(w['text'] for w in suc_ws)
    return lookup


# ── API pública ───────────────────────────────────────────────────────────────

def parse_pdf(pdf_bytes: io.BytesIO) -> list[dict]:
    suc_lookup = _build_sucursal_lookup(pdf_bytes)
    pdf_bytes.seek(0)

    with pdfplumber.open(pdf_bytes) as pdf:
        texto = '\n'.join(p.extract_text() or '' for p in pdf.pages)

    lineas   = [l.strip() for l in texto.split('\n') if l.strip()]
    en_tabla = False
    i        = 0
    filas    = []

    while i < len(lineas):
        linea = lineas[i]

        if linea.startswith('FECHA') and 'DESCRIPCIÓN' in linea:
            en_tabla = True
            i += 1
            continue

        if not en_tabla or linea.startswith('Página') or linea.startswith('Saldo'):
            i += 1
            continue

        m_date = _RE_FECHA_LINE.match(linea)
        if m_date:
            date_str       = m_date.group(1)
            anio, mes, dia = date_str.split('/')
            fecha = f'{dia}/{mes}/{anio}'
            resto = m_date.group(2).strip()

            while (not _RE_VALOR.search(resto)
                   and i + 1 < len(lineas)
                   and not _RE_FECHA_LINE.match(lineas[i + 1])):
                i += 1
                resto = f'{resto} {lineas[i]}'

            m = _RE_VALOR.search(resto)
            if not m:
                i += 1
                continue

            v = parse_valor(m.group(1))
            if v is None:
                i += 1
                continue

            antes_valor = resto[:resto.rfind(m.group(1))].strip()
            _, desc, resto_refs = _separar_sucursal_desc(antes_valor)
            ref1, ref2, documento = _extraer_refs(resto_refs)

            sucursal = suc_lookup.get((fecha, v), '')
            if sucursal:
                suf = ' ' + sucursal
                if desc.upper().endswith(suf.upper()):
                    desc = desc[:len(desc) - len(suf)].strip()

            filas.append({
                'fecha':       fecha,
                'descripcion': desc,
                'sucursal':    sucursal,
                'ref1':        ref1,
                'ref2':        ref2,
                'documento':   documento,
                'valor':       v,
            })

        i += 1

    validas = [
        f for f in filas
        if not any(d in f['descripcion'].upper() for d in DESCRIPCIONES_ELIMINAR)
        and f['valor'] >= 0
    ]

    seen, resultado = set(), []
    for f in validas:
        key = f'{f["fecha"]}_{f["descripcion"]}_{f["valor"]}'
        if key not in seen:
            seen.add(key)
            resultado.append(f)

    log.info('parse_pdf 2833: %d brutas → %d tras filtros y dedup', len(filas), len(resultado))
    return resultado


def normalize(raw_rows: list[dict]) -> list[list]:
    result = []
    for row in raw_rows:
        ref1         = str(row['ref1'] or '').strip().lstrip('0')
        fecha        = row['fecha']
        v            = row['valor']
        matching_key = f'{fecha}_{ref1}_{valor_str(v)}'
        payment_date = fecha.replace('/', '-')

        result.append([
            ref1,               # [0] identification
            payment_date,       # [1] payment_date  (DD-MM-YYYY)
            row['descripcion'], # [2] transaction_code_1
            row['sucursal'],    # [3] transaction_code_2
            ref1,               # [4] email
            'PREBANCOLOMBIA',   # [5] payment_method
            '',                 # [6] program
            '',                 # [7] phone
            v,                  # [8] payment_amount
            matching_key,       # [9] matching_key
        ])
    return result


def cheque_logic(
    normalized_rows: list[list],
    pendientes_raw: list[list],
) -> tuple[list, list, list, list]:
    normales = [r for r in normalized_rows if 'CHEQUE' not in str(r[2]).upper()]
    cheques  = [r for r in normalized_rows if 'CHEQUE'     in str(r[2]).upper()]

    if not cheques:
        return normales, [], [], []

    if not pendientes_raw:
        return normales, cheques, [], []

    first = pendientes_raw[0]
    if 'ESTADO' in first:
        header     = [h.strip() for h in first]
        data_rows  = pendientes_raw[1:]
        start_row  = 2
        ident_idx  = header.index('identification') if 'identification' in header else 0
        valor_idx  = header.index('payment_amount') if 'payment_amount' in header else 8
        estado_idx = header.index('ESTADO')
    else:
        data_rows  = pendientes_raw
        start_row  = 1
        ident_idx, valor_idx, estado_idx = 1, 9, 12

    mapa: dict[str, list] = {}
    for i, row in enumerate(data_rows, start=start_row):
        ident  = str(row[ident_idx]  if len(row) > ident_idx  else '').strip()
        val    = norm_valor_str(row[valor_idx]  if len(row) > valor_idx  else '')
        estado = str(row[estado_idx] if len(row) > estado_idx else '').strip()
        clave  = f'{ident}|{val}'
        mapa.setdefault(clave, []).append(
            {'row_index': i, 'estado': estado, 'estado_col': estado_idx}
        )

    pendientes_nuevos, conciliados, actualizaciones = [], [], []

    for row in cheques:
        clave = f'{row[0]}|{norm_valor_str(row[8])}'

        if clave not in mapa:
            pendientes_nuevos.append(row)
            continue

        registros = mapa[clave]
        pendiente = next((r for r in registros if r['estado'] == 'PENDIENTE'), None)
        ya_concil = next((r for r in registros if r['estado'] == 'CONCILIADO'), None)

        if pendiente:
            conciliados.append(row)
            actualizaciones.append(pendiente)
        elif ya_concil:
            log.warning('Cheque ya CONCILIADO ignorado: %s', clave)

    return normales, pendientes_nuevos, conciliados, actualizaciones
