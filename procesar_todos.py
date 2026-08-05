#!/opt/matching-test/venv/bin/python3
"""
procesar_todos.py — procesa todos los bancos.

Bancos cubiertos: BC2576, BC2833, Placetopay, WOMPI, Stripe, Colpatria, Davivienda, PayU.

Para cada banco:
  1. Lista archivos nuevos en la carpeta Drive INBOX
  2. Descarga y parsea con el módulo fuentes/<banco>.py
  3. Normaliza al esquema estándar (11 columnas)
  4. Cheques → se apartan del proceso (tabla pagos_apartados, tipo='cheque');
     nunca entran a consolidated_transactions
  5. Upsert en Supabase consolidated_transactions (si SKIP_SUPABASE != true)
  6. Mueve el archivo a la carpeta HISTORICO

Hasta el 2 de agosto de 2026 esto también escribía cada pago a un Google Sheet
("CONSOLIDADO"), con un tab por día. Se quitó: nadie lo leía, y la dedup que
dependía de él —las llaves del tab anterior— ahora sale de `registration_date`
en la propia base, que es el mismo dato sin el intermediario.

PayU necesita DOS archivos (PayU + Moneda). Si solo hay uno, espera.

Flags:
  --bank <nombre>   Procesa solo ese banco (default: todos)
  --dry-run         Loguea sin escribir a Supabase ni mover archivos
"""

import argparse
import logging
import os
import sys
from datetime import datetime

import pytz
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build

import fuentes.bancolombia_2576 as mod_bc2576
import fuentes.bancolombia_2833 as mod_bc2833
import fuentes.colpatria as mod_colpatria
import fuentes.davivienda as mod_davivienda
import fuentes.payu as mod_payu
import fuentes.placetopay as mod_placetopay
import fuentes.stripe as mod_stripe
import fuentes.wompi as mod_wompi
from utils import dry_run
from utils.drive import download_pdf as download_file
from utils.drive import list_files, move_file
from utils.supabase import (
    existing_matching_keys,
    keys_del_dia_anterior,
    select_all,
    upsert,
    upsert_pagos_apartados,
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%S',
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

SCOPES = [
    'https://www.googleapis.com/auth/drive',
]

BANCOS = {
    # dedup_sufijo: True para bancos cuya matching_key es fecha+documento+monto
    # (puede colisionar entre 2 pagos reales distintos, ver Fase 1.2). False
    # para los que usan un id de transacción único (no colisiona nunca).
    'placetopay': {'mod': mod_placetopay, 'prefix': 'PLACETOPAY', 'dedup_sufijo': False},
    'wompi':      {'mod': mod_wompi,      'prefix': 'WOMPI',      'dedup_sufijo': False},
    'stripe':     {'mod': mod_stripe,     'prefix': 'STRIPE',     'dedup_sufijo': False},
    'colpatria':  {'mod': mod_colpatria,  'prefix': 'COLPATRIA',  'dedup_sufijo': True},
    'davivienda': {'mod': mod_davivienda, 'prefix': 'DAVIVIENDA', 'dedup_sufijo': True},
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
    return build('drive', 'v3', credentials=creds, cache_discovery=False)


# ── Dedup / colisiones de matching_key ────────────────────────────────────────

def _asignar_sufijos_duplicados(rows: list[tuple], banco: str) -> list[tuple]:
    """Numera colisiones de matching_key por POSICIÓN dentro del lote (un
    mismo archivo) en vez de descartarlas. La llave de Bancolombia es
    fecha+documento+monto, así que una persona que paga 2 o 3 veces el mismo
    día por el mismo monto genera la misma llave y antes solo sobrevivía el
    último: el upsert pisaba a los anteriores y la plata desaparecía.

    El sufijo dice cuántas veces pagó, no que la fila esté repetida — el 1er
    pago va sin sufijo, el 2do " (pago 2)", el 3ro " (pago 3)", … Se asigna
    por posición para que reprocesar el mismo archivo dé siempre las mismas
    llaves (idempotente)."""
    contador: dict[str, int] = {}
    resultado = []
    for row in rows:
        base = row[10]
        n = contador.get(base, 0)
        contador[base] = n + 1
        if n == 0:
            resultado.append(row)
            continue
        row = list(row)
        row[10] = f'{base} (pago {n + 1})'
        log.warning('[%s] %sº pago igual del día (mismo documento y monto): %s -> %s',
                    banco, n + 1, base, row[10])
        resultado.append(tuple(row))
    return resultado


def _filtrar_duplicados(candidatos: list[tuple], yesterday_keys: set[str],
                         banco: str, usar_sufijos: bool) -> list[tuple]:
    """Descarta filas cuya llave ya está en el tab de ayer (Sheets, sin
    cambios). Dentro del lote de hoy: si usar_sufijos, no descarta
    colisiones — son pagos distintos de la misma persona y se numeran (ver
    _asignar_sufijos_duplicados); si no, mantiene el comportamiento viejo de
    quedarse solo con la 1ra ocurrencia."""
    sin_ayer = [row for row in candidatos if row[10] not in yesterday_keys]
    omitidos = len(candidatos) - len(sin_ayer)
    if omitidos:
        log.debug('[%s] %d duplicado(s) omitido(s) (ya en ayer).', banco, omitidos)

    if usar_sufijos:
        return _asignar_sufijos_duplicados(sin_ayer, banco)

    seen, filtradas = set(), []
    for row in sin_ayer:
        key = row[10]
        if key in seen:
            log.debug('[%s] Duplicado omitido: %s', banco, key)
            continue
        seen.add(key)
        filtradas.append(row)
    return filtradas


def _alertar_colision_supabase(filtradas: list[tuple], banco: str,
                                supabase_url: str, srk: str) -> None:
    """Alerta (log) si alguna matching_key de este lote ya existe en
    Supabase — colisión entre archivos/días distintos, no detectable con
    solo mirar el lote actual. No cambia sufijos (eso es solo por posición
    dentro del lote, para mantener el reproceso idempotente)."""
    ya_existentes = existing_matching_keys(supabase_url, srk, [r[10] for r in filtradas])
    for k in ya_existentes:
        log.warning('[%s] matching_key ya existe en Supabase (colisión entre lotes/días): %s', banco, k)


# ── Correcciones de documento: por qué acá ya no se aplican ───────────────────
#
# Hasta el 5 de agosto, al ingresar un archivo se reescribía el documento de
# toda fila que trajera un número ya corregido antes ("memoria por documento":
# una corrección se aplicaba sola a todos los pagos futuros con ese número).
# La idea era no tener que repetir la corrección cuando el banco manda mal el
# mismo documento varias veces.
#
# Se quitó porque el costo era mucho mayor que el ahorro. El número que queda
# guardado como "malo" es el que **tecleó una persona**, y si resulta ser el
# documento real de alguien más, los pagos de esa persona quedan con la
# identidad ajena sin que nadie lo note. Pasó en producción el 5 de agosto:
# corrigieron el pago de Fabián a los seis minutos, pero el número mal
# digitado era el de Alexander, y su pago ($695.284, que calzaba exacto con su
# cuota) quedó marcado como de Fabián.
#
# Desde entonces una corrección vale SOLO para el pago en el que se hizo, y la
# aplica `cruzar.py` leyendo `matching_key_original`. Un pago nuevo entra
# siempre con el documento que mandó el banco; si viene mal, se corrige.


# ── Cheques (Fase 2E): se apartan del proceso por completo ────────────────────

_CAMPOS_FIRMA_CHEQUE = (
    'val', 'identification', 'transaction_code_1', 'transaction_code_2',
    'email', 'payment_method', 'program', 'phone', 'payment_amount',
)


def _firma_cheque(vals: dict) -> tuple:
    """Firma de contenido de un cheque para detectar `aparicion` (primera vez
    / segunda vez): todas las columnas del consolidado EXCEPTO payment_date
    (y matching_key, que deriva de la fecha) — ver Fase 2.2 (E)."""
    monto = vals.get('payment_amount')
    try:
        monto_norm = round(float(monto), 2) if monto not in (None, '') else None
    except (TypeError, ValueError):
        monto_norm = monto
    return tuple(str(vals.get(c) or '') for c in _CAMPOS_FIRMA_CHEQUE if c != 'payment_amount') + (monto_norm,)


def _apartar_cheques(cheques: list[tuple], banco: str, supabase_url: str, srk: str, dry_run: bool) -> None:
    """Aparta cheques a pagos_apartados (tipo='cheque'). Nunca entran al
    consolidado ni al cruce — el área financiera no maneja cheques, se los
    pasa al área de Cartera. Calcula `aparicion` comparando contra cheques ya
    apartados (mismo criterio de firma que arriba). Sin conciliación ni
    rebote: solo 'primera vez'/'segunda vez'; una 3ra aparición es inesperada
    y solo se alerta por log (no hay un tercer valor válido en el esquema)."""
    if not cheques:
        return

    cheques = _asignar_sufijos_duplicados(cheques, banco)

    if dry_run:
        log.info('[%s] [DRY RUN] %d cheque(s) se apartarían a pagos_apartados (no se escriben).',
                  banco, len(cheques))
        return

    tz_bogota = pytz.timezone('America/Bogota')
    hoy = datetime.now(tz_bogota).strftime('%Y-%m-%d')

    existentes = select_all(supabase_url, srk, 'pagos_apartados',
                             select=','.join(_CAMPOS_FIRMA_CHEQUE) + ',tipo')
    conteo_firmas: dict[tuple, int] = {}
    for e in existentes:
        if e.get('tipo') != 'cheque':
            continue
        firma = _firma_cheque(e)
        conteo_firmas[firma] = conteo_firmas.get(firma, 0) + 1

    payload = []
    for row in cheques:
        dd, mm, yyyy = str(row[2]).split('-')
        vals = {
            'val': row[0], 'identification': row[1], 'transaction_code_1': row[3],
            'transaction_code_2': row[4], 'email': row[5], 'payment_method': row[6],
            'program': row[7], 'phone': row[8], 'payment_amount': row[9],
        }
        firma = _firma_cheque(vals)
        n = conteo_firmas.get(firma, 0) + 1
        conteo_firmas[firma] = n

        if n == 1:
            aparicion = 'primera vez'
        elif n == 2:
            aparicion = 'segunda vez'
        else:
            log.warning('[%s] Cheque con %da aparición (inesperado, solo debería haber 2): %s',
                        banco, n, row[10])
            aparicion = 'segunda vez'

        payload.append({
            'matching_key':  row[10],
            'tipo':          'cheque',
            'origen':        'automatico',
            'es_pago_unico': False,
            'incp_resuelto': None,
            'aparicion':     aparicion,
            'fecha_ingreso': hoy,
            'payment_date':  f'{yyyy}-{mm}-{dd}',
            **vals,
        })

    upsert_pagos_apartados(supabase_url, srk, payload)
    log.info('[%s] %d cheque(s) apartados a pagos_apartados.', banco, len(payload))


# ── Procesamiento genérico ────────────────────────────────────────────────────

def _procesar_banco(drive, banco: str, cfg: dict,
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
                log.warning('[%s] Sin filas válidas, se deja en Inbox para revisión: %s', banco, fname)
                continue

            normalized = mod.normalize(raw_rows)
            candidatos, cheques = mod.cheque_logic(normalized)

            usar_sufijos = cfg.get('dedup_sufijo', False)
            filtradas = _filtrar_duplicados(candidatos, yesterday_keys, banco, usar_sufijos)

            log.info('[%s] %d filas normalizadas → %d tras dedup (%d cheque(s) apartado(s)).',
                     banco, len(normalized), len(filtradas), len(cheques))

            _apartar_cheques(cheques, banco, os.environ['SUPABASE_URL'],
                              os.environ['SUPABASE_SERVICE_ROLE_KEY'], dry_run)

            if dry_run:
                for s in filtradas[:3]:
                    log.info('[DRY RUN] matching_key=%s | amount=%s', s[10], s[9])
                continue

            if not filtradas:
                log.info('[%s] Sin filas nuevas tras dedup.', banco)
            else:
                if skip_supa:
                    log.info('[%s] SKIP_SUPABASE=true — no se escribe nada.', banco)
                else:
                    if usar_sufijos:
                        _alertar_colision_supabase(filtradas, banco, supabase_url, srk)
                    upsert(supabase_url, srk, filtradas)

            if hist:
                move_file(drive, fid, hist)
                log.info('[%s] Movido a Histórico: %s', banco, fname)

        except Exception:
            log.exception('[%s] Error procesando %s', banco, fname)


# ── Bancolombia (PDFs con lógica de cheques) ─────────────────────────────────

def _procesar_bancolombia(drive, banco: str, cfg: dict,
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
            raw_rows   = mod.parse_pdf(buf)
            if not raw_rows:
                log.warning('[%s] Sin filas válidas, se deja en Inbox para revisión: %s', banco, fname)
                continue

            normalized = mod.normalize(raw_rows)
            candidatos, cheques = mod.cheque_logic(normalized)

            # Bancolombia (2576/2833): matching_key = fecha_documento_monto,
            # puede colisionar entre 2 pagos reales distintos (Fase 1.2) — se
            # numeran en vez de descartarse.
            filtradas = _filtrar_duplicados(candidatos, yesterday_keys, banco, True)

            log.info('[%s] %d normalizadas → %d al consolidado (%d cheque(s) apartado(s))',
                     banco, len(normalized), len(filtradas), len(cheques))

            _apartar_cheques(cheques, banco, supabase_url, srk, dry_run)

            if dry_run:
                for s in filtradas[:3]:
                    log.info('[DRY RUN] matching_key=%s | amount=%s', s[10], s[9])
                continue

            if filtradas:
                if not skip_supa:
                    _alertar_colision_supabase(filtradas, banco, supabase_url, srk)
                    upsert(supabase_url, srk, filtradas)
                else:
                    log.info('[%s] SKIP_SUPABASE=true — no se escribe nada.', banco)

            if hist:
                move_file(drive, fid, hist)
                log.info('[%s] Movido a Histórico: %s', banco, fname)

        except Exception:
            log.exception('[%s] Error procesando %s', banco, fname)


# ── PayU (caso especial: dos archivos) ────────────────────────────────────────

def _procesar_payu(drive, yesterday_keys: set[str], dry_run: bool):
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
                log.warning('[PAYU] Sin filas tras JOIN, se dejan en Inbox para revisión: %s + %s',
                            pf['name'], mf['name'])
                continue

            normalized = mod_payu.normalize(raw_rows)

            seen, filtradas = set(), []
            for row in normalized:
                key = row[10]
                if key in yesterday_keys or key in seen:
                    continue
                seen.add(key)
                filtradas.append(row)

            log.info('[PAYU] %d filas → %d tras dedup.', len(normalized), len(filtradas))

            if dry_run:
                for s in filtradas[:3]:
                    log.info('[DRY RUN] matching_key=%s | amount=%s', s[10], s[9])
                continue

            if filtradas:
                if not skip_supa:
                    upsert(supabase_url, srk, filtradas)
                else:
                    log.info('[PAYU] SKIP_SUPABASE=true — no se escribe nada.')

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
    parser.add_argument(
        '--dry-run-salida', metavar='RUTA',
        help='Dónde dejar el detalle de la simulación (default: logs/dry-run-*.jsonl).',
    )
    args = parser.parse_args()

    # El flag ya existía y frena las escrituras desde la lógica de este script;
    # encender además el interruptor de utils/ cierra el paso en la puerta, que
    # es lo que garantiza que no se escape ninguna llamada nueva.
    dry_run.desde_args(args, 'procesar_todos')
    if args.dry_run:
        log.info('=== DRY RUN activado ===')

    drive = _build_services()

    # Llaves del último día con ingresos, para no re-escribir lo que ya entró.
    yesterday_keys = keys_del_dia_anterior(
        os.environ['SUPABASE_URL'], os.environ['SUPABASE_SERVICE_ROLE_KEY'])
    log.info('Llaves históricas cargadas: %d', len(yesterday_keys))

    for banco, tipo in _PIPELINE:
        if args.bank and args.bank != banco:
            continue
        if tipo == 'payu':
            _procesar_payu(drive, yesterday_keys, args.dry_run)
        elif tipo == 'bancolombia':
            _procesar_bancolombia(drive, banco, BANCOS_BANCOLOMBIA[banco],
                                  yesterday_keys, args.dry_run)
        else:
            _procesar_banco(drive, banco, BANCOS[banco],
                            yesterday_keys, args.dry_run)

    log.info('procesar_todos.py completado.')
    if dry_run.activo():
        log.warning('%s', dry_run.resumen())


if __name__ == '__main__':
    main()
