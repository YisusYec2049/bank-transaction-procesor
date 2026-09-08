#!/opt/matching-test/venv/bin/python3
"""
vigilante.py — ¿hay algo nuevo que procesar?

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

Es deliberadamente barato: solo lista bandejas (unas pocas llamadas al depósito),
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

Lo que hace vigilable a una bandeja es una sola propiedad: **se vacía sola al
procesarse**. `procesar_todos.py` mueve cada archivo de banco a su histórico,
y `sync_cartera.py` hace lo mismo con los de referencia. Procesado el
archivo, la bandeja queda vacía y el vigilante se calla — sin eso, dispararía
la cadena cada 15 minutos para siempre.

Desde que los archivos entran por la plataforma (Drive se desconectó el
2026-09-08) esa propiedad se cumple SIEMPRE, porque el destino de archivado se
deriva de la fuente y no de una variable que alguien pueda olvidar. Antes había
que exigirle a cada carpeta de Drive su Histórico configurado, y por eso las de
referencia no se vigilaron hasta el 2026-08-10.

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
import sys

from dotenv import load_dotenv

from procesar_todos import BANCOS, BANCOS_BANCOLOMBIA
from utils import deposito
from utils.origen import Bandeja, hay_de_donde_leer, listar, todos_los_que_contienen

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
#
# El primer valor es el nombre de la FUENTE, que es además la carpeta dentro
# del depósito — tiene que coincidir con `FUENTES_DEL_DEPOSITO` en
# `procesar_todos.py` y con lo que escribe la pantalla de carga.
CARPETAS_REFERENCIA = [
    ('payu_uc',  'Payu UC.xlsx'),
    ('ingresos', 'Ingresos PSE y PAYU.xlsx'),
]


def _bandejas() -> list[tuple[str, Bandeja]]:
    """(etiqueta, bandeja) de cada bandeja de pagos que se vigila.

    La lista sale de las FUENTES, que son las mismas que procesa
    `procesar_todos.py`. Antes salía de las carpetas de Drive configuradas, y
    eso ató lo que el vigilante mira a una variable de entorno: el día que se
    desconectó Drive se habría quedado mirando una lista VACÍA, sin volver a
    avisar nunca de un archivo subido por la plataforma — que es justamente su
    razón de existir.
    """
    if not hay_de_donde_leer(Bandeja(fuente='bc2576')):
        return []
    fuentes = list({**BANCOS_BANCOLOMBIA, **BANCOS}) + ['payu', 'payu_moneda']
    return [(f, Bandeja(fuente=f)) for f in fuentes]


def _carpetas_referencia() -> list[tuple[str, Bandeja]]:
    """(etiqueta, bandeja) de las referencias que es SEGURO vigilar.

    La condición de siempre: que el archivo SALGA de su bandeja al cargarse. Si
    no sale, la bandeja nunca se vacía y el vigilante dispara la cadena cada 15
    minutos para siempre.

    En el depósito eso se cumple SIEMPRE, porque el destino de archivado se
    deriva de la fuente y no de una variable que alguien pueda olvidar. Era la
    condición que en Drive obligaba a exigir una carpeta de Histórico
    configurada.

    CARTERA PREVENTIVA sigue fuera a propósito (ver el comentario de
    `CARPETAS_REFERENCIA`).
    """
    if not hay_de_donde_leer(Bandeja(fuente='payu_uc')):
        return []
    return [(etiqueta, Bandeja(fuente=fuente)) for fuente, etiqueta in CARPETAS_REFERENCIA]


def hay_trabajo() -> bool:
    encontrado = False

    # 1. Los archivos de REFERENCIA, primero: son contra lo que se cruza.
    for etiqueta, bandeja in _carpetas_referencia():
        archivos = listar(bandeja)
        if archivos:
            log.info('%s: %d archivo(s) esperando -> %s',
                     etiqueta, len(archivos), ', '.join(f['name'] for f in archivos[:5]))
            encontrado = True

    # El reporte de WOMPI también es referencia, pero se cuenta distinto:
    # 1 archivo es el estado normal (su carpeta conserva a propósito el más
    # reciente, ver `_archivar_reportes_wompi` en cruzar.py), así que "tener
    # un archivo" no puede ser la señal. La señal es tener DOS O MÁS.
    bandeja_reporte = Bandeja(fuente='wompi_reporte')
    if hay_de_donde_leer(bandeja_reporte):
        reportes = todos_los_que_contienen(bandeja_reporte, WOMPI_REPORTE_PATTERN)
        if len(reportes) >= 2:
            log.info('ReportePagosWompi: %d entregas sin archivar -> %s',
                     len(reportes), ', '.join(f['name'] for f in reportes))
            encontrado = True

    # 2. Las bandejas de los bancos y pasarelas, después.
    for etiqueta, bandeja in _bandejas():
        archivos = listar(bandeja)
        if archivos:
            log.info('%s: %d archivo(s) esperando -> %s',
                     etiqueta, len(archivos), ', '.join(f['name'] for f in archivos[:5]))
            encontrado = True

    return encontrado


def main():
    load_dotenv()

    parser = argparse.ArgumentParser(
        description='Sale con 0 si hay archivos nuevos por procesar, con 1 si no hay nada.')
    parser.add_argument('--dry-run', action='store_true',
                        help='Informativo: reporta pero siempre sale con 0.')
    args = parser.parse_args()

    # ⚠️ Este script responde con el CÓDIGO DE SALIDA, así que una condición de
    # error nunca puede salir con 0 "por las dudas" sin mirar antes si de verdad
    # hay algo que revisar: 0 significa "corré la cadena completa", y repetido
    # cada 15 minutos es la forma exacta del atasco de Stripe.
    if not deposito.activo():
        log.error('DEPOSITO_BUCKET no está configurada: el vigilante no tiene dónde mirar.')
        sys.exit(1)

    try:
        encontrado = hay_trabajo()
    except Exception:
        # Ante un fallo del almacenamiento se deja pasar la cadena a propósito:
        # un cargue que se queda sin procesar es peor que una corrida de más,
        # que además es idempotente y está protegida por flock.
        log.exception('No se pudo consultar el depósito; se deja pasar la cadena por precaución.')
        sys.exit(0)

    if encontrado:
        log.info('Hay trabajo: se dispara el pipeline.')
        sys.exit(0)

    log.info('Nada nuevo por procesar.')
    sys.exit(0 if args.dry_run else 1)


if __name__ == '__main__':
    main()
