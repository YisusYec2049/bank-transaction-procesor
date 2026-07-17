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


def sync_cartera_preventiva(supabase_url: str, service_role_key: str, rows: list[dict],
                             batch_size: int = 500) -> None:
    """Sincroniza cartera_preventiva por 'llave', sin borrar la tabla completa.

    A diferencia de replace_table (DELETE + INSERT), esto hace upsert por
    'llave' — solo toca las columnas que vienen del Excel. No pisa
    fecha_pago/medio_pago/valor_pago/codigo_transaccion_1/2/correo_elec/
    diferencia, que llena aparte cruzar_cartera_preventiva.py: antes, cada
    replace_table las dejaba en NULL hasta que ese script volvía a correr
    (~1 min después), dejando la vista de "resueltas" vacía en cada ciclo de
    sync_cartera.py. Requiere índice único en cartera_preventiva.llave
    (sql/007_cartera_preventiva_llave_unique.sql).

    Las llaves que ya no aparecen en el Excel nuevo (cuota que salió de
    cartera pendiente) se borran aparte, al final — EXCEPTO (Fase 4, 16 de
    julio): llaves de "línea de saldo" generadas por
    cruzar_cartera_preventiva.py (contienen " (saldo" en el texto, ver
    _generar_llave_saldo) — no existen en ningún Excel, así que sin esto se
    borrarían solas en el primer sync después de crearse; y llaves con
    cerrado_manual=true en cartera_preventiva_overrides — una cuota cerrada
    a mano no debe desaparecer si el Excel deja de traerla.
    """
    vistas: set[str] = set()
    deduped = []
    for r in rows:
        llave = r.get('llave')
        if not llave or llave in vistas:
            continue
        vistas.add(llave)
        deduped.append(r)

    if not deduped:
        log.warning('cartera_preventiva: 0 filas leídas del Excel, se omite la sincronización '
                     '(no se borra ni se toca la tabla existente).')
        return

    hdrs_upsert = _headers(service_role_key, prefer='return=minimal,resolution=merge-duplicates')
    for i in range(0, len(deduped), batch_size):
        batch = deduped[i:i + batch_size]
        resp = http.post(
            f'{supabase_url}/rest/v1/cartera_preventiva?on_conflict=llave',
            json=batch,
            headers=hdrs_upsert,
            timeout=30,
        )
        _raise_for_status(resp)

    existentes = select_all(supabase_url, service_role_key, 'cartera_preventiva', select='llave')
    llaves_actuales = {r['llave'] for r in existentes if r.get('llave')}

    overrides_cerrados = select_all(supabase_url, service_role_key, 'cartera_preventiva_overrides',
                                     select='llave,cerrado_manual')
    llaves_protegidas = {r['llave'] for r in overrides_cerrados if r.get('cerrado_manual')}

    llaves_a_borrar = [
        k for k in (llaves_actuales - vistas)
        if ' (saldo' not in k and k not in llaves_protegidas
    ]

    if llaves_a_borrar:
        hdrs_delete = _headers(service_role_key, prefer='return=minimal')
        for i in range(0, len(llaves_a_borrar), batch_size):
            batch = llaves_a_borrar[i:i + batch_size]
            valores = ','.join(f'"{v}"' for v in batch)
            resp = http.delete(
                f'{supabase_url}/rest/v1/cartera_preventiva',
                params={'llave': f'in.({valores})'},
                headers=hdrs_delete,
                timeout=30,
            )
            _raise_for_status(resp)

    log.info('cartera_preventiva sincronizada: %d filas actualizadas/insertadas, %d borradas (ya no están en el Excel).',
              len(deduped), len(llaves_a_borrar))


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
