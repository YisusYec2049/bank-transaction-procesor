"""Cliente Supabase: upsert a consolidated_transactions."""

import logging
from datetime import datetime

import pytz
import requests as http

log = logging.getLogger(__name__)

_ENDPOINT = '/rest/v1/consolidated_transactions?on_conflict=matching_key'
_PREFER   = 'return=minimal,resolution=merge-duplicates'


def _raise_for_status(resp: http.Response) -> None:
    """Como resp.raise_for_status(), pero logueando resp.text ANTES de
    lanzar la excepción. El mensaje default de requests.HTTPError no
    incluye el cuerpo de la respuesta — y ahí es donde PostgREST pone el
    detalle real (ej. `{"code":"PGRST102","message":"All object keys must
    match"}`). Bug real (16 de julio): un 400 en upsert_cartera_preventiva
    quedó invisible en el log del cron durante horas porque solo se veía
    "HTTPError: 400 Client Error", sin ninguna pista de la causa. Usar esto
    en vez de resp.raise_for_status() directo en cualquier llamada nueva."""
    if not resp.ok:
        log.error('Supabase %s %s -> %s: %s', resp.request.method if resp.request else '?',
                   resp.url, resp.status_code, resp.text)
    resp.raise_for_status()


def upsert(supabase_url: str, service_role_key: str, rows: list[list]) -> None:
    """
    rows: filas normalizadas [identification, payment_date(DD-MM-YYYY), ...]
    registration_date se agrega aquí como la fecha de hoy en Bogotá.
    """
    tz_bogota = pytz.timezone('America/Bogota')
    today_iso = datetime.now(tz_bogota).strftime('%Y-%m-%d')

    payload = []
    for r in rows:
        dd, mm, yyyy = str(r[2]).split('-')
        payload.append({
            'registration_date':  today_iso,
            'identification':     r[1],
            'payment_date':       f'{yyyy}-{mm}-{dd}',
            'transaction_code_1': r[3],
            'transaction_code_2': r[4],
            'email':              r[5],
            'payment_method':     r[6],
            'program':            r[7],
            'phone':              r[8],
            'payment_amount':     r[9],
            'matching_key':       r[10],
        })

    hdrs = {
        'apikey':        service_role_key,
        'Authorization': f'Bearer {service_role_key}',
        'Content-Type':  'application/json',
        'Prefer':        _PREFER,
    }
    resp = http.post(
        f'{supabase_url}{_ENDPOINT}',
        json=payload,
        headers=hdrs,
        timeout=30,
    )
    _raise_for_status(resp)
    log.info('Upsert Supabase OK: %d registros, HTTP %s.', len(payload), resp.status_code)


def existing_matching_keys(supabase_url: str, service_role_key: str, keys: list[str]) -> set[str]:
    """Subconjunto de `keys` que ya existe en consolidated_transactions.

    Usado para alertar colisiones de matching_key entre lotes/archivos
    distintos (dentro de un mismo archivo la numeración de duplicados es
    por posición, ver procesar_todos.py — esto solo detecta y loguea, no
    decide sufijos)."""
    if not keys:
        return set()
    encontrados: set[str] = set()
    batch_size = 200
    for i in range(0, len(keys), batch_size):
        batch = keys[i:i + batch_size]
        valores = ','.join(f'"{v}"' for v in batch)
        resp = http.get(
            f'{supabase_url}/rest/v1/consolidated_transactions',
            params={'select': 'matching_key', 'matching_key': f'in.({valores})'},
            headers=_headers(service_role_key),
            timeout=30,
        )
        _raise_for_status(resp)
        encontrados.update(r['matching_key'] for r in resp.json())
    return encontrados


def _headers(service_role_key: str, prefer: str | None = None) -> dict:
    hdrs = {
        'apikey':        service_role_key,
        'Authorization': f'Bearer {service_role_key}',
        'Content-Type':  'application/json',
    }
    if prefer:
        hdrs['Prefer'] = prefer
    return hdrs


def select_all(supabase_url: str, service_role_key: str, table: str,
                select: str = '*', page_size: int = 1000) -> list[dict]:
    """Trae todas las filas de `table` paginando de a `page_size`."""
    rows: list[dict] = []
    offset = 0
    while True:
        hdrs = _headers(service_role_key)
        hdrs['Range-Unit'] = 'items'
        hdrs['Range'] = f'{offset}-{offset + page_size - 1}'
        resp = http.get(
            f'{supabase_url}/rest/v1/{table}',
            params={'select': select},
            headers=hdrs,
            timeout=30,
        )
        _raise_for_status(resp)
        page = resp.json()
        rows.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
    return rows


def replace_table(supabase_url: str, service_role_key: str, table: str,
                   rows: list[dict], batch_size: int = 500) -> None:
    """Borra todo el contenido de `table` y lo reemplaza con `rows`."""
    hdrs = _headers(service_role_key, prefer='return=minimal')
    resp = http.delete(
        f'{supabase_url}/rest/v1/{table}',
        params={'id': 'gte.0'},
        headers=hdrs,
        timeout=30,
    )
    _raise_for_status(resp)

    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        resp = http.post(
            f'{supabase_url}/rest/v1/{table}',
            json=batch,
            headers=hdrs,
            timeout=30,
        )
        _raise_for_status(resp)

    log.info('Tabla "%s" reemplazada: %d filas.', table, len(rows))


def replace_cartera_preventiva_staging(supabase_url: str, service_role_key: str,
                                        rows: list[dict]) -> bool:
    """Reemplaza POR COMPLETO `cartera_preventiva_staging` con las filas del
    Excel de Cartera Preventiva más reciente (Spec C — versión de carga, 21
    de julio). A diferencia de la vieja `sync_cartera_preventiva` (que
    protegía las columnas de resultado del cruce en la tabla VIVA), staging
    no tiene ningún estado que proteger — es solo el espejo del Excel, se
    resincroniza completo en cada corrida igual que Payu UC / Ingresos. La
    tabla VIVA (`cartera_preventiva`) ya NO la toca el sync; solo el swap
    (`activar_cartera.py`, disparado por el botón "Cargar Cartera") y
    `cruzar_cartera_preventiva.py` (columnas de resultado).

    Si la lectura del Excel vino vacía, no se toca staging (misma
    salvaguarda del 15 de julio: "0 filas nuevas" no es "borrar todo lo
    viejo"). Devuelve True si reemplazó, False si se omitió."""
    if not rows:
        log.warning('cartera_preventiva_staging: 0 filas leídas del Excel, se omite '
                    '(no se toca la tabla existente).')
        return False
    replace_table(supabase_url, service_role_key, 'cartera_preventiva_staging', rows)
    return True


def insert_rows(supabase_url: str, service_role_key: str, table: str,
                 rows: list[dict], batch_size: int = 500) -> None:
    """INSERT plano (sin upsert) de `rows` en `table`, en lotes. Usado por el
    swap de versión de cartera (Spec C) para archivar filas hacia las tablas
    `_archivo` — nunca pisa nada existente, siempre agrega."""
    if not rows:
        return
    hdrs = _headers(service_role_key, prefer='return=minimal')
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        resp = http.post(f'{supabase_url}/rest/v1/{table}', json=batch, headers=hdrs, timeout=60)
        _raise_for_status(resp)
    log.info('Insertadas en "%s": %d fila(s).', table, len(rows))


def delete_all_rows(supabase_url: str, service_role_key: str, table: str,
                     pk_column: str = 'id') -> None:
    """Borra TODAS las filas de `table` — equivalente a TRUNCATE, que la API
    REST de PostgREST no expone directamente. Filtra por `pk_column is not
    null`, cierto para toda fila real (es su primary key). Usado por el swap
    de versión de cartera (Spec C) para vaciar las tablas vivas tras
    archivarlas."""
    hdrs = _headers(service_role_key, prefer='return=minimal')
    resp = http.delete(
        f'{supabase_url}/rest/v1/{table}',
        params={pk_column: 'not.is.null'},
        headers=hdrs,
        timeout=60,
    )
    _raise_for_status(resp)
    log.info('Tabla "%s" vaciada por completo.', table)


def upsert_cartera_cargas(supabase_url: str, service_role_key: str, rows: list[dict]) -> None:
    """Upsert por `carga_id` a `cartera_cargas` — marcador de control de
    versiones de cartera (Spec C): `estado` en ('staged','activa','archivada').
    fin-platform prende el banner "hay cartera pendiente" cuando existe una
    fila `staged`."""
    if not rows:
        return
    hdrs = _headers(service_role_key, prefer='return=minimal,resolution=merge-duplicates')
    resp = http.post(
        f'{supabase_url}/rest/v1/cartera_cargas?on_conflict=carga_id',
        json=rows,
        headers=hdrs,
        timeout=30,
    )
    _raise_for_status(resp)
    log.info('Upsert cartera_cargas OK: %d registro(s).', len(rows))


def upsert_cruce(supabase_url: str, service_role_key: str, rows: list[dict]) -> None:
    """Upsert de filas ya armadas (dicts con las 21 columnas de cruce_cartera)."""
    hdrs = _headers(
        service_role_key,
        prefer='return=minimal,resolution=merge-duplicates',
    )
    resp = http.post(
        f'{supabase_url}/rest/v1/cruce_cartera?on_conflict=matching_key',
        json=rows,
        headers=hdrs,
        timeout=30,
    )
    _raise_for_status(resp)
    log.info('Upsert cruce_cartera OK: %d registros, HTTP %s.', len(rows), resp.status_code)


def upsert_cartera_preventiva(supabase_url: str, service_role_key: str, rows: list[dict]) -> None:
    """Upsert parcial por id: cada dict solo trae `id` + las columnas de
    resultado del cruce (fecha_pago, medio_pago, valor_pago, códigos,
    correo_elec, diferencia) — el resto de la fila (llave, cliente, etc.,
    puestas ahí por el sync del Excel) no se toca."""
    hdrs = _headers(service_role_key, prefer='return=minimal,resolution=merge-duplicates')
    resp = http.post(
        f'{supabase_url}/rest/v1/cartera_preventiva?on_conflict=id',
        json=rows,
        headers=hdrs,
        timeout=30,
    )
    _raise_for_status(resp)
    log.info('Upsert cartera_preventiva OK: %d registros, HTTP %s.', len(rows), resp.status_code)


def insert_cartera_preventiva_lineas(supabase_url: str, service_role_key: str, rows: list[dict]) -> None:
    """Inserta líneas NUEVAS de saldo pendiente (Fase 4.4, pago parcial) —
    filas que no existen todavía, sin `id` (bigserial). Upsert por `llave`
    (no por `id`, que no existe aún) para que un reproceso del mismo evento
    no duplique la línea si ya se había creado."""
    if not rows:
        return
    hdrs = _headers(service_role_key, prefer='return=minimal,resolution=merge-duplicates')
    resp = http.post(
        f'{supabase_url}/rest/v1/cartera_preventiva?on_conflict=llave',
        json=rows,
        headers=hdrs,
        timeout=30,
    )
    _raise_for_status(resp)
    log.info('Líneas de saldo nuevas insertadas en cartera_preventiva: %d.', len(rows))


def upsert_pago_asociaciones(supabase_url: str, service_role_key: str, rows: list[dict]) -> None:
    """Upsert por (matching_key, llave) a pago_asociaciones (Fase 4.2/4.3):
    cada dict trae matching_key, llave, monto, origen."""
    if not rows:
        return
    hdrs = _headers(service_role_key, prefer='return=minimal,resolution=merge-duplicates')
    resp = http.post(
        f'{supabase_url}/rest/v1/pago_asociaciones?on_conflict=matching_key,llave',
        json=rows,
        headers=hdrs,
        timeout=30,
    )
    _raise_for_status(resp)
    log.info('Upsert pago_asociaciones OK: %d registros.', len(rows))


def update_cruce_valores(supabase_url: str, service_role_key: str, updates: list[dict]) -> None:
    """PATCH individual por matching_key: cada dict trae matching_key + los
    campos a actualizar (ej. {'matching_key': ..., 'cruce': 'Juan Perez'}).
    No usa upsert/POST porque cruce_cartera tiene columnas que podrían no
    aceptar NULL en un insert parcial — un PATCH real solo toca las columnas
    dadas, sin reconstruir la fila."""
    hdrs = _headers(service_role_key, prefer='return=minimal')
    for u in updates:
        mk = u['matching_key']
        body = {k: v for k, v in u.items() if k != 'matching_key'}
        resp = http.patch(
            f'{supabase_url}/rest/v1/cruce_cartera',
            params={'matching_key': f'eq.{mk}'},
            json=body,
            headers=hdrs,
            timeout=30,
        )
        _raise_for_status(resp)
    log.info('Update cruce_cartera (cruce inverso) OK: %d filas.', len(updates))


def upsert_pagos_apartados(supabase_url: str, service_role_key: str, rows: list[dict]) -> None:
    """Upsert por matching_key a pagos_apartados (matrículas, cesantías,
    pago por llave, cheques — ver Fase 2 del rediseño)."""
    if not rows:
        return
    hdrs = _headers(service_role_key, prefer='return=minimal,resolution=merge-duplicates')
    resp = http.post(
        f'{supabase_url}/rest/v1/pagos_apartados?on_conflict=matching_key',
        json=rows,
        headers=hdrs,
        timeout=30,
    )
    _raise_for_status(resp)
    log.info('Upsert pagos_apartados OK: %d registros, HTTP %s.', len(rows), resp.status_code)


def upsert_cartera_saldos_favor(supabase_url: str, service_role_key: str, rows: list[dict]) -> None:
    """Upsert por (matching_key, llave_origen) a cartera_saldos_favor: el
    ledger de saldo a favor asociable por cliente (modelo "Saldo a Favor
    Manual", 21 de julio). El pipeline solo escribe filas `origen='sobrante'`
    (plata que sobró tras cubrir todas las cuotas conocidas de una
    inscripción) — nunca las consume ni las marca `aplicado`; eso lo hace
    `financial-platform` al asociar/descartar a mano."""
    if not rows:
        return
    hdrs = _headers(service_role_key, prefer='return=minimal,resolution=merge-duplicates')
    resp = http.post(
        f'{supabase_url}/rest/v1/cartera_saldos_favor?on_conflict=matching_key,llave_origen',
        json=rows,
        headers=hdrs,
        timeout=30,
    )
    _raise_for_status(resp)
    log.info('Upsert cartera_saldos_favor OK: %d registros.', len(rows))


def delete_by_keys(supabase_url: str, service_role_key: str, table: str,
                    key_column: str, keys: list[str], batch_size: int = 200) -> None:
    """Borra de `table` todas las filas cuyo `key_column` esté en `keys`."""
    if not keys:
        return
    hdrs = _headers(service_role_key, prefer='return=minimal')
    for i in range(0, len(keys), batch_size):
        batch = keys[i:i + batch_size]
        valores = ','.join(f'"{v}"' for v in batch)
        resp = http.delete(
            f'{supabase_url}/rest/v1/{table}',
            params={key_column: f'in.({valores})'},
            headers=hdrs,
            timeout=30,
        )
        _raise_for_status(resp)
    log.info('Borradas de "%s" (por %s): %d llave(s).', table, key_column, len(keys))
