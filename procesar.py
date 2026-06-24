#!/home/y1sus/Documents/Dev/matching-test/venv/bin/python3
"""
procesar.py — Detecta PDFs nuevos de Bancolombia en Google Drive,
los normaliza al esquema estándar, deduplica, escribe al CONSOLIDADO
y hace upsert a Supabase.

Flags:
  --bank {2576,2833}  Cuenta a procesar (default: 2576)
  --dry-run           Procesa y loguea todo sin escribir a Sheets ni Supabase,
                      y sin mover archivos a Histórico.
"""

import argparse
import io
import logging
import os
import sys
from datetime import datetime

import pytz
import requests as http
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%S',
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

SCOPES = [
    'https://www.googleapis.com/auth/drive',
    'https://www.googleapis.com/auth/spreadsheets',
]


# ── Google API ────────────────────────────────────────────────────────────────

def _build_services():
    sa_path = os.environ['GOOGLE_SA_JSON']
    creds   = service_account.Credentials.from_service_account_file(sa_path, scopes=SCOPES)
    drive   = build('drive',  'v3', credentials=creds, cache_discovery=False)
    sheets  = build('sheets', 'v4', credentials=creds, cache_discovery=False)
    return drive, sheets


def _list_pdfs(drive, folder_id: str) -> list[dict]:
    result = drive.files().list(
        q=(f"'{folder_id}' in parents"
           " and mimeType='application/pdf'"
           " and trashed=false"),
        fields='files(id, name)',
        orderBy='createdTime',
    ).execute()
    return result.get('files', [])


def _download_pdf(drive, file_id: str) -> io.BytesIO:
    request    = drive.files().get_media(fileId=file_id)
    buf        = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    buf.seek(0)
    return buf


def _move_file(drive, file_id: str, dest_folder_id: str) -> None:
    f            = drive.files().get(fileId=file_id, fields='parents').execute()
    prev_parents = ','.join(f.get('parents', []))
    drive.files().update(
        fileId=file_id,
        addParents=dest_folder_id,
        removeParents=prev_parents,
        fields='id,parents',
    ).execute()


# ── Sheets helpers ────────────────────────────────────────────────────────────

def _col_letter(idx: int) -> str:
    """Índice 0-based → letra de columna en notación A1."""
    return chr(ord('A') + idx)


def _get_yesterday_keys(sheets, spreadsheet_id: str) -> set[str]:
    """Devuelve el conjunto de matching_keys del tab más reciente anterior a hoy."""
    meta = sheets.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields='sheets.properties.title',
    ).execute()

    tz    = pytz.timezone('America/Bogota')
    today = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)

    tabs = []
    for s in meta.get('sheets', []):
        title = s['properties']['title']
        try:
            dt = tz.localize(datetime.strptime(title, '%d-%m-%Y'))
            if dt < today:
                tabs.append((dt, title))
        except ValueError:
            pass

    if not tabs:
        log.warning('No se encontró tab de día anterior en CONSOLIDADO. Dedup histórico omitido.')
        return set()

    tabs.sort(key=lambda x: x[0], reverse=True)
    yesterday_tab = tabs[0][1]
    log.info('Dedup contra tab de ayer: %s', yesterday_tab)

    resp = sheets.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"'{yesterday_tab}'!A1:K",
    ).execute()
    rows = resp.get('values', [])
    if not rows:
        return set()

    header = [h.strip() for h in rows[0]]
    key_idx = None
    for col_name in ('matching_key', 'LLAVE'):
        if col_name in header:
            key_idx = header.index(col_name)
            break
    if key_idx is None:
        log.warning('Columna matching_key/LLAVE no encontrada en tab %s.', yesterday_tab)
        return set()

    return {
        str(row[key_idx]).strip()
        for row in rows[1:]
        if len(row) > key_idx and row[key_idx]
    }


def _append_to_sheet(sheets, spreadsheet_id: str, tab_name: str, rows: list[list]) -> None:
    sheets.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=f"'{tab_name}'!A1",
        valueInputOption='RAW',
        insertDataOption='INSERT_ROWS',
        body={'values': rows},
    ).execute()
    log.info('Escritas %d filas en tab "%s".', len(rows), tab_name)


def _read_cheques_sheet(sheets, cheques_sheet_id: str) -> list[list]:
    resp = sheets.spreadsheets().values().get(
        spreadsheetId=cheques_sheet_id,
        range="'Hoja 1'!A1:M",
    ).execute()
    return resp.get('values', [])


def _write_pendientes(sheets, cheques_sheet_id: str, rows: list[list]) -> None:
    sheets.spreadsheets().values().append(
        spreadsheetId=cheques_sheet_id,
        range="'Hoja 1'!A1",
        valueInputOption='RAW',
        insertDataOption='INSERT_ROWS',
        body={'values': [[*r, 'PENDIENTE'] for r in rows]},
    ).execute()
    log.info('Cheques PENDIENTE escritos: %d', len(rows))


def _mark_conciliado(sheets, cheques_sheet_id: str, actualizaciones: list[dict]) -> None:
    data = [
        {
            'range': f"'Hoja 1'!{_col_letter(a['estado_col'])}{a['row_index']}",
            'values': [['CONCILIADO']],
        }
        for a in actualizaciones
    ]
    sheets.spreadsheets().values().batchUpdate(
        spreadsheetId=cheques_sheet_id,
        body={'valueInputOption': 'RAW', 'data': data},
    ).execute()
    log.info('Cheques marcados CONCILIADO: %d', len(actualizaciones))


# ── Supabase ──────────────────────────────────────────────────────────────────

def _upsert_supabase(supabase_url: str, service_role_key: str, rows: list[list]) -> None:
    """
    Mapea las filas normalizadas al esquema de Supabase y hace upsert.
    payment_date se convierte de DD-MM-YYYY a YYYY-MM-DD (ISO).
    """
    tz_bogota    = pytz.timezone('America/Bogota')
    today_iso    = datetime.now(tz_bogota).strftime('%Y-%m-%d')

    payload = []
    for r in rows:
        dd, mm, yyyy = str(r[1]).split('-')         # DD-MM-YYYY → partes
        payment_date_iso = f'{yyyy}-{mm}-{dd}'      # YYYY-MM-DD
        payload.append({
            'registration_date':  today_iso,
            'identification':     r[0],
            'payment_date':       payment_date_iso,
            'transaction_code_1': r[2],
            'transaction_code_2': r[3],
            'email':              r[4],
            'payment_method':     r[5],
            'program':            r[6],
            'phone':              r[7],
            'payment_amount':     r[8],
            'matching_key':       r[9],
        })

    url  = f'{supabase_url}/rest/v1/consolidated_transactions?on_conflict=matching_key'
    hdrs = {
        'apikey':       service_role_key,
        'Authorization': f'Bearer {service_role_key}',
        'Content-Type': 'application/json',
        'Prefer':       'return=minimal,resolution=merge-duplicates',
    }
    resp = http.post(url, json=payload, headers=hdrs, timeout=30)
    resp.raise_for_status()
    log.info('Upsert Supabase OK: %d registros, HTTP %s.', len(payload), resp.status_code)


def _ensure_tab(sheets, spreadsheet_id: str, tab_name: str, headers: list) -> None:
    meta = sheets.spreadsheets().get(
        spreadsheetId=spreadsheet_id, fields='sheets.properties.title'
    ).execute()
    existing = [s['properties']['title'] for s in meta.get('sheets', [])]
    if tab_name in existing:
        return
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={'requests': [{'addSheet': {'properties': {'title': tab_name}}}]},
    ).execute()
    sheets.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"'{tab_name}'!A1",
        valueInputOption='RAW',
        body={'values': [headers]},
    ).execute()
    log.info('Tab "%s" creada en CONSOLIDADO.', tab_name)


def _ensure_cheques_header(sheets, cheques_sheet_id: str, headers: list) -> None:
    resp = sheets.spreadsheets().values().get(
        spreadsheetId=cheques_sheet_id, range="'Hoja 1'!A1:A1"
    ).execute()
    if resp.get('values'):
        return
    sheets.spreadsheets().values().update(
        spreadsheetId=cheques_sheet_id,
        range="'Hoja 1'!A1",
        valueInputOption='RAW',
        body={'values': [headers]},
    ).execute()
    log.info('Cabecera escrita en CHEQUES_PENDIENTES.')


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description='Procesa extractos Bancolombia.')
    parser.add_argument(
        '--bank', choices=['2576', '2833'], default='2576',
        help='Cuenta bancaria a procesar (default: 2576).',
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Ejecuta todo sin escribir a Sheets/Supabase ni mover archivos.',
    )
    args = parser.parse_args()

    if args.bank == '2576':
        from bancolombia_2576 import cheque_logic, normalize, parse_pdf, HEADERS, CHEQUES_HEADERS
        prefix = 'BC2576'
    else:
        from bancolombia_2833 import cheque_logic, normalize, parse_pdf, HEADERS, CHEQUES_HEADERS
        prefix = 'BC2833'

    if args.dry_run:
        log.info('=== DRY RUN activado — banco %s ===', args.bank)

    drive, sheets = _build_services()

    inbox_folder     = os.environ[f'{prefix}_INBOX_FOLDER_ID']
    historico_folder = os.environ[f'{prefix}_HISTORICO_FOLDER_ID']
    consolidado_id   = os.environ[f'{prefix}_CONSOLIDADO_SHEET_ID']
    cheques_sheet_id = os.environ[f'{prefix}_CHEQUES_SHEET_ID']
    supabase_url     = os.environ['SUPABASE_URL']
    service_role_key = os.environ['SUPABASE_SERVICE_ROLE_KEY']

    tz_bogota = pytz.timezone('America/Bogota')
    today_tab = datetime.now(tz_bogota).strftime('%d-%m-%Y')
    log.info('Banco: %s | Tab del día: %s', args.bank, today_tab)

    # Crear tab del día e inicializar cheques si hace falta (idempotente).
    if not args.dry_run:
        _ensure_tab(sheets, consolidado_id, today_tab, HEADERS)
        _ensure_cheques_header(sheets, cheques_sheet_id, CHEQUES_HEADERS)

    pdfs = _list_pdfs(drive, inbox_folder)
    if not pdfs:
        log.info('No hay PDFs en la carpeta de entrada. Fin.')
        return

    log.info('PDFs encontrados: %d', len(pdfs))

    # Cargar llaves de ayer una sola vez para todo el lote.
    yesterday_keys = _get_yesterday_keys(sheets, consolidado_id)
    log.info('Llaves históricas cargadas: %d', len(yesterday_keys))

    for pdf_file in pdfs:
        file_id   = pdf_file['id']
        file_name = pdf_file['name']
        log.info('── Procesando: %s (%s)', file_name, file_id)

        # 1. Descargar y parsear el PDF.
        pdf_bytes  = _download_pdf(drive, file_id)
        raw_rows   = parse_pdf(pdf_bytes)
        log.info('Filas extraídas del PDF: %d', len(raw_rows))

        if not raw_rows:
            log.warning('PDF sin filas válidas, se mueve igualmente a Histórico.')
            if not args.dry_run:
                _move_file(drive, file_id, historico_folder)
            continue

        # 2. Normalizar al esquema estándar.
        normalized = normalize(raw_rows)
        log.info('Filas normalizadas: %d', len(normalized))

        # 3. Lógica de cheques.
        pendientes_raw = _read_cheques_sheet(sheets, cheques_sheet_id)
        normales, pendientes_nuevos, conciliados, actualizaciones = cheque_logic(
            normalized, pendientes_raw
        )
        log.info(
            'Cheques → nuevos PENDIENTE: %d | recién CONCILIADO: %d | ignorados: %d',
            len(pendientes_nuevos),
            len(conciliados),
            len([r for r in normalized if 'CHEQUE' in str(r[2]).upper()])
            - len(pendientes_nuevos) - len(conciliados),
        )

        if not args.dry_run:
            if pendientes_nuevos:
                _write_pendientes(sheets, cheques_sheet_id, pendientes_nuevos)
            if actualizaciones:
                _mark_conciliado(sheets, cheques_sheet_id, actualizaciones)

        # 4. Unir normales + cheques conciliados → candidatos al consolidado.
        candidatos = normales + conciliados
        log.info('Candidatos al consolidado: %d', len(candidatos))

        # 5. Dedup contra llaves del día anterior + dedup interno del lote.
        seen, filtradas = set(), []
        for row in candidatos:
            key = row[9]  # matching_key
            if key in yesterday_keys or key in seen:
                log.debug('Duplicado omitido: %s', key)
                continue
            seen.add(key)
            filtradas.append(row)

        log.info('Filas tras dedup: %d', len(filtradas))

        skip_supabase = os.environ.get('SKIP_SUPABASE', '').lower() == 'true'

        if not filtradas:
            log.info('Sin filas nuevas tras dedup.')
        elif args.dry_run:
            log.info('[DRY RUN] Se escribirían %d filas a Sheets y Supabase.', len(filtradas))
            for sample in filtradas[:3]:
                log.info('[DRY RUN] Muestra: matching_key=%s | amount=%s', sample[9], sample[8])
        else:
            # 6. Escribir al tab del día en CONSOLIDADO.
            _append_to_sheet(sheets, consolidado_id, today_tab, filtradas)

            # 7. Upsert a Supabase (omitir si SKIP_SUPABASE=true en .env).
            if skip_supabase:
                log.info('[SKIP_SUPABASE] Upsert omitido. %d filas escritas solo en Sheets.', len(filtradas))
            else:
                _upsert_supabase(supabase_url, service_role_key, filtradas)

        # 8. Mover PDF a Histórico (idempotencia: no se reprocesa).
        if not args.dry_run:
            _move_file(drive, file_id, historico_folder)
            log.info('PDF movido a Histórico: %s', file_name)
        else:
            log.info('[DRY RUN] PDF no movido: %s', file_name)

    log.info('procesar.py completado.')


if __name__ == '__main__':
    main()
