#!/opt/matching-test/venv/bin/python3
"""
procesar_todos.py — procesa todos los bancos.

Bancos cubiertos: BC2576, BC2833, Placetopay, WOMPI, Stripe, Colpatria, Davivienda, PayU.

Para cada banco:
  1. Lista archivos nuevos en la carpeta Drive INBOX
  2. Descarga y parsea con el módulo fuentes/<banco>.py
  3. Normaliza al esquema estándar (10 columnas)
  4. Escribe al tab del día en CONSOLIDADO (Google Sheets) — siempre
  5. Cheques → gestiona en sheet CHEQUES_PENDIENTES
  6. Upsert en Supabase consolidated_transactions (si SKIP_SUPABASE != true)
  7. Mueve el archivo a la carpeta HISTORICO

PayU necesita DOS archivos (PayU + Moneda). Si solo hay uno, espera.

Flags:
  --bank <nombre>   Procesa solo ese banco (default: todos)
  --dry-run         Loguea sin escribir a Sheets/Supabase ni mover archivos
"""

import argparse
import io
import logging
import os
import sys
from datetime import datetime

import httplib2
import pytz
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build

import fuentes.bancolombia_2576 as mod_bc2576
import fuentes.bancolombia_2833 as mod_bc2833
import fuentes.placetopay as mod_placetopay
import fuentes.wompi      as mod_wompi
import fuentes.stripe     as mod_stripe
import fuentes.colpatria  as mod_colpatria
import fuentes.davivienda as mod_davivienda
import fuentes.payu       as mod_payu

from utils.drive    import list_files, download_pdf as download_file, move_file
from utils.sheets   import (ensure_tab, append_rows, get_yesterday_keys,
                             read_cheques, write_pendientes, mark_conciliado,
                             ensure_cheques_header)
from utils.supabase import upsert, upsert_cheque

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

BANCOS = {
    'placetopay': {'mod': mod_placetopay, 'prefix': 'PLACETOPAY', 'cheques': False},
    'wompi':      {'mod': mod_wompi,      'prefix': 'WOMPI',      'cheques': False},
    'stripe':     {'mod': mod_stripe,     'prefix': 'STRIPE',     'cheques': False},
    'colpatria':  {'mod': mod_colpatria,  'prefix': 'COLPATRIA',  'cheques': True},
    'davivienda': {'mod': mod_davivienda, 'prefix': 'DAVIVIENDA', 'cheques': True},
}


BANCOS_BANCOLOMBIA = {
    'bc2576': {'mod': mod_bc2576, 'prefix': 'BC2576'},
    'bc2833': {'mod': mod_bc2833, 'prefix': 'BC2833'},
}

# Orden de procesamiento (coincide con el orden en el consolidado)
_PIPELINE = [
    ('payu',       'payu'),
    ('bc2576',     'bancolombia'),
    ('wompi',      'banco'),
    ('placetopay', 'banco'),
    ('bc2833',     'bancolombia'),
    ('colpatria',  'banco'),
    ('davivienda', 'banco'),
    ('stripe',     'banco'),
]


# ── Google API ────────────────────────────────────────────────────────────────

def _build_services():
    creds  = service_account.Credentials.from_service_account_file(
        os.environ['GOOGLE_SA_JSON'], scopes=SCOPES
    )
    http   = httplib2.Http(timeout=60)
    authed = creds.authorize(http)
    drive  = build('drive',  'v3', http=authed, cache_discovery=False)
    sheets = build('sheets', 'v4', http=authed, cache_discovery=False)
    return drive, sheets


# ── Procesamiento genérico ────────────────────────────────────────────────────

def _procesar_banco(drive, sheets, banco: str, cfg: dict,
                    today_tab: str, consolidado_id: str,
                    yesterday_keys: set[str], dry_run: bool):
    mod    = cfg['mod']
    prefix = cfg['prefix']
    inbox  = os.environ.get(f'{prefix}_INBOX_FOLDER_ID', '')
    hist   = os.environ.get(f'{prefix}_HISTORICO_FOLDER_ID', '')

    if not inbox:
        log.warning('[%s] Sin INBOX configurado, saltando.', banco)
        return

    archivos = list_files(drive, inbox)
    if not archivos:
        log.info('[%s] Sin archivos nuevos.', banco)
        return

    log.info('[%s] %d archivo(s) nuevo(s).', banco, len(archivos))

    supabase_url = os.environ['SUPABASE_URL']
    srk          = os.environ['SUPABASE_SERVICE_ROLE_KEY']
    skip_supa    = os.environ.get('SKIP_SUPABASE', '').lower() == 'true'

    for f in archivos:
        fid   = f['id']
        fname = f['name']
        log.info('[%s] Procesando: %s', banco, fname)

        try:
            buf        = download_file(drive, fid)
            raw_rows   = mod.parse_file(buf, fname)
            if not raw_rows:
                log.warning('[%s] Sin filas válidas: %s', banco, fname)
                if hist and not dry_run:
                    move_file(drive, fid, hist)
                continue

            normalized = mod.normalize(raw_rows)
            normales, _nuevos, conciliados, _acts = mod.cheque_logic(normalized, [])
            candidatos = normales + conciliados

            # Dedup contra llaves de ayer
            seen, filtradas = set(), []
            for row in candidatos:
                key = row[9]
                if key in yesterday_keys or key in seen:
                    log.debug('[%s] Duplicado omitido: %s', banco, key)
                    continue
                seen.add(key)
                filtradas.append(row)

            log.info('[%s] %d filas normalizadas → %d tras dedup.',
                     banco, len(normalized), len(filtradas))

            if dry_run:
                for s in filtradas[:3]:
                    log.info('[DRY RUN] matching_key=%s | amount=%s', s[9], s[8])
                continue

            if not filtradas:
                log.info('[%s] Sin filas nuevas tras dedup.', banco)
            else:
                # Siempre escribe al CONSOLIDADO de Sheets
                append_rows(sheets, consolidado_id, today_tab, filtradas)

                # Supabase solo si no está desactivado
                if skip_supa:
                    log.info('[%s] SKIP_SUPABASE=true — solo Sheets.', banco)
                else:
                    upsert(supabase_url, srk, filtradas)
                    if cfg['cheques']:
                        for row in conciliados:
                            upsert_cheque(supabase_url, srk, banco, row)

            if hist:
                move_file(drive, fid, hist)
                log.info('[%s] Movido a Histórico: %s', banco, fname)

        except Exception:
            log.exception('[%s] Error procesando %s', banco, fname)


# ── Bancolombia (PDFs con lógica de cheques) ─────────────────────────────────

def _procesar_bancolombia(drive, sheets, banco: str, cfg: dict,
                          today_tab: str, consolidado_id: str,
                          yesterday_keys: set[str], dry_run: bool):
    mod    = cfg['mod']
    prefix = cfg['prefix']
    inbox  = os.environ.get(f'{prefix}_INBOX_FOLDER_ID', '')
    hist   = os.environ.get(f'{prefix}_HISTORICO_FOLDER_ID', '')
    cheques_id = os.environ.get(f'{prefix}_CHEQUES_SHEET_ID', '')

    if not inbox:
        log.warning('[%s] Sin INBOX configurado, saltando.', banco)
        return

    archivos = list_files(drive, inbox)
    if not archivos:
        log.info('[%s] Sin archivos nuevos.', banco)
        return

    log.info('[%s] %d archivo(s) nuevo(s).', banco, len(archivos))

    supabase_url = os.environ['SUPABASE_URL']
    srk          = os.environ['SUPABASE_SERVICE_ROLE_KEY']
    skip_supa    = os.environ.get('SKIP_SUPABASE', '').lower() == 'true'

    # Leer cheques pendientes existentes antes de procesar
    pendientes_raw = []
    if cheques_id and not dry_run:
        try:
            pendientes_raw = read_cheques(sheets, cheques_id)
        except Exception:
            log.exception('[%s] No se pudo leer sheet de cheques.', banco)

    for f in archivos:
        fid   = f['id']
        fname = f['name']
        log.info('[%s] Procesando: %s', banco, fname)

        try:
            buf        = download_file(drive, fid)
            raw_rows   = mod.parse_pdf(buf)
            if not raw_rows:
                log.warning('[%s] Sin filas válidas: %s', banco, fname)
                if hist and not dry_run:
                    move_file(drive, fid, hist)
                continue

            normalized = mod.normalize(raw_rows)
            normales, pendientes_nuevos, conciliados, actualizaciones = \
                mod.cheque_logic(normalized, pendientes_raw)

            # Solo normales + conciliados van al consolidado
            candidatos = normales + conciliados

            seen, filtradas = set(), []
            for row in candidatos:
                key = row[9]
                if key in yesterday_keys or key in seen:
                    log.debug('[%s] Duplicado omitido: %s', banco, key)
                    continue
                seen.add(key)
                filtradas.append(row)

            log.info('[%s] %d normalizadas → %d al consolidado | %d PENDIENTE | %d conciliados',
                     banco, len(normalized), len(filtradas),
                     len(pendientes_nuevos), len(conciliados))

            if dry_run:
                for s in filtradas[:3]:
                    log.info('[DRY RUN] matching_key=%s | amount=%s', s[9], s[8])
                if pendientes_nuevos:
                    log.info('[DRY RUN] %d cheques nuevos PENDIENTE (no se escriben)', len(pendientes_nuevos))
                continue

            if filtradas:
                append_rows(sheets, consolidado_id, today_tab, filtradas)
                if not skip_supa:
                    upsert(supabase_url, srk, filtradas)
                else:
                    log.info('[%s] SKIP_SUPABASE=true — solo Sheets.', banco)

            if cheques_id:
                ensure_cheques_header(sheets, cheques_id, mod.CHEQUES_HEADERS)
                if pendientes_nuevos:
                    write_pendientes(sheets, cheques_id, pendientes_nuevos)
                if actualizaciones:
                    mark_conciliado(sheets, cheques_id, actualizaciones)

            if hist:
                move_file(drive, fid, hist)
                log.info('[%s] Movido a Histórico: %s', banco, fname)

        except Exception:
            log.exception('[%s] Error procesando %s', banco, fname)


# ── PayU (caso especial: dos archivos) ────────────────────────────────────────

def _procesar_payu(drive, sheets, today_tab: str, consolidado_id: str,
                   yesterday_keys: set[str], dry_run: bool):
    payu_inbox   = os.environ.get('PAYU_INBOX_FOLDER_ID', '')
    moneda_inbox = os.environ.get('PAYU_MONEDA_INBOX_FOLDER_ID', '')
    payu_hist    = os.environ.get('PAYU_HISTORICO_FOLDER_ID', '')
    moneda_hist  = os.environ.get('PAYU_MONEDA_HISTORICO_FOLDER_ID', payu_hist)

    if not payu_inbox or not moneda_inbox:
        log.warning('[PAYU] Sin INBOX configurado, saltando.')
        return

    payu_files   = list_files(drive, payu_inbox)
    moneda_files = list_files(drive, moneda_inbox)

    if not payu_files and not moneda_files:
        log.info('[PAYU] Sin archivos nuevos.')
        return

    supabase_url = os.environ['SUPABASE_URL']
    srk          = os.environ['SUPABASE_SERVICE_ROLE_KEY']
    skip_supa    = os.environ.get('SKIP_SUPABASE', '').lower() == 'true'

    while payu_files and moneda_files:
        pf = payu_files.pop(0)
        mf = moneda_files.pop(0)
        log.info('[PAYU] Par: %s + %s', pf['name'], mf['name'])

        try:
            payu_buf   = download_file(drive, pf['id'])
            moneda_buf = download_file(drive, mf['id'])
            raw_rows   = mod_payu.parse_file(payu_buf, moneda_buf,
                                             payu_filename=pf['name'],
                                             moneda_filename=mf['name'])
            if not raw_rows:
                log.warning('[PAYU] Sin filas tras JOIN.')
            else:
                normalized = mod_payu.normalize(raw_rows)

                seen, filtradas = set(), []
                for row in normalized:
                    key = row[9]
                    if key in yesterday_keys or key in seen:
                        continue
                    seen.add(key)
                    filtradas.append(row)

                log.info('[PAYU] %d filas → %d tras dedup.', len(normalized), len(filtradas))

                if dry_run:
                    for s in filtradas[:3]:
                        log.info('[DRY RUN] matching_key=%s | amount=%s', s[9], s[8])
                elif filtradas:
                    append_rows(sheets, consolidado_id, today_tab, filtradas)
                    if not skip_supa:
                        upsert(supabase_url, srk, filtradas)
                    else:
                        log.info('[PAYU] SKIP_SUPABASE=true — solo Sheets.')

            if not dry_run:
                if payu_hist:
                    move_file(drive, pf['id'], payu_hist)
                if moneda_hist:
                    move_file(drive, mf['id'], moneda_hist)

        except Exception:
            log.exception('[PAYU] Error procesando par %s / %s', pf['name'], mf['name'])

    if payu_files:
        log.warning('[PAYU] %d archivo(s) PayU sin pareja Moneda.', len(payu_files))
    if moneda_files:
        log.warning('[PAYU] %d archivo(s) Moneda sin pareja PayU.', len(moneda_files))


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    load_dotenv()

    parser = argparse.ArgumentParser(description='Procesa todos los bancos.')
    parser.add_argument(
        '--bank',
        choices=list(BANCOS_BANCOLOMBIA.keys()) + list(BANCOS.keys()) + ['payu'],
        default=None,
        help='Procesa solo ese banco (default: todos).',
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='No escribe a Sheets/Supabase ni mueve archivos.',
    )
    args = parser.parse_args()

    if args.dry_run:
        log.info('=== DRY RUN activado ===')

    consolidado_id = os.environ.get('CONSOLIDADO_SHEET_ID', '')
    if not consolidado_id:
        log.error('CONSOLIDADO_SHEET_ID no configurado en .env')
        sys.exit(1)

    drive, sheets = _build_services()

    tz_bogota = pytz.timezone('America/Bogota')
    today_tab = datetime.now(tz_bogota).strftime('%d-%m-%Y')
    log.info('Tab del día: %s', today_tab)

    # Crear tab del día si no existe (idempotente)
    if not args.dry_run:
        ensure_tab(sheets, consolidado_id, today_tab, mod_placetopay.HEADERS)

    # Cargar llaves de ayer para dedup
    yesterday_keys = get_yesterday_keys(sheets, consolidado_id)
    log.info('Llaves históricas cargadas: %d', len(yesterday_keys))

    for banco, tipo in _PIPELINE:
        if args.bank and args.bank != banco:
            continue
        if tipo == 'payu':
            _procesar_payu(drive, sheets, today_tab, consolidado_id, yesterday_keys, args.dry_run)
        elif tipo == 'bancolombia':
            _procesar_bancolombia(drive, sheets, banco, BANCOS_BANCOLOMBIA[banco],
                                  today_tab, consolidado_id, yesterday_keys, args.dry_run)
        else:
            _procesar_banco(drive, sheets, banco, BANCOS[banco],
                            today_tab, consolidado_id, yesterday_keys, args.dry_run)

    log.info('procesar_todos.py completado.')


if __name__ == '__main__':
    main()
