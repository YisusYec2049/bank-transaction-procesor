#!/opt/matching-test/venv/bin/python3
"""
sync_cartera.py — sincroniza los Excel de referencia del cruce de cartera a Supabase.

Los 3 archivos (Payu UC, Ingresos PSE y PAYU, Cartera Preventiva) los sube el
área desde `financial-platform`, cada uno a SU carpeta del depósito
(`payu_uc`, `ingresos`, `cartera_prev`). Ya no hay ninguna variable de entorno
de carpetas: el nombre de la fuente ES la carpeta, y el destino de archivado se
deriva de él. **Google Drive se desconectó el 2026-09-08.**

Los 3 archivos son OPCIONALES: si un archivo no está en su bandeja esta
corrida, no es error — se salta y se mantiene lo que ya se cargó antes.
Ingresos PSE y PAYU en particular solo se sube el primer y el último día de
la semana; su ausencia el resto de los días es normal. Tras cargar un
archivo con éxito, se MUEVE a su histórico (nunca se borra) para que
la bandeja quede limpia — así "no hay archivo esta corrida" y
"ya se cargó" son indistinguibles por diseño, y detectar si Cartera
Preventiva tiene una versión nueva pendiente de activar se reduce a mirar
`cartera_cargas` (ver más abajo), sin comparar nombres de archivo.

Escrituras por archivo:
  - Payu UC.xlsx            → cartera_inscrip (replace_table)
  - Ingresos PSE y PAYU.xlsx → cartera_ingresos_bancolombia_2576 / _2833 /
                                _wompi / _stripe_usa (replace_table cada una)
  - CARTERA PREVENTIVA*.xlsx → cartera_preventiva_staging (replace_table) —
    YA NO escribe sobre cartera_preventiva (la tabla VIVA). Cada carga nueva
    marca una fila `cartera_cargas(estado='staged')` — el marcador que
    prende el banner "hay cartera pendiente" en financial-platform. La
    tabla viva solo cambia cuando alguien aprieta el botón "Cargar Cartera"
    (ver activar_cartera.py), nunca por este script.

Se corre por cron (encadenado con cruzar.py, ver crontab del VPS) o
manualmente cada vez que el equipo sube un Excel nuevo.
"""

import argparse
import io
import logging
import os
import sys
from datetime import datetime

import pytz
from dotenv import load_dotenv

from utils import deposito, dry_run, registro
from utils.excel_cartera import (
    read_bancolombia_2576,
    read_bancolombia_2833,
    read_cartera_preventiva,
    read_inscrip,
    read_stripe_usa,
    read_wompi,
)
from utils.origen import Bandeja, descargar, mas_reciente, mover_a_historico
from utils.supabase import (
    delete_by_keys,
    replace_cartera_preventiva_staging,
    replace_table,
    select_all,
    upsert_cartera_cargas,
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%S',
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

PAYU_UC_FILENAME     = 'Payu UC.xlsx'
INGRESOS_FILENAME    = 'Ingresos PSE y PAYU.xlsx'
CARTERA_PREV_PATTERN = 'CARTERA PREVENTIVA'

# Código de salida cuando un archivo de cruce se subió y NO se pudo leer.
# Se distingue del fallo genérico (1) a propósito: la corrida no se rompió, se
# FRENÓ, y son dos mensajes distintos en la pantalla — "no se procesó nada
# porque un archivo de cruce está mal" contra "la corrida falló".
SALIDA_CRUCE_ILEGIBLE = 3


def _registrar_carga_staged(supabase_url: str, srk: str, filas: int) -> str:
    """Marca en `cartera_cargas` que hay una versión nueva de Cartera
    Preventiva staged, sin activar. Si ya había una carga `staged` de una
    subida anterior (nunca activada), se borra su fila de control — el
    Excel nuevo ya reemplazó por completo el contenido de staging, así que
    esa carga vieja ya no existe en ningún lado y dejar su fila sería un
    'staged' fantasma. Devuelve el `carga_id` nuevo (timestamp ISO Bogotá)."""
    tz_bogota = pytz.timezone('America/Bogota')
    carga_id = datetime.now(tz_bogota).strftime('%Y-%m-%dT%H:%M:%S.%f%z')

    cargas = select_all(supabase_url, srk, 'cartera_cargas', select='carga_id,estado')
    staged_viejas = [c['carga_id'] for c in cargas if c.get('estado') == 'staged']
    if staged_viejas:
        delete_by_keys(supabase_url, srk, 'cartera_cargas', 'carga_id', staged_viejas)

    upsert_cartera_cargas(supabase_url, srk, [{
        'carga_id': carga_id, 'filas': filas, 'estado': 'staged',
    }])
    return carga_id


def _procesar_opcional(nombre: str, bandeja: Bandeja, cargar) -> dict | None:
    """Patrón común a los 3 archivos de referencia: buscar en su bandeja →
    si está, descargar + cargar (`cargar` hace el replace_table y devuelve
    CUÁNTAS filas cargó, 0 si no tocó la tabla) + mover a Histórico; si no
    está, loguear y seguir SIN error — los 3 son opcionales.

    Cada bandeja está dedicada a un solo tipo de archivo, así que se toma el
    más reciente que haya sin importar cómo se llame. **El nombre no decide
    nada**: lo que hace que un archivo sea el de esta bandeja es su estructura,
    que es justamente lo que `cargar` comprueba al leerlo.

    Devuelve `None` si todo fue bien, o el registro del fallo si el archivo se
    subió y no se pudo leer — ver la explicación del freno en `main()`.

    🔴 **"No lo subieron" y "lo subieron mal" son dos cosas distintas**, y esa
    es toda la regla de este archivo. Que falte es normal (Ingresos se sube
    unos días sí y otros no) y la corrida sigue con lo que ya estaba cargado.
    Que esté y no se pueda leer NO es normal: significa que el área cree que
    actualizó esa referencia y no es cierto, así que repartir pagos contra la
    versión vieja es aplicar plata de hoy sobre información de antes — y un
    pago se reparte UNA SOLA VEZ en su vida, así que deshacerlo después es
    descartar y asociar a mano, cuota por cuota."""
    archivo = mas_reciente(bandeja)
    if not archivo:
        log.info('%s: nadie lo subió esta corrida (bandeja "%s"), se omite.',
                 nombre, bandeja.fuente)
        return None

    log.info('Descargando %s ...', nombre)
    try:
        filas = cargar(archivo)
    except Exception as exc:
        # El archivo se queda en su bandeja a propósito: es lo que deja
        # reemplazarlo desde la pantalla y volver a procesar sin que nadie
        # toque el servidor.
        log.exception('%s: se subió a la bandeja "%s" y NO se pudo leer.', nombre, bandeja.fuente)
        motivo = f'{type(exc).__name__}: {exc}'
        # Queda anotado en el registro que ya mira la pantalla, así el área ve
        # el archivo en rojo con su motivo sin que nadie lea un log del
        # servidor — que es lo que dejó pasar el atasco de Stripe un mes.
        registro.anotar(archivo, resultado='error', detalle=f'No se pudo leer. {motivo}')
        return {'nombre': nombre, 'archivo': archivo, 'motivo': motivo}

    if not filas:
        return None  # `cargar` ya logueó por qué no se movió (ej. lectura vacía)

    registro.anotar(archivo, filas_leidas=filas, resultado='ok')

    # ⚠️ Archivar es el ÚLTIMO paso y el menos importante: la tabla de
    # referencia ya quedó cargada arriba. Si falla, se avisa fuerte y la corrida
    # sigue — este script es el primero de la cadena, así que una excepción acá
    # se lleva por delante `procesar_todos.py` y NINGÚN pago del día entra. Pasó
    # el 2026-09-08: `Payu UC.xlsx` chocó con el del día anterior en el histórico
    # del depósito y quedaron 152 pagos ($122.112.305) sin ingresar.
    #
    # El costo de seguir es acotado y conocido: el archivo se queda en su
    # bandeja, así que el vigilante lo va a ver como trabajo nuevo cada 15
    # minutos hasta que alguien lo saque. Volver a cargarlo no ensucia nada
    # (los 3 archivos de referencia reemplazan su tabla entera), y es
    # muchísimo menos grave que frenar los pagos del día.
    try:
        if mover_a_historico(archivo, bandeja):
            log.info('%s movido a Histórico.', nombre)
    except Exception:
        log.exception('%s: se cargó bien pero NO se pudo archivar. Se queda en su bandeja y '
                      'la corrida SIGUE; hay que sacarlo a mano o se va a releer cada corrida.',
                      nombre)

    return None


def main() -> int:
    parser = argparse.ArgumentParser(description='Refresca las tablas de referencia desde la plataforma.')
    dry_run.agregar_flags(parser)
    args = parser.parse_args()
    dry_run.desde_args(args, 'sync')

    load_dotenv()

    supabase_url = os.environ.get('SUPABASE_URL', '')
    srk          = os.environ.get('SUPABASE_SERVICE_ROLE_KEY', '')

    faltantes = [n for n, v in [('SUPABASE_URL', supabase_url),
                                ('SUPABASE_SERVICE_ROLE_KEY', srk)] if not v]
    if faltantes:
        log.error('Variables faltantes en .env: %s', ', '.join(faltantes))
        sys.exit(1)

    # ⚠️ Sin depósito no hay de dónde leer NADA, pero eso NO corta la corrida:
    # este script es el primero de la cadena y un `exit(1)` acá se lleva por
    # delante la ingesta de pagos del día. Se grita en el log y se sigue.
    if not deposito.activo():
        log.error('DEPOSITO_BUCKET no está configurada: no hay de dónde leer los archivos de '
                  'referencia. Las tablas de referencia se quedan como estaban.')

    def _cargar_payu_uc(archivo) -> int:
        rows = read_inscrip(descargar(archivo))
        if not rows:
            log.warning('Payu UC: 0 filas leídas, se omite la carga (no se toca cartera_inscrip).')
            return 0
        replace_table(supabase_url, srk, 'cartera_inscrip', rows)
        return len(rows)

    def _cargar_ingresos(archivo) -> int:
        ingresos_bytes = descargar(archivo).read()
        bc2576_rows = read_bancolombia_2576(io.BytesIO(ingresos_bytes))
        bc2833_rows = read_bancolombia_2833(io.BytesIO(ingresos_bytes))
        wompi_rows  = read_wompi(io.BytesIO(ingresos_bytes))
        stripe_rows = read_stripe_usa(io.BytesIO(ingresos_bytes))
        if not (bc2576_rows or bc2833_rows or wompi_rows or stripe_rows):
            log.warning('Ingresos PSE y PAYU: 0 filas leídas en las 4 hojas, se omite la carga '
                        '(no se tocan las tablas cartera_ingresos_*).')
            return 0
        replace_table(supabase_url, srk, 'cartera_ingresos_bancolombia_2576', bc2576_rows)
        replace_table(supabase_url, srk, 'cartera_ingresos_wompi', wompi_rows)
        replace_table(supabase_url, srk, 'cartera_ingresos_stripe_usa', stripe_rows)
        # 2833 aparte: si su hoja no se puede leer, `read_bancolombia_2833`
        # lanza y la corrida se frena arriba. Acá se cubre el otro caso —la
        # hoja se leyó y vino vacía—, donde un replace_table con [] borraría el
        # mirror entero: se conserva lo cargado la vez anterior, que para los
        # pagos de 2833 sigue siendo mejor señal que ninguna.
        if bc2833_rows:
            replace_table(supabase_url, srk, 'cartera_ingresos_bancolombia_2833', bc2833_rows)
        else:
            log.error('BANCOL 2833: 0 filas, no se reemplaza cartera_ingresos_bancolombia_2833 '
                      '(se conserva la carga anterior).')
        return len(bc2576_rows) + len(bc2833_rows) + len(wompi_rows) + len(stripe_rows)

    def _cargar_cartera_prev(archivo) -> int:
        rows = read_cartera_preventiva(descargar(archivo))
        ok = replace_cartera_preventiva_staging(supabase_url, srk, rows)
        if not ok:
            return 0
        carga_id = _registrar_carga_staged(supabase_url, srk, len(rows))
        log.info('Cartera Preventiva: %d fila(s) a staging, carga %s marcada "staged".',
                  len(rows), carga_id)
        return len(rows)

    # El `nombre` que se pasa es solo la etiqueta para los logs; la bandeja es
    # la que sabe de dónde sale el archivo y a dónde se archiva.
    #
    # Se intentan los 3 aunque uno falle, a propósito: así una corrida reporta
    # TODOS los archivos que hay que corregir en vez de destapar uno por día.
    fallos = [f for f in (
        _procesar_opcional(PAYU_UC_FILENAME, Bandeja(fuente='payu_uc'), _cargar_payu_uc),
        _procesar_opcional(INGRESOS_FILENAME, Bandeja(fuente='ingresos'), _cargar_ingresos),
        _procesar_opcional(CARTERA_PREV_PATTERN, Bandeja(fuente='cartera_prev'),
                           _cargar_cartera_prev),
    ) if f]

    if fallos:
        # 🔴 EL FRENO. Este script es el primero de la cadena
        # (`sync_cartera && procesar_todos && cruzar && preventiva`), así que
        # salir distinto de 0 acá es lo que impide que se procese un solo pago.
        #
        # Es DELIBERADO, no el efecto de que se escape una excepción. Hasta el
        # 2026-09-21 pasaba lo segundo: el 21 subieron a la bandeja de Payu UC
        # un Excel que no era (sin la hoja `Inscrip`), el lector reventó, la
        # cadena murió y la pantalla dijo "no entró ningún archivo nuevo" —
        # porque un fallo y una corrida sin trabajo se veían igual. El área
        # estuvo dos horas sin saber que sus 9 archivos seguían sin entrar.
        #
        # Lo que cambia es que ahora se frena DICIENDO qué pasó, y con el
        # archivo culpable anotado en el registro que ya mira la pantalla.
        log.error('%s', '─' * 70)
        log.error('CORRIDA FRENADA: %d archivo(s) de cruce se subieron y no se pudieron leer.',
                  len(fallos))
        for f in fallos:
            log.error('  · %s (bandeja "%s") → %s',
                      f['archivo'].get('name'), f['archivo'].get('fuente'), f['motivo'])
        log.error('NO se procesó ningún pago. Los archivos se quedan en su bandeja: al '
                  'reemplazarlos por los correctos y volver a procesar, la corrida sigue sola.')
        log.error('%s', '─' * 70)
        return SALIDA_CRUCE_ILEGIBLE

    log.info('sync_cartera.py completado.')
    return 0


if __name__ == '__main__':
    codigo = main()
    # El resumen de la simulación se imprime SIEMPRE, también cuando la corrida
    # se frena: si no, probar el freno con `--dry-run` no mostraría nada.
    if dry_run.activo():
        log.warning('%s', dry_run.resumen())
    if codigo:
        sys.exit(codigo)
