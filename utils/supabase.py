"""Cliente Supabase: upsert a consolidated_transactions."""

import logging
from datetime import datetime

import pytz
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from utils import dry_run

log = logging.getLogger(__name__)

_ENDPOINT = '/rest/v1/consolidated_transactions?on_conflict=matching_key'
_PREFER   = 'return=minimal,resolution=merge-duplicates'


def _construir_sesion() -> requests.Session:
    """Una sola conexión TCP+TLS para toda la corrida, en vez de una nueva por
    petición.

    Medido contra producción el 2026-08-02: una petición trivial cuesta
    **433 ms** abriendo conexión y **247 ms** reusándola. El apretón de manos
    de TLS es casi la mitad del costo de hablar con la base, y este pipeline
    hace cientos de peticiones por corrida.

    Los reintentos son parte del mismo cambio y no un extra: una conexión que
    se mantiene viva puede quedar rancia del otro lado, y el 30 de julio una
    corrida ya murió con `SSLEOFError` a media escritura. Se reintenta **solo
    en métodos idempotentes** — `allowed_methods` deja fuera POST y PATCH a
    propósito: si un POST llegó y lo que se perdió fue la respuesta,
    reintentarlo escribiría dos veces. Ese riesgo no se corre con plata.
    """
    sesion = requests.Session()
    politica = Retry(
        total=3,
        backoff_factor=0.5,              # 0.5s, 1s, 2s
        status_forcelist=(502, 503, 504),
        allowed_methods=frozenset(['GET', 'HEAD', 'OPTIONS']),
        raise_on_status=False,
    )
    adaptador = HTTPAdapter(max_retries=politica, pool_maxsize=20)
    sesion.mount('https://', adaptador)
    sesion.mount('http://', adaptador)
    return sesion


# Se llama `http` para que las ~40 llamadas de este módulo (`http.get`,
# `http.post`, …) no cambien: una Session expone los mismos verbos.
http = _construir_sesion()


def _raise_for_status(resp: requests.Response) -> None:
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
    if dry_run.registrar('consolidated_transactions', 'upsert', rows):
        return
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

    # registration_date es la fecha en que un pago ENTRA por primera vez al
    # sistema, y debe ser ESTABLE: no se re-escribe cuando el mismo archivo se
    # reprocesa. Sin esto, el export acumulado de Stripe (que trae varios días
    # de pagos) re-sellaba con la fecha de hoy pagos que ya habían entrado días
    # atrás, cada vez que se procesaba. Solución: los pagos que YA existen se
    # actualizan (merge) en todas sus columnas MENOS registration_date; solo
    # los nuevos la estrenan. Van en dos POST porque un array de merge exige
    # que todos los objetos tengan el mismo set de claves (PGRST102).
    existentes = existing_matching_keys(
        supabase_url, service_role_key, [p['matching_key'] for p in payload])
    nuevos   = [p for p in payload if p['matching_key'] not in existentes]
    ya_estan = [{k: v for k, v in p.items() if k != 'registration_date'}
                for p in payload if p['matching_key'] in existentes]

    for lote in (nuevos, ya_estan):
        if not lote:
            continue
        resp = http.post(
            f'{supabase_url}{_ENDPOINT}',
            json=lote,
            headers=hdrs,
            timeout=30,
        )
        _raise_for_status(resp)
    log.info('Upsert Supabase OK: %d registros (%d nuevos, %d ya existían — '
             'sin re-sellar fecha de ingreso).', len(payload), len(nuevos), len(ya_estan))


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
    if dry_run.registrar(table, 'replace', rows):
        return
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
    # Devuelve True (no None) porque el llamador usa el valor para decidir si
    # registra la carga: en simulación la respuesta correcta es "sí habría
    # reemplazado", no "se omitió".
    if dry_run.registrar('cartera_preventiva_staging', 'replace', rows):
        return True
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
    if dry_run.registrar(table, 'insert', rows):
        return
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
    if dry_run.registrar(table, 'delete', None):
        return
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
    if dry_run.registrar('cartera_cargas', 'upsert', rows):
        return
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
    if dry_run.registrar('cruce_cartera', 'upsert', rows):
        return
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
    if dry_run.registrar('cartera_preventiva', 'upsert', rows):
        return
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
    if dry_run.registrar('cartera_preventiva', 'insert', rows):
        return
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
    if dry_run.registrar('pago_asociaciones', 'upsert', rows):
        return
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


# Qué funciones de lote existen en esta base. None = todavía no se preguntó.
# Se recuerda para no gastar una petición por lote preguntando lo mismo.
_LOTE_DISPONIBLE: dict[str, bool] = {}


def _patch_una_por_una(supabase_url: str, service_role_key: str, tabla: str,
                        updates: list[dict]) -> int:
    """El camino de siempre: un PATCH por fila.

    Se conserva como respaldo, no como reliquia. Es lo que corre mientras la
    función de lote no esté creada en la base, y es lo que deja el despliegue
    desacoplado del SQL: subir el código antes de correr la migración no rompe
    nada, solo va lento. Esa dependencia de orden —código nuevo que exige SQL
    ya corrido— fue la que dejó el motor caído un día entero el 24 de julio.
    """
    hdrs = _headers(service_role_key, prefer='return=minimal')
    escritas = 0
    for u in updates:
        body = {k: v for k, v in u.items() if k != 'matching_key'}
        if not body:
            continue
        resp = http.patch(
            f'{supabase_url}/rest/v1/{tabla}',
            params={'matching_key': f"eq.{u['matching_key']}"},
            json=body,
            headers=hdrs,
            timeout=30,
        )
        _raise_for_status(resp)
        escritas += 1
    return escritas


def _actualizar_por_matching_key(supabase_url: str, service_role_key: str, tabla: str,
                                  rpc: str, updates: list[dict], batch_size: int) -> int:
    """Aplica actualizaciones parciales por `matching_key` en una sola petición.

    **Nunca usa upsert/POST contra la tabla.** Tanto `cruce_cartera` como
    `consolidated_transactions` tienen columnas `NOT NULL` sin default, y un
    POST con `on_conflict` pero payload parcial las viola al construir la tupla
    de INSERT: Postgres valida el `NOT NULL` **antes** de que el `ON CONFLICT`
    alcance a redirigir al UPDATE, así que revienta con 23502 aunque la fila ya
    exista. Es exactamente el crash del 24 de julio.

    Por eso la escritura en lote va por una función de base y no por PostgREST:
    es la única forma de mandar valores distintos para filas distintas en una
    sola petición sin pasar por el camino de INSERT.

    Si la función no existe en esta base, cae sola al PATCH fila por fila.
    """
    if _LOTE_DISPONIBLE.get(rpc) is False:
        return _patch_una_por_una(supabase_url, service_role_key, tabla, updates)

    hdrs = _headers(service_role_key, prefer='return=minimal')
    for i in range(0, len(updates), batch_size):
        lote = updates[i:i + batch_size]
        resp = http.post(
            f'{supabase_url}/rest/v1/rpc/{rpc}',
            json={'cambios': lote},
            headers=hdrs,
            timeout=120,
        )
        if resp.status_code in (404, 400) and rpc in resp.text:
            # PGRST202: la función no está creada acá todavía.
            if rpc not in _LOTE_DISPONIBLE:
                log.warning('La función %s() no existe en la base: se actualiza fila por '
                            'fila (lento). Correr la migración para activarla.', rpc)
            _LOTE_DISPONIBLE[rpc] = False
            return _patch_una_por_una(supabase_url, service_role_key, tabla, updates)
        _raise_for_status(resp)
        _LOTE_DISPONIBLE[rpc] = True

    return len(updates)


def _ultimo_por_llave(updates: list[dict]) -> list[dict]:
    """Deja una sola entrada por `matching_key`, la última.

    El bucle de PATCH aplicaba las repetidas en orden, así que la última era la
    que quedaba. En lote, el `UPDATE ... FROM` de Postgres elegiría una de las
    dos sin criterio — se resuelve acá para que el resultado no cambie.
    """
    unicos: dict[str, dict] = {}
    for u in updates:
        unicos[u['matching_key']] = u
    return list(unicos.values())


def update_cruce_valores(supabase_url: str, service_role_key: str, updates: list[dict],
                          batch_size: int = 2000) -> None:
    """Actualiza filas de cruce_cartera; cada dict trae matching_key + las
    columnas a cambiar (ej. {'matching_key': ..., 'cruce': 'Juan Perez'}).

    Medido contra producción el 2026-08-02: 1.080 filas × 247 ms ≈ **4,5
    minutos** fila por fila, contra ~0,4 s en lote. Era el mayor cuello de
    botella del pipeline; el cálculo nunca fue lento — se pedía el mismo favor
    mil veces.

    Los lotes son **heterogéneos** a propósito: la fase 9.4 manda `incp` solo
    cuando lo resolvió. La función de base mira qué claves trae cada objeto y
    deja intactas las que no vienen; si rellenara con NULL, borraría el INCP de
    las filas donde no venía.
    """
    if dry_run.registrar('cruce_cartera', 'update', updates):
        return
    if not updates:
        return

    filas = _ultimo_por_llave(updates)
    escritas = _actualizar_por_matching_key(
        supabase_url, service_role_key, 'cruce_cartera',
        'actualizar_cruce_valores', filas, batch_size)
    log.info('Update cruce_cartera (cruce inverso) OK: %d filas%s.', escritas,
             ' (en lote)' if _LOTE_DISPONIBLE.get('actualizar_cruce_valores') else '')


def update_consolidated_campos(supabase_url: str, service_role_key: str,
                                updates: list[dict]) -> None:
    """Escribe de vuelta en consolidated_transactions los campos que el pago
    crudo no traía y el pipeline resolvió después. Cada dict trae
    `matching_key` + las columnas a escribir.

    Hoy son los dos que salen del ReportePagosWompi:
      - `metodo_de_pago` (link/manual, punto #8 del 23 de julio),
      - `program` (el "Proyecto" del reporte, 27 de julio) — el CSV de WOMPI
        no trae el programa en ninguna columna, así que sin esto la vista de
        Transacciones consolidadas lo muestra vacío para todo WOMPI.

    PATCH individual por `matching_key` (mismo patrón que `update_cruce_valores`
    / `update_cruce_inverso`): NO usa upsert/POST. `consolidated_transactions`
    tiene columnas `NOT NULL` sin default (ej. `registration_date`), y un POST
    con `on_conflict` pero payload parcial viola ese `NOT NULL` al construir la
    tupla de INSERT — Postgres valida `NOT NULL` ANTES de que el `ON CONFLICT`
    pueda redirigir al UPDATE, así que revienta con 23502 aunque la fila exista.
    Un PATCH real solo toca las columnas enviadas sobre las filas que ya
    existen; si una llave no está, es no-op.

    Estos campos viven acá, y no solo en cruce_cartera, porque un pago apartado
    (matrícula, cesantías…) se borra del cruce y el reporte de métricas WOMPI
    igual tiene que poder contarlo (punto #8, 23 de julio)."""
    if dry_run.registrar('consolidated_transactions', 'update', updates):
        return
    if not updates:
        return

    # Un objeto que solo trae la llave no tiene nada que escribir.
    filas = [u for u in _ultimo_por_llave(updates)
             if any(k != 'matching_key' for k in u)]
    if not filas:
        return

    escritas = _actualizar_por_matching_key(
        supabase_url, service_role_key, 'consolidated_transactions',
        'actualizar_consolidated_campos', filas, 2000)
    log.info('Update consolidated_transactions OK: %d filas%s.', escritas,
             ' (en lote)' if _LOTE_DISPONIBLE.get('actualizar_consolidated_campos') else '')


def upsert_pagos_apartados(supabase_url: str, service_role_key: str, rows: list[dict]) -> None:
    """Upsert por matching_key a pagos_apartados (matrículas, cesantías,
    pago por llave, cheques — ver Fase 2 del rediseño)."""
    if dry_run.registrar('pagos_apartados', 'upsert', rows):
        return
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
    if dry_run.registrar('cartera_saldos_favor', 'upsert', rows):
        return
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
    if dry_run.registrar(table, 'delete', keys):
        return
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
