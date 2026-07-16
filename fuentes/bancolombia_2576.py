"""
Parser y normalizador para extractos PDF de Bancolombia cuenta 2576.

Esquema de salida normalizado (11 columnas, índices 0-10):
  [0] identification      ← ref1 sin ceros iniciales
  [1] payment_date        ← DD-MM-YYYY
  [2] transaction_code_1  ← descripcion del movimiento
  [3] transaction_code_2  ← sucursal
  [4] email               ← igual a identification en esta fuente
  [5] payment_method      ← 'BANCOLOMBIA'
  [6] program             ← ''
  [7] phone               ← ''
  [8] payment_amount      ← float
  [9] matching_key        ← {DD/MM/YYYY}_{ref1}_{valor_js}
"""

import io
import re
import logging
from collections import defaultdict

import pdfplumber

from utils.parser import parse_valor, valor_str

log = logging.getLogger(__name__)

# ── Filtros ───────────────────────────────────────────────────────────────────

DESCRIPCIONES_ELIMINAR = frozenset([
    'ABONO', 'COMISION', 'RTE FUENTE', 'RTE ICA', 'IMPTO GOBIERNO',
    'VALOR IVA', 'IVA BOTON', 'IVA COMISION', 'RETENCION EN LA FUENTE',
    'TRASL ENTRE FONDOS', 'COMIS TRASLADO', 'PAGO DE PROV BANCOLOMBIA SA',
    'PAGO VIRTUAL PSE', 'BOTON', 'TRANSF BOTON',
])

_FIJAS = [
    'ABONO BRUTO AMEX', 'ABONO BRUTO MASTER', 'ABONO BRUTO VISA',
    'ABONO INTERESES AHORROS', 'CARGUE TARJETA PREPAGO PROPIA',
    'COMISION AMEX', 'COMISION BOTON', 'COMISION MASTER',
    'COMISION RECAUDO CAJA', 'COMISION TRANSF DE VICTOR HUGO', 'COMISION VISA',
    'CONSIG LOCAL REFEREN EFECTIVO', 'CONSIG NAL REFERENCIA CHEQUE',
    'CONSIG NAL REFERENCIA EFECTIVO', 'CONSIG NACIONAL REFERENCIA EFECTIVO',
    'CONSIG NACIONAL REFEREN EFECTIVO',
    'CONSIG LOCAL REFEREN CHEQUE', 'CONSIG LOCAL REFERENCIA CHEQUE',
    'CONSIG NACIONAL REFEREN CHEQUE', 'CONSIG NACIONAL REFERENCIA CHEQUE',
    'IMPTO GOBIERNO 4X1000', 'IVA BOTON', 'IVA COMISION RECAUDO CAJA',
    'IVA DE LA COMISION', 'PAGO A PROVEEDORES', 'PAGO DE PROV BANCOLOMBIA SA',
    'PAGO DE PROV CONSTRUCCIONES', 'PAGO DE PROV PROTECCION SA',
    'PAGO VIRTUAL PSE', 'RECAUDO SUCURSAL VIRTUAL', 'RETENCION EN LA FUENTE',
    'RTE FUENTE AMEX', 'RTE FUENTE MASTER', 'RTE FUENTE VISA',
    'RTE ICA AMEX', 'RTE ICA MASTER', 'RTE ICA VISA', 'TRANSF BOTON',
    'TRANSF DE VICTOR HUGO CORTES P', 'TRANSFERENCIA CTA SUC VIRTUAL',
    'TRANSFERENCIA DESDE NEQUI', 'TRANSFERENCIA NEQUI',
]

_ABIERTAS = [
    'PAGO DE TERC', 'PAGO DE PROV', 'PAGO INTERBANC',
    'PAGO LLAVE', 'PAGO QR', 'PAGO PSE', 'TRANSF DE',
]

_ABIERTAS_SET     = frozenset(_ABIERTAS)
_TIPOS_ORDENADOS  = sorted(_FIJAS + _ABIERTAS, key=len, reverse=True)

# ── Expresiones regulares ─────────────────────────────────────────────────────

_RE_FECHA      = re.compile(r'^\d{4}/\d{2}/\d{2}$')
_RE_FECHA_LINE = re.compile(r'^(\d{4}/\d{2}/\d{2})\s+(.*)')
_RE_VALOR      = re.compile(r'([-]?[\d,]+\.\d{2})$')
_RE_REF        = re.compile(r'\b\d{6,}\b')

# ── Cabeceras de hojas ────────────────────────────────────────────────────────

HEADERS = [
    'VAL',
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
    """
    {(fecha_DD/MM/YYYY, valor_float): sucursal_str}
    Extrae sucursal por posición X: columna SUCURSAL ∈ [240, 329).
    """
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

    log.info('parse_pdf 2576: %d brutas → %d tras filtros y dedup', len(filas), len(resultado))
    return resultado


def normalize(raw_rows: list[dict]) -> list[list]:
    result = []
    for row in raw_rows:
        ref1         = str(row['ref1'] or '').strip().lstrip('0')
        ref2         = str(row['ref2'] or '').strip().lstrip('0')
        ident        = ref2 if ref2 else ref1
        fecha        = row['fecha']
        v            = row['valor']
        matching_key = f'{fecha}_{ident}_{valor_str(v)}'
        payment_date = fecha.replace('/', '-')

        result.append([
            '',                 # [0]  VAL
            ident,              # [1]  identification
            payment_date,       # [2]  payment_date  (DD-MM-YYYY)
            row['descripcion'], # [3]  transaction_code_1
            row['sucursal'],    # [4]  transaction_code_2
            ident,              # [5]  email
            'BANCOLOMBIA',      # [6]  payment_method
            '',                 # [7]  program
            '',                 # [8]  phone
            v,                  # [9]  payment_amount
            matching_key,       # [10] matching_key
        ])
    return result


def cheque_logic(normalized_rows: list[list]) -> tuple[list, list]:
    """Separa cheques del resto. Los cheques se apartan del proceso por
    completo (nunca entran al consolidado ni al cruce — ver pagos_apartados
    en procesar_todos.py). transaction_code_1 (índice 3) es la descripción;
    antes se comparaba por error contra payment_date (índice 2), así que
    ningún cheque se detectaba nunca (bug de Fase 1.1)."""
    normales = [r for r in normalized_rows if 'CHEQUE' not in str(r[3]).upper()]
    cheques  = [r for r in normalized_rows if 'CHEQUE'     in str(r[3]).upper()]
    return normales, cheques
