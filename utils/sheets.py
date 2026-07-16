"""Cliente Google Sheets: leer, escribir y gestionar tabs."""

import logging
from datetime import datetime

import pytz

log = logging.getLogger(__name__)


def get_yesterday_keys(sheets, spreadsheet_id: str) -> set[str]:
    """Devuelve las matching_keys del tab más reciente anterior a hoy."""
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
        log.warning('No hay tab de día anterior en CONSOLIDADO. Dedup histórico omitido.')
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

    header  = [h.strip() for h in rows[0]]
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


def ensure_tab(sheets, spreadsheet_id: str, tab_name: str, headers: list) -> None:
    meta = sheets.spreadsheets().get(
        spreadsheetId=spreadsheet_id, fields='sheets.properties.title'
    ).execute()
    existing = [s['properties']['title'] for s in meta.get('sheets', [])]
    if tab_name in existing:
        log.info('Tab "%s" ya existe.', tab_name)
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
    log.info('Tab "%s" creada con cabecera.', tab_name)


def append_rows(sheets, spreadsheet_id: str, tab_name: str, rows: list[list]) -> None:
    sheets.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=f"'{tab_name}'!A1",
        valueInputOption='RAW',
        insertDataOption='INSERT_ROWS',
        body={'values': rows},
    ).execute()
    log.info('Escritas %d filas en tab "%s".', len(rows), tab_name)
