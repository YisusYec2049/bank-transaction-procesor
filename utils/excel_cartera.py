"""Lectura de los Excel de referencia para el cruce de cartera
(Payu UC.xlsx / Ingresos PSE y PAYU.xlsx), descargados de Google Drive."""

import logging
from typing import BinaryIO

import openpyxl

from utils.parser import valor_str

log = logging.getLogger(__name__)


def _cell_str(v) -> str:
    if v is None:
        return ''
    if isinstance(v, float):
        return valor_str(v)
    if isinstance(v, int):
        return str(v)
    return str(v).strip()


def _find_header_row(ws, expected: list[str], max_scan: int = 5):
    """Busca, entre las primeras `max_scan` filas, la que contiene todas las
    columnas de `expected` (comparación case-insensitive). Devuelve
    (fila_1based, {header_normalizado: col_idx_0based})."""
    wanted = {h.strip().lower() for h in expected}
    best_row, best_map, best_hits = None, {}, -1

    for row_idx, row in enumerate(
        ws.iter_rows(min_row=1, max_row=max_scan, values_only=True), start=1
    ):
        col_map = {}
        for col_idx, cell in enumerate(row):
            if cell is None:
                continue
            key = str(cell).strip().lower()
            if key:
                col_map[key] = col_idx
        hits = len(wanted & col_map.keys())
        if hits > best_hits:
            best_row, best_map, best_hits = row_idx, col_map, hits

    missing = wanted - best_map.keys()
    if missing:
        raise ValueError(f'No se encontraron las columnas {missing} en las primeras {max_scan} filas.')

    return best_row, best_map


def read_inscrip(path: str | BinaryIO) -> list[dict]:
    """Payu UC.xlsx → hoja Inscrip. [{numero_id, id_inscripcion}, ...]."""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb['Inscrip']
        header_row, cols = _find_header_row(ws, ['Numero_ID', 'Id_Inscripcion'])

        rows = []
        for row in ws.iter_rows(min_row=header_row + 1, values_only=True):
            numero_id = _cell_str(row[cols['numero_id']])
            if not numero_id:
                continue
            rows.append({
                'numero_id':      numero_id,
                'id_inscripcion': _cell_str(row[cols['id_inscripcion']]),
            })
        log.info('Inscrip: %d filas leídas.', len(rows))
        return rows
    finally:
        wb.close()


def read_bancolombia_2576(path: str | BinaryIO) -> list[dict]:
    """Ingresos PSE y PAYU.xlsx → hoja BANCOLOMBIA 2576. [{referencia_1, incp}, ...]."""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb['BANCOLOMBIA 2576']
        header_row, cols = _find_header_row(ws, ['REFERENCIA 1', 'incp'])

        rows = []
        for row in ws.iter_rows(min_row=header_row + 1, values_only=True):
            ref1 = _cell_str(row[cols['referencia 1']])
            if not ref1:
                continue
            rows.append({
                'referencia_1': ref1,
                'incp':         _cell_str(row[cols['incp']]),
            })
        log.info('BANCOLOMBIA 2576 (Ingresos): %d filas leídas.', len(rows))
        return rows
    finally:
        wb.close()


def read_wompi(path: str | BinaryIO) -> list[dict]:
    """Ingresos PSE y PAYU.xlsx → hoja WOMPI. [{email, inscrip}, ...]."""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb['WOMPI']
        header_row, cols = _find_header_row(ws, ['email', 'INSCRIP'])

        rows = []
        for row in ws.iter_rows(min_row=header_row + 1, values_only=True):
            email = _cell_str(row[cols['email']]).lower()
            if not email:
                continue
            rows.append({
                'email':   email,
                'inscrip': _cell_str(row[cols['inscrip']]),
            })
        log.info('WOMPI (Ingresos): %d filas leídas.', len(rows))
        return rows
    finally:
        wb.close()


def read_stripe_usa(path: str | BinaryIO) -> list[dict]:
    """Ingresos PSE y PAYU.xlsx → hoja STRIPE_USA. [{email_cliente, incp}, ...]."""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb['STRIPE_USA']
        header_row, cols = _find_header_row(ws, ['EMAIL CLIENTE', 'INCP'])

        rows = []
        for row in ws.iter_rows(min_row=header_row + 1, values_only=True):
            email = _cell_str(row[cols['email cliente']]).lower()
            if not email:
                continue
            rows.append({
                'email_cliente': email,
                'incp':          _cell_str(row[cols['incp']]),
            })
        log.info('STRIPE_USA (Ingresos): %d filas leídas.', len(rows))
        return rows
    finally:
        wb.close()
