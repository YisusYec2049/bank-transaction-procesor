#!/opt/matching-test/venv/bin/python3
"""
vigilante.py — ¿hay algo nuevo que procesar en Drive?

Responde con el CÓDIGO DE SALIDA, para encadenarlo con `&&` en el cron:

    0 → sí hay trabajo (el cron sigue y corre la cadena completa)
    1 → no hay nada (el cron se detiene ahí, sin gastar una corrida)

Para qué existe
---------------
El pipeline corre una vez al día a las 10:30 (hora Colombia) y esa sigue
siendo LA corrida: el equipo sube todo antes de esa hora y el lote se procesa
completo, de una sola vez, para poder revisarlo sobre un resultado quieto.

Este script es la EXCEPCIÓN, no el camino normal: cubre lo que llegó tarde.
Antes, un archivo subido a las 10:45 esperaba hasta el día siguiente o tocaba
apretar "Actualizar cruce" a mano.

Por eso en el crontab del VPS la línea de este script está acotada a las
horas POSTERIORES a la corrida diaria (`5,20,35,50 16-23 * * *` — el servidor
va en UTC, así que son las 11:00 a 18:59 de Colombia). Es a propósito y no es
un descuido: si corriera también en la mañana, un cargue hecho a las 8:00 se
procesaría a las 8:05 y otro a las 8:40 dispararía una segunda corrida
parcial, justo lo contrario de "una corrida limpia con todo el lote junto".
Decisión del usuario, 2026-07-26.

Es deliberadamente barato: solo lista carpetas (unas pocas llamadas a Drive),
no descarga ni escribe nada. La corrida real la hacen los scripts de siempre,
encadenados después de este.

Qué carpetas vigila
-------------------
SOLO las bandejas de entrada de bancos/pasarelas, porque `procesar_todos.py`
las VACÍA en cada corrida (mueve cada archivo a Histórico). Esa propiedad es
la que hace que esto no se dispare en bucle: procesado el archivo, la carpeta
queda vacía y el vigilante se calla solo.

Por eso NO se vigilan las carpetas de cartera (Payu UC / Ingresos / Cartera
Preventiva): mientras el `.env` siga usando el fallback `CARTERA_DRIVE_FOLDER_ID`
(carpeta única, sin Histórico propio), esos archivos se quedan donde están
después de cargarse y el vigilante dispararía la cadena para siempre. Si algún
día se configuran las carpetas dedicadas con su Histórico, se pueden agregar.

El ReportePagosWompi es un caso aparte: su carpeta conserva a propósito el
archivo más reciente (ver `_archivar_reportes_wompi` en cruzar.py), así que
"tener un archivo" es el estado normal y no puede ser la señal. La señal es
tener DOS O MÁS: significa que llegó una entrega nueva y la anterior todavía
no se ha archivado.
"""

import argparse
import logging
import os
import sys

from dotenv import load_dotenv

from procesar_todos import BANCOS, BANCOS_BANCOLOMBIA
from utils.drive import build_drive_service, find_all_files, list_files

# force=True porque importar procesar_todos ya configuró el logger raíz, y la
# primera llamada gana: sin esto el prefijo [vigilante] se perdía y en
# pipeline.log (compartido con toda la cadena) no se distinguía quién habla.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s [vigilante] %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%S',
    stream=sys.stdout,
    force=True,
)
log = logging.getLogger(__name__)

WOMPI_REPORTE_PATTERN = 'ReportePagosWompi'


def _bandejas() -> list[tuple[str, str]]:
    """(etiqueta, folder_id) de cada bandeja de entrada configurada."""
    prefijos = [cfg['prefix'] for cfg in BANCOS_BANCOLOMBIA.values()]
    prefijos += [cfg['prefix'] for cfg in BANCOS.values()]
    prefijos += ['PAYU', 'PAYU_MONEDA']

    bandejas = []
    for p in prefijos:
        folder_id = os.environ.get(f'{p}_INBOX_FOLDER_ID', '')
        if folder_id:
            bandejas.append((p, folder_id))
    return bandejas


def hay_trabajo(drive) -> bool:
    encontrado = False

    for etiqueta, folder_id in _bandejas():
        archivos = list_files(drive, folder_id)
        if archivos:
            log.info('%s: %d archivo(s) esperando -> %s',
                     etiqueta, len(archivos), ', '.join(f['name'] for f in archivos[:5]))
            encontrado = True

    # El reporte de WOMPI: 1 archivo es el estado normal (se conserva a
    # propósito); 2 o más significa que llegó una entrega nueva.
    reporte_folder = os.environ.get('WOMPI_REPORTE_DRIVE_FOLDER_ID', '')
    if reporte_folder:
        reportes = find_all_files(drive, reporte_folder, WOMPI_REPORTE_PATTERN)
        if len(reportes) >= 2:
            log.info('ReportePagosWompi: %d entregas sin archivar -> %s',
                     len(reportes), ', '.join(f['name'] for f in reportes))
            encontrado = True

    return encontrado


def main():
    load_dotenv()

    parser = argparse.ArgumentParser(
        description='Sale con 0 si hay archivos nuevos en Drive, con 1 si no hay nada.')
    parser.add_argument('--dry-run', action='store_true',
                        help='Informativo: reporta pero siempre sale con 0.')
    args = parser.parse_args()

    sa_json = os.environ.get('GOOGLE_SA_JSON', '')
    if not sa_json:
        log.error('GOOGLE_SA_JSON no configurado.')
        sys.exit(0 if args.dry_run else 0)

    try:
        drive = build_drive_service(sa_json)
        encontrado = hay_trabajo(drive)
    except Exception:
        # Ante un fallo de Drive se deja pasar la cadena a propósito: un
        # cargue que se queda sin procesar es peor que una corrida de más,
        # que además es idempotente y está protegida por flock.
        log.exception('No se pudo consultar Drive; se deja pasar la cadena por precaución.')
        sys.exit(0)

    if encontrado:
        log.info('Hay trabajo: se dispara el pipeline.')
        sys.exit(0)

    log.info('Nada nuevo en Drive.')
    sys.exit(0 if args.dry_run else 1)


if __name__ == '__main__':
    main()
