"""El registro de archivos procesados: qué entró, cuándo y qué dejó.

Por qué existe. Hasta hoy el único rastro de que un archivo se procesó era que
hubiera cambiado de carpeta, y el único rastro de que **falló** era una línea en
un log del servidor. Eso es lo que dejó pasar el atasco de Stripe: catorce
archivos fallando todos los días durante un mes, con el vigilante disparando la
cadena cada 15 minutos, sin que nadie lo viera desde ninguna pantalla.

Dos usos, y el segundo es el que ataca un agujero viejo:

1. **Ver qué pasó.** Una tabla que `financial-platform` puede mostrar: archivo,
   fuente, cuándo, cuántas filas trajo, cuántos pagos nuevos dejó, y si terminó
   bien o mal.
2. **Avisar del archivo repetido.** Volver a subir un PDF ya procesado hoy
   **duplica los pagos en silencio** —la llave lleva el documento adentro, así
   que entran como nuevos—. Con la huella del contenido guardada, la pantalla
   puede advertirlo antes de dejarlo entrar.

⚠️ El registro es memoria, no parte del proceso de la plata: **si falla, se
avisa y la corrida sigue**. Un pago no se puede perder porque no se pudo anotar
de qué archivo vino.

⚠️ Las filas de esta tabla NO se borran nunca, aunque el archivo sí caduque a
los 3 meses. El aviso de "esto ya se procesó" tiene que valer para siempre.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os

from utils import dry_run
from utils.supabase import _headers, http

log = logging.getLogger(__name__)

TABLA = 'archivos_procesados'


def huella(contenido: io.BytesIO) -> str:
    """SHA-256 del contenido, dejando el archivo listo para volver a leerse.

    Se compara por CONTENIDO y no por nombre a propósito: dos archivos distintos
    pueden llamarse igual (`unified_payments (3).csv` se repite cada semana) y
    el mismo contenido puede llegar con otro nombre.
    """
    contenido.seek(0)
    digest = hashlib.sha256(contenido.read()).hexdigest()
    contenido.seek(0)
    return digest


def anotar(archivo: dict, *, huella_contenido: str = '', tamano: int | None = None,
           filas_leidas: int | None = None, pagos_nuevos: int | None = None,
           resultado: str = 'ok', detalle: str = '') -> None:
    """Deja constancia de un archivo procesado. Nunca lanza."""
    url = os.environ.get('SUPABASE_URL', '')
    srk = os.environ.get('SUPABASE_SERVICE_ROLE_KEY', '')
    if not (url and srk):
        return

    fila = {
        'fuente':       archivo.get('fuente') or '',
        'nombre':       archivo.get('name') or '',
        'huella':       huella_contenido,
        'tamano_bytes': tamano,
        'origen':       archivo.get('origen') or '',
        'ruta':         archivo.get('id') or '',
        'lote':         archivo.get('lote') or None,
        'filas_leidas': filas_leidas,
        'pagos_nuevos': pagos_nuevos,
        'resultado':    resultado,
        'detalle':      (detalle or '')[:2000] or None,
    }

    # También pasa por el modo simulación: es una escritura a producción, y el
    # dry-run tiene que poder correr sin dejar rastro.
    if dry_run.registrar(TABLA, 'insert', [fila]):
        return

    try:
        resp = http.post(f'{url}/rest/v1/{TABLA}', json=[fila],
                         headers=_headers(srk, prefer='return=minimal'), timeout=30)
        resp.raise_for_status()
    except Exception as e:
        # A propósito NO se usa log.exception ni se relanza: que no se haya
        # podido anotar un archivo no puede tumbar la corrida que ya metió los
        # pagos. Si la tabla todavía no existe, esto es lo único que se ve.
        log.warning('No se pudo anotar %s en %s (%s). La corrida sigue.',
                    fila['nombre'], TABLA, e)
