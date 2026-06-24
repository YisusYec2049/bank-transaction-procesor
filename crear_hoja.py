#!/usr/bin/env python3
"""
crear_hoja.py — Crea la tab del día (DD-MM-YYYY, zona America/Bogota) en el
spreadsheet CONSOLIDADO, e inicializa la cabecera de CHEQUES_PENDIENTES si
la hoja está vacía.

Idempotente: si la tab ya existe, informa y sale sin error.
Ejecutar una vez al día, antes del primer ciclo de procesar.py.

Flags:
  --bank {2576,2833}  Cuenta a preparar (default: 2576)
"""

import argparse
import os
import sys
import logging
from datetime import datetime

import pytz
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%S',
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/spreadsheets']


def _sheets_service():
    sa_path = os.environ['GOOGLE_SA_JSON']
    creds   = service_account.Credentials.from_service_account_file(sa_path, scopes=SCOPES)
    return build('sheets', 'v4', credentials=creds, cache_discovery=False)


def _today_bogota() -> str:
    tz = pytz.timezone('America/Bogota')
    return datetime.now(tz).strftime('%d-%m-%Y')


def _existing_tabs(sheets, spreadsheet_id: str) -> list[str]:
    meta = sheets.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields='sheets.properties.title',
    ).execute()
    return [s['properties']['title'] for s in meta.get('sheets', [])]


def crear_tab_dia(sheets, spreadsheet_id: str, tab_name: str, headers: list) -> None:
    existentes = _existing_tabs(sheets, spreadsheet_id)

    if tab_name in existentes:
        log.info('Tab "%s" ya existe en el CONSOLIDADO. Nada que hacer.', tab_name)
        return

    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={'requests': [{'addSheet': {'properties': {'title': tab_name}}}]},
    ).execute()
    log.info('Tab "%s" creada en el CONSOLIDADO.', tab_name)

    sheets.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"'{tab_name}'!A1",
        valueInputOption='RAW',
        body={'values': [headers]},
    ).execute()
    log.info('Cabecera escrita en "%s".', tab_name)


def inicializar_cheques(sheets, cheques_sheet_id: str, cheques_headers: list) -> None:
    resp = sheets.spreadsheets().values().get(
        spreadsheetId=cheques_sheet_id,
        range="'Hoja 1'!A1:A1",
    ).execute()

    if resp.get('values'):
        log.info('CHEQUES_PENDIENTES ya tiene contenido. No se modifica.')
        return

    sheets.spreadsheets().values().update(
        spreadsheetId=cheques_sheet_id,
        range="'Hoja 1'!A1",
        valueInputOption='RAW',
        body={'values': [cheques_headers]},
    ).execute()
    log.info('Cabecera escrita en CHEQUES_PENDIENTES.')


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description='Prepara tabs del día en el CONSOLIDADO.')
    parser.add_argument(
        '--bank', choices=['2576', '2833'], default='2576',
        help='Cuenta bancaria a preparar (default: 2576).',
    )
    args = parser.parse_args()

    if args.bank == '2576':
        from bancolombia_2576 import HEADERS, CHEQUES_HEADERS
        prefix = 'BC2576'
    else:
        from bancolombia_2833 import HEADERS, CHEQUES_HEADERS
        prefix = 'BC2833'

    consolidado_id   = os.environ[f'{prefix}_CONSOLIDADO_SHEET_ID']
    cheques_sheet_id = os.environ[f'{prefix}_CHEQUES_SHEET_ID']
    tab_name         = _today_bogota()

    log.info('Banco: %s | Fecha de hoy (Bogotá): %s', args.bank, tab_name)

    sheets = _sheets_service()
    crear_tab_dia(sheets, consolidado_id, tab_name, HEADERS)
    inicializar_cheques(sheets, cheques_sheet_id, CHEQUES_HEADERS)

    log.info('crear_hoja.py completado.')


if __name__ == '__main__':
    main()
