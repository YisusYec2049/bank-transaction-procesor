"""El depósito: los archivos que sube el área desde la plataforma.

Es el segundo origen posible de un archivo, al lado de Google Drive. Vive en el
almacenamiento del mismo proyecto de Supabase que ya usa todo el pipeline, así
que no agrega cuentas, credenciales ni permisos que administrar: entra con la
misma service role de siempre.

La estructura copia a propósito la de Drive, para que el pipeline cambie de
puerta y no de lógica:

    <bucket>/entrada/<fuente>/<archivo>      ← lo que subió el área, sin procesar
    <bucket>/historico/<fuente>/<archivo>    ← lo ya procesado

`<fuente>` es el nombre corto de la bandeja ('wompi', 'bc2576', 'cartera_prev'),
así que **no hace falta una variable de entorno por carpeta**: se deriva. La
única variable nueva es `DEPOSITO_BUCKET`, y si está vacía este módulo se apaga
entero y el pipeline sigue leyendo solo de Drive — o sea que desplegar el código
antes de crear el bucket no rompe nada.

Este módulo NO decide nada: solo sabe hablar con el almacenamiento. Quién mira
primero, qué se archiva y cuándo lo decide `utils/origen.py`.
"""

from __future__ import annotations

import io
import logging
import os
from datetime import datetime, timedelta, timezone

from utils import dry_run
from utils.supabase import http

log = logging.getLogger(__name__)

ENTRADA = 'entrada'
HISTORICO = 'historico'

# Supabase deja un objeto vacío para que una "carpeta" exista aunque no tenga
# archivos. No es un archivo del área y no se debe intentar procesar.
_PLACEHOLDER = '.emptyFolderPlaceholder'

# El listado devuelve como máximo 100 objetos por página. Se pagina hasta
# agotar la carpeta por el mismo motivo que en Drive: sin paginar, los que se
# perderían son los ÚLTIMOS, o sea los más nuevos — justo los del día.
_POR_PAGINA = 100


def _config() -> tuple[str, str, str]:
    return (
        os.environ.get('DEPOSITO_BUCKET', '').strip(),
        os.environ.get('SUPABASE_URL', '').strip(),
        os.environ.get('SUPABASE_SERVICE_ROLE_KEY', '').strip(),
    )


def activo() -> bool:
    """¿Hay depósito configurado en esta corrida?

    Con esto en False el pipeline se comporta exactamente como antes de que el
    depósito existiera.
    """
    bucket, url, srk = _config()
    return bool(bucket and url and srk)


def _headers(srk: str) -> dict:
    return {'apikey': srk, 'Authorization': f'Bearer {srk}'}


def ruta(zona: str, fuente: str, nombre: str) -> str:
    return f'{zona}/{fuente}/{nombre}'


def listar(fuente: str) -> list[dict]:
    """Los archivos sin procesar de esa fuente, del más viejo al más reciente.

    Devuelve `{'id': ruta completa, 'name': nombre del archivo}` — la ruta hace
    de identificador porque es con lo que se descarga y se mueve, igual que el
    id de Drive.

    ⚠️ Ante un fallo del almacenamiento devuelve vacío y lo grita en el log.
    Cortar la corrida entera sería peor: dejaría fuera también los archivos que
    entran por Drive. Pero "vacío" acá significa que lo que subió el área no se
    va a procesar, así que el mensaje va en ERROR y no en WARNING — es
    exactamente la forma del atasco de Stripe, que estuvo un mes fallando en
    silencio.
    """
    bucket, url, srk = _config()
    if not (bucket and url and srk):
        return []

    prefijo = f'{ENTRADA}/{fuente}'
    encontrados: list[dict] = []
    offset = 0

    while True:
        try:
            resp = http.post(
                f'{url}/storage/v1/object/list/{bucket}',
                headers={**_headers(srk), 'Content-Type': 'application/json'},
                json={
                    'prefix': f'{prefijo}/',
                    'limit': _POR_PAGINA,
                    'offset': offset,
                    'sortBy': {'column': 'created_at', 'order': 'asc'},
                },
                timeout=30,
            )
            resp.raise_for_status()
            pagina = resp.json() or []
        except Exception:
            log.exception('DEPÓSITO [%s]: no se pudo listar la entrada. Los archivos que el '
                          'área haya subido a esa fuente NO se van a procesar en esta corrida.',
                          fuente)
            return []

        for obj in pagina:
            nombre = obj.get('name') or ''
            # Sin `id` es una carpeta, no un archivo.
            if not obj.get('id') or not nombre or nombre == _PLACEHOLDER:
                continue
            encontrados.append({'id': ruta(ENTRADA, fuente, nombre), 'name': nombre,
                                'created_at': obj.get('created_at')})

        if len(pagina) < _POR_PAGINA:
            return encontrados
        offset += _POR_PAGINA


def caducar(fuentes: list[str], dias: int = 90) -> int:
    """Borra del histórico lo que tenga más de `dias`. Devuelve cuántos borró.

    Decisión del usuario (2026-09-06): **tres meses**. Los archivos de algunas
    fuentes son acumulativos —cada export de Stripe trae casi todo el anterior
    más 3 o 4 pagos nuevos— así que guardarlos para siempre es guardar 17 veces
    lo mismo. Tres meses cubren la ventana donde de verdad se mide algo.

    ⚠️ Lo que NO se borra nunca son las filas de `archivos_procesados`: el aviso
    de "este archivo ya se procesó" tiene que valer para siempre, y ahí es donde
    vive.

    ⚠️ Solo toca el depósito. Los archivos de Drive son de otra cuenta y otra
    historia; ahí no se borra nada.
    """
    bucket, url, srk = _config()
    if not (bucket and url and srk):
        return 0

    corte = datetime.now(timezone.utc) - timedelta(days=dias)
    borrados = 0

    for fuente in fuentes:
        viejos: list[str] = []
        offset = 0
        while True:
            try:
                resp = http.post(
                    f'{url}/storage/v1/object/list/{bucket}',
                    headers={**_headers(srk), 'Content-Type': 'application/json'},
                    json={'prefix': f'{HISTORICO}/{fuente}/', 'limit': _POR_PAGINA,
                          'offset': offset,
                          'sortBy': {'column': 'created_at', 'order': 'asc'}},
                    timeout=30,
                )
                resp.raise_for_status()
                pagina = resp.json() or []
            except Exception:
                log.warning('DEPÓSITO [%s]: no se pudo revisar el histórico para caducar.', fuente)
                break

            for obj in pagina:
                nombre = obj.get('name') or ''
                if not obj.get('id') or not nombre or nombre == _PLACEHOLDER:
                    continue
                if _es_viejo(obj.get('created_at'), corte):
                    viejos.append(ruta(HISTORICO, fuente, nombre))

            if len(pagina) < _POR_PAGINA:
                break
            offset += _POR_PAGINA

        if not viejos:
            continue
        if dry_run.registrar(f'deposito:{HISTORICO}/{fuente}', 'delete', viejos):
            continue
        try:
            resp = http.delete(f'{url}/storage/v1/object/{bucket}',
                               headers={**_headers(srk), 'Content-Type': 'application/json'},
                               json={'prefixes': viejos}, timeout=60)
            resp.raise_for_status()
            borrados += len(viejos)
            log.info('DEPÓSITO [%s]: %d archivo(s) de más de %d días borrados del histórico.',
                     fuente, len(viejos), dias)
        except Exception:
            log.warning('DEPÓSITO [%s]: no se pudieron borrar %d archivo(s) viejo(s).',
                        fuente, len(viejos))

    return borrados


def _es_viejo(creado: str | None, corte: datetime) -> bool:
    """Sin fecha NO se borra: ante la duda se conserva. Perder un archivo es
    irreversible; que sobre uno solo ocupa lugar."""
    if not creado:
        return False
    try:
        return datetime.fromisoformat(creado.replace('Z', '+00:00')) < corte
    except ValueError:
        return False


def descargar(ruta_archivo: str) -> io.BytesIO:
    """Baja el contenido del archivo. Falla a gritos: si no se puede leer, no
    hay nada que procesar y el llamador tiene que enterarse."""
    bucket, url, srk = _config()
    if not (bucket and url and srk):
        raise RuntimeError('DEPÓSITO no configurado: falta DEPOSITO_BUCKET, SUPABASE_URL '
                           'o SUPABASE_SERVICE_ROLE_KEY.')

    resp = http.get(f'{url}/storage/v1/object/{bucket}/{ruta_archivo}',
                    headers=_headers(srk), timeout=120)
    resp.raise_for_status()
    return io.BytesIO(resp.content)


def _ya_esta_en_historico(fuente: str, nombre: str) -> bool:
    """¿El histórico de esa fuente ya tiene un archivo con ese nombre exacto?

    `search` del listado busca por coincidencia parcial, así que el nombre se
    compara aparte: `Payu UC.xlsx` no puede darse por ocupado porque exista
    `Payu UC (2026-09-08).xlsx`.

    Si no se puede preguntar, se responde que NO está: así el comportamiento
    ante un fallo del almacenamiento es el de siempre (intentar con el nombre
    original) en vez de renombrar archivos por una consulta que no respondió.
    """
    bucket, url, srk = _config()
    try:
        resp = http.post(
            f'{url}/storage/v1/object/list/{bucket}',
            headers={**_headers(srk), 'Content-Type': 'application/json'},
            json={'prefix': f'{HISTORICO}/{fuente}/', 'limit': _POR_PAGINA,
                  'offset': 0, 'search': nombre},
            timeout=30,
        )
        resp.raise_for_status()
        return any((o.get('name') or '') == nombre for o in (resp.json() or []))
    except Exception:
        log.warning('DEPÓSITO [%s]: no se pudo revisar si %s ya está en el histórico.',
                    fuente, nombre)
        return False


def _nombre_para_historico(fuente: str, nombre: str) -> str:
    """El nombre con el que se archiva, esquivando el que ya esté ocupado.

    Existe porque mover en el depósito es ESCRIBIR UNA RUTA, no cambiar un
    archivo de carpeta como en Drive: si la ruta ya existe, el almacenamiento
    responde 400 y el archivo se queda sin archivar.

    Y no es un caso raro, es el de todos los días: los archivos de referencia
    llegan siempre con el mismo nombre (`Payu UC.xlsx` está fijo en
    `sync_cartera.py`), así que el segundo día chocan sí o sí. El 2026-09-08 eso
    tumbó la corrida entera y 152 pagos no entraron.

    Se le pega la fecha, y la hora si ese mismo día ya se archivó otro igual:

        Payu UC.xlsx  →  Payu UC (2026-09-08).xlsx  →  Payu UC (2026-09-08 09-56-49).xlsx

    Los dos se conservan a propósito: el archivo es la única copia de lo que
    entró ese día, y el histórico del depósito ya se limpia solo a los 3 meses.
    """
    if not _ya_esta_en_historico(fuente, nombre):
        return nombre

    base, punto, ext = nombre.rpartition('.')
    if not punto:            # un nombre sin extensión: el sufijo va al final
        base, ext = nombre, ''
    sufijo_ext = f'.{ext}' if punto else ''

    ahora = datetime.now(timezone.utc)
    for marca in (ahora.strftime('%Y-%m-%d'), ahora.strftime('%Y-%m-%d %H-%M-%S')):
        candidato = f'{base} ({marca}){sufijo_ext}'
        if not _ya_esta_en_historico(fuente, candidato):
            log.info('DEPÓSITO [%s]: "%s" ya está en el histórico, se archiva como "%s".',
                     fuente, nombre, candidato)
            return candidato

    # Hasta acá no se llega archivando de a un archivo por vez: el segundo
    # candidato lleva los segundos. Si pasara, se avisa y se deja fallar el
    # move — antes que archivar dos cosas distintas bajo el mismo nombre.
    log.error('DEPÓSITO [%s]: no se encontró un nombre libre en el histórico para %s.',
              fuente, nombre)
    return nombre


def mover_a_historico(ruta_archivo: str, fuente: str, nombre: str) -> None:
    """Archiva el archivo ya procesado dentro del mismo depósito.

    ⚠️ Pasa por el modo simulación, igual que su equivalente de Drive: mover un
    archivo al Histórico es de las escrituras que más duelen, porque si la
    corrida no lo procesó bien, moverlo lo esconde. En dry-run se registra la
    intención y el archivo se queda donde está.
    """
    bucket, url, srk = _config()
    if not (bucket and url and srk):
        raise RuntimeError('DEPÓSITO no configurado.')

    if dry_run.registrar(f'deposito:{ruta(HISTORICO, fuente, nombre)}', 'move', [ruta_archivo]):
        return

    destino = ruta(HISTORICO, fuente, _nombre_para_historico(fuente, nombre))
    resp = http.post(
        f'{url}/storage/v1/object/move',
        headers={**_headers(srk), 'Content-Type': 'application/json'},
        json={'bucketId': bucket, 'sourceKey': ruta_archivo, 'destinationKey': destino},
        timeout=60,
    )
    resp.raise_for_status()
    log.info('DEPÓSITO: archivo movido a histórico: %s -> %s', ruta_archivo, destino)
