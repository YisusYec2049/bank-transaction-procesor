#!/opt/matching-test/venv/bin/python3
"""
vigilante.py — ¿hay algo nuevo que procesar en Drive?

Responde con el CÓDIGO DE SALIDA, para encadenarlo con `&&` en el cron:

    0 → sí hay trabajo (el cron sigue y corre la cadena completa)
    1 → no hay nada (el cron se detiene ahí, sin gastar una corrida)

Para qué existe
---------------
El pipeline corre una vez al día a las 9:30 (hora Colombia) y esa sigue
siendo LA corrida: el equipo sube todo antes de esa hora y el lote se procesa
completo, de una sola vez, para poder revisarlo sobre un resultado quieto.

Este script es la EXCEPCIÓN, no el camino normal: cubre lo que llegó tarde.
Antes, un archivo subido a las 9:45 esperaba hasta el día siguiente o tocaba
apretar "Actualizar cruce" a mano.

Por eso en el crontab del VPS este script está acotado a las horas
POSTERIORES a la corrida diaria: `50 14 * * *` más `5,20,35,50 15-23 * * *`
— el servidor va en UTC, así que son las 9:50 a 18:50 de Colombia. Es a
propósito y no es un descuido: si corriera también antes de las 9:30, un
cargue hecho a las 8:00 se procesaría a las 8:05 y otro a las 8:40 dispararía
una segunda corrida parcial, justo lo contrario de "una corrida limpia con
todo el lote junto". Decisión del usuario, 2026-07-26; horario movido de
10:30/11:00 a 9:30/9:50 el 2026-08-10.

Son dos líneas de cron y no una porque en la hora 14 UTC solo puede entrar el
minuto :50. Los minutos :05 y :20 caerían antes de la corrida del día, y :35
encima de ella — a cinco minutos de que arranque, con el `flock` todavía
tomado.

Es deliberadamente barato: solo lista carpetas (unas pocas llamadas a Drive),
no descarga ni escribe nada. La corrida real la hacen los scripts de siempre,
encadenados después de este.

Qué carpetas vigila, y en qué orden
-----------------------------------
Primero los archivos de REFERENCIA (Payu UC, Ingresos PSE y PAYU y el
ReportePagosWompi) y después las bandejas de bancos y pasarelas. El orden es
el del proceso: la referencia es contra lo que se cruza, así que va antes que
los pagos. No cambia el resultado —se revisan todas las carpetas siempre, sin
salir en la primera que tenga algo— pero deja el log en el mismo orden en que
va a ocurrir todo después.

Lo que hace vigilable a una carpeta es una sola propiedad: **se vacía sola al
procesarse**. `procesar_todos.py` mueve cada archivo de banco a su Histórico,
y `sync_cartera.py` hace lo mismo con los de referencia. Procesado el
archivo, la carpeta queda vacía y el vigilante se calla — sin eso, dispararía
la cadena cada 15 minutos para siempre.

Las de referencia NO se vigilaban antes porque vivían en una carpeta única
(`CARTERA_DRIVE_FOLDER_ID`) sin Histórico propio, así que el archivo se
quedaba ahí después de cargarse. Eso dejó de ser cierto: hoy tienen carpeta e
Histórico dedicados en el `.env`. Agregadas el 2026-08-10, con la condición de
seguridad escrita en `_carpetas_referencia`: solo se vigila la carpeta que
tenga su Histórico configurado.

**CARTERA PREVENTIVA queda fuera a propósito** (decisión del usuario,
2026-08-10), aunque cumple la condición: para esa está el botón "Buscar
archivos nuevos" de la plataforma, que corre solo `sync_cartera.py` (~4 s)
en vez de la cadena completa (~3 min). Y no se pierde nada por esperar:
`sync_cartera.py` solo deja la cartera nueva EN ESPERA — el cambio de verdad
lo hace "Cargar Cartera", que dispara su propio reproceso.

El ReportePagosWompi se cuenta distinto que las demás: su carpeta conserva a
propósito el archivo más reciente (ver `_archivar_reportes_wompi` en
cruzar.py), así que "tener un archivo" es el estado normal y no puede ser la
señal. La señal es tener DOS O MÁS: llegó una entrega nueva y la anterior
todavía no se ha archivado.
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

# Los archivos de REFERENCIA contra los que se cruza, con el MISMO par de
# variables que lee sync_cartera.py: (etiqueta, variable de la carpeta,
# variable de su Histórico). Las dos hacen falta — ver `_carpetas_referencia`.
#
# CARTERA PREVENTIVA NO está acá a propósito (decisión del usuario,
# 2026-08-10): esa la trae el botón "Buscar archivos nuevos" de la plataforma,
# que corre solo `sync_cartera.py` (~4 s) en vez de la cadena entera. Si una
# sesión futura la agrega "por consistencia", subir esa cartera pasaría a
# disparar el pipeline completo sin que nadie lo haya pedido.
CARPETAS_REFERENCIA = [
    ('Payu UC.xlsx',             'PAYU_UC_FOLDER_ID',  'PAYU_UC_HIST_FOLDER_ID'),
    ('Ingresos PSE y PAYU.xlsx', 'INGRESOS_FOLDER_ID', 'INGRESOS_HIST_FOLDER_ID'),
]


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


def _carpetas_referencia() -> list[tuple[str, str]]:
    """(etiqueta, folder_id) de las carpetas de referencia que es SEGURO vigilar.

    La condición es que la carpeta tenga configurado su Histórico: sin él,
    `sync_cartera.py` carga el archivo y lo DEJA donde está (solo avisa con un
    warning), así que la carpeta nunca se vacía y el vigilante dispararía la
    cadena cada 15 minutos para siempre. Con Histórico, el archivo sale al
    cargarse y el vigilante se calla solo — la misma propiedad que hace que
    esto funcione con las bandejas de los bancos.

    Por eso se exigen las DOS variables y no se usa el fallback
    `CARTERA_DRIVE_FOLDER_ID` (la carpeta única de antes, que no tiene
    Histórico propio). Si alguien le quita el Histórico a una carpeta, esa
    deja de vigilarse sola en vez de entrar en bucle.
    """
    carpetas = []
    for etiqueta, var_folder, var_hist in CARPETAS_REFERENCIA:
        folder_id = os.environ.get(var_folder, '')
        hist_id   = os.environ.get(var_hist, '')
        if folder_id and hist_id:
            carpetas.append((etiqueta, folder_id))
        elif folder_id:
            log.debug('%s no se vigila: le falta %s en .env.', etiqueta, var_hist)
    return carpetas


def hay_trabajo(drive) -> bool:
    encontrado = False

    # 1. Los archivos de REFERENCIA, primero: son contra lo que se cruza.
    for etiqueta, folder_id in _carpetas_referencia():
        archivos = list_files(drive, folder_id)
        if archivos:
            log.info('%s: %d archivo(s) esperando -> %s',
                     etiqueta, len(archivos), ', '.join(f['name'] for f in archivos[:5]))
            encontrado = True

    # El reporte de WOMPI también es referencia, pero se cuenta distinto:
    # 1 archivo es el estado normal (su carpeta conserva a propósito el más
    # reciente, ver `_archivar_reportes_wompi` en cruzar.py), así que "tener
    # un archivo" no puede ser la señal. La señal es tener DOS O MÁS.
    reporte_folder = os.environ.get('WOMPI_REPORTE_DRIVE_FOLDER_ID', '')
    if reporte_folder:
        reportes = find_all_files(drive, reporte_folder, WOMPI_REPORTE_PATTERN)
        if len(reportes) >= 2:
            log.info('ReportePagosWompi: %d entregas sin archivar -> %s',
                     len(reportes), ', '.join(f['name'] for f in reportes))
            encontrado = True

    # 2. Las bandejas de los bancos y pasarelas, después.
    for etiqueta, folder_id in _bandejas():
        archivos = list_files(drive, folder_id)
        if archivos:
            log.info('%s: %d archivo(s) esperando -> %s',
                     etiqueta, len(archivos), ', '.join(f['name'] for f in archivos[:5]))
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
