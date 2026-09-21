"""¿Este archivo sirve para la caja donde lo subieron?

Responde esa pregunta **abriendo el archivo con el mismo lector que lo va a
procesar en la corrida**. Esa es toda la idea, y es lo que hace que la respuesta
valga: si el lector puede, sirve.

Por qué vive acá y no en la pantalla. El 2026-09-18 el área le cambió el nombre
a una hoja dentro de `Ingresos PSE y PAYU` (`BANCOL 2833` → `PREBANCOLOMBIA
2833`) y el pipeline dejó de leerla **cuatro días sin que nadie se enterara**.
Si la pantalla tuviera su propia lista de nombres de hoja, habría seguido
diciendo "archivo correcto" todo ese tiempo — un semáforo que miente en verde es
peor que no tener semáforo, porque el área le cree. Acá no puede desfasarse:
`_CHEQUEOS` usa las mismas funciones que `sync_cartera.py`, `procesar_todos.py`
y `cruzar.py`.

⚠️ **El nombre del archivo no decide nada.** Un archivo llamado como sea, si
tiene la estructura de su caja, está bien; y un `Payu UC.xlsx` que por dentro sea
otra cosa está mal. Requisito explícito del usuario (2026-09-21), y además el
único que funciona: a las cajas de bancos les caen nombres distintos todos los
días.

Los tres veredictos, y qué significa cada uno para quien lo lee:

  ok                   — sirve. Se devuelve además cuántas filas trajo, que es
                         lo que delata un archivo cortado a la mitad.
  archivo_incorrecto   — esto no es el archivo de esta caja. *Subieron otra
                         cosa.*
  formato_incorrecto   — sí es el archivo, pero le cambiaron algo adentro (una
                         hoja, una columna). *Hay que corregirlo.*

La diferencia entre los dos rojos es la que pidió el usuario, y se decide así:
si el archivo no se reconoce como el de su caja es el primero; si se reconoce y
algo adentro falla, el segundo.
"""

from __future__ import annotations

import io
import logging

import pdfplumber

from fuentes import bancolombia_2576 as mod_bc2576
from fuentes import bancolombia_2833 as mod_bc2833
from fuentes import colpatria as mod_colpatria
from fuentes import davivienda as mod_davivienda
from fuentes import payu as mod_payu
from fuentes import placetopay as mod_placetopay
from fuentes import stripe as mod_stripe
from fuentes import wompi as mod_wompi
from utils.excel_cartera import (
    read_bancolombia_2576,
    read_bancolombia_2833,
    read_cartera_preventiva,
    read_inscrip,
    read_pagos_wompi_reporte,
    read_stripe_usa,
    read_wompi,
)

log = logging.getLogger(__name__)

OK                 = 'ok'
ARCHIVO_INCORRECTO = 'archivo_incorrecto'
FORMATO_INCORRECTO = 'formato_incorrecto'


class FormatoInvalido(Exception):
    """El archivo ES el de su caja, pero algo adentro no se puede leer."""


# Lo que se escribe en "se espera el archivo ___". Sale de acá y no de la
# pantalla porque hay una caja donde el nombre NO es el de su fuente: en
# `cartera_prev` se suben dos archivos distintos —la Cartera normal y la
# Preventiva, las dos válidas—, así que el texto dice "CARTERA" a secas
# (decisión del usuario, 2026-09-21).
ESPERA = {
    'payu_uc':       'PAYU UC',
    'ingresos':      'INGRESOS PSE Y PAYU',
    'cartera_prev':  'CARTERA',
    'wompi_reporte': 'REPORTE PAGOS WOMPI',
    'bc2576':        'BANCOLOMBIA 2576',
    'bc2833':        'BANCOLOMBIA 2833',
    'wompi':         'WOMPI',
    'stripe':        'STRIPE',
    'placetopay':    'PLACETOPAY',
    'payu':          'PAYU',
    'payu_moneda':   'PAYU MONEDA',
    'colpatria':     'COLPATRIA',
    'davivienda':    'DAVIVIENDA',
}


def _excel(contenido: io.BytesIO) -> io.BytesIO:
    """Una copia fresca: los lectores consumen el buffer y hay chequeos que
    abren el mismo archivo varias veces (Ingresos lee 4 hojas)."""
    contenido.seek(0)
    return io.BytesIO(contenido.read())


# ── Los archivos de cruce ────────────────────────────────────────────────────
#
# Los tres primeros siguen el mismo patrón: el lector lanza `KeyError` cuando la
# hoja que identifica al archivo no existe (o sea, no es este archivo) y
# `ValueError` cuando la hoja está pero le faltan columnas (o sea, es este
# archivo y hay que corregirlo). Esa correspondencia es la que separa los dos
# rojos, y por eso los chequeos no la reimplementan: la traducen.


def _payu_uc(contenido: io.BytesIO) -> int:
    try:
        return len(read_inscrip(_excel(contenido)))
    except ValueError as e:
        raise FormatoInvalido(str(e)) from e


def _cartera_prev(contenido: io.BytesIO) -> int:
    try:
        return len(read_cartera_preventiva(_excel(contenido)))
    except ValueError as e:
        raise FormatoInvalido(str(e)) from e


def _wompi_reporte(contenido: io.BytesIO) -> int:
    try:
        return len(read_pagos_wompi_reporte(_excel(contenido)))
    except ValueError as e:
        raise FormatoInvalido(str(e)) from e


def _ingresos(contenido: io.BytesIO) -> int:
    """`Ingresos PSE y PAYU` son CUATRO hojas en un archivo, así que acá la
    regla es distinta: si ninguna se puede leer, no es este archivo; si algunas
    sí y otras no, es este archivo con algo cambiado adentro.

    Es exactamente el caso del 2026-09-18 — tres hojas perfectas y la de 2833
    renombrada—, que hasta hoy no se veía por ningún lado."""
    leidas, fallaron = 0, []
    for nombre, lector in (('BANCOLOMBIA 2576', read_bancolombia_2576),
                           ('WOMPI', read_wompi),
                           ('STRIPE_USA', read_stripe_usa),
                           ('BANCOL 2833', read_bancolombia_2833)):
        try:
            leidas += len(lector(_excel(contenido)))
        except Exception as e:  # noqa: BLE001 — cualquier fallo de una hoja cuenta igual
            fallaron.append(f'{nombre} ({e})')

    if len(fallaron) == 4:
        raise ValueError('ninguna de las 4 hojas se pudo leer')
    if fallaron:
        raise FormatoInvalido('no se pudo leer: ' + '; '.join(fallaron))
    return leidas


# ── Los archivos de bancos y pasarelas ───────────────────────────────────────
#
# Acá NO se distinguen los dos rojos, y es a propósito: un PDF o un CSV no tiene
# "hojas" que permitan decir "es el archivo pero le falta algo". O el parser lo
# reconoce o no.
#
# ⚠️ Un archivo que se lee y trae CERO pagos es VÁLIDO, no un error. Los
# extractos de 2833 traen días enteros de puro movimiento interno del banco
# (liquidaciones del datáfono, el 4x1000, intereses) sin un solo pago de
# estudiante. Tratar eso como archivo malo fue lo que dejó dos extractos
# atascados 11 días disparando la cadena cada 15 minutos.


def _banco(mod, pdf: bool = False):
    def chequeo(contenido: io.BytesIO) -> int:
        buf = _excel(contenido)
        filas = mod.parse_pdf(buf) if pdf else mod.parse_file(buf, '')
        return len(filas)
    return chequeo


# Las dos cuentas de Bancolombia son el único par que el parser NO puede
# distinguir solo: los dos extractos son el mismo PDF del mismo banco, y los dos
# se bajan la misma mañana con nombres casi iguales (`ZIP_16869342576_…` y
# `ZIP_19100002833_…`). Cruzarlos no es cosmético — cambia el `payment_method`
# con el que entra cada pago, y con él la hoja donde se busca su CORREO(2).
#
# Se distinguen por el NÚMERO DE CUENTA, que el propio extracto trae impreso en
# su encabezado ("Número de Cuenta:16869342576"). O sea: sigue siendo la
# estructura del archivo la que decide, no su nombre.
_CUENTAS = {'bc2576': '16869342576', 'bc2833': '19100002833'}


def _bancolombia(fuente: str, mod):
    otra = next(c for f, c in _CUENTAS.items() if f != fuente)

    def chequeo(contenido: io.BytesIO) -> int:
        buf = _excel(contenido)
        try:
            with pdfplumber.open(buf) as pdf:
                encabezado = pdf.pages[0].extract_text() or ''
        except Exception:  # noqa: BLE001 — que no se pueda abrir ya lo dice el parser
            encabezado = ''

        # Solo se rechaza con evidencia POSITIVA de que es el otro extracto. Si
        # el encabezado cambia de forma y no aparece ninguna cuenta, se deja
        # pasar al parser: un rojo de más sobre un archivo bueno le enseña al
        # área a desconfiar del semáforo.
        if otra in encabezado:
            raise ValueError(f'es el extracto de la cuenta {otra}')

        return len(mod.parse_pdf(_excel(contenido)))
    return chequeo


def _payu(contenido: io.BytesIO) -> int:
    """PayU llega en DOS archivos que se emparejan, así que no se puede correr
    el parser con uno solo: se revisa que cada uno sea lo que dice ser."""
    headers, filas = mod_payu._read_tsv(_excel(contenido))
    if not headers:
        raise ValueError('no se encontró el encabezado del archivo de PayU')
    faltan = [c for c in ('FECHA', 'DOCUMENTO', 'DESCRIPCION', 'CREDITOS') if c not in headers]
    if faltan:
        raise FormatoInvalido('faltan las columnas ' + ', '.join(faltan))
    return len(filas)


def _payu_moneda(contenido: io.BytesIO) -> int:
    headers, filas = mod_payu._read_moneda_csv(_excel(contenido))
    if not headers:
        raise ValueError('no se encontró el encabezado del archivo de moneda')
    return len(filas)


_CHEQUEOS = {
    'payu_uc':       _payu_uc,
    'ingresos':      _ingresos,
    'cartera_prev':  _cartera_prev,
    'wompi_reporte': _wompi_reporte,
    'bc2576':        _bancolombia('bc2576', mod_bc2576),
    'bc2833':        _bancolombia('bc2833', mod_bc2833),
    'wompi':         _banco(mod_wompi),
    'stripe':        _banco(mod_stripe),
    'placetopay':    _banco(mod_placetopay),
    'colpatria':     _banco(mod_colpatria),
    'davivienda':    _banco(mod_davivienda),
    'payu':          _payu,
    'payu_moneda':   _payu_moneda,
}


def revisar(fuente: str, contenido: io.BytesIO) -> dict:
    """El veredicto para ese archivo en esa caja. **Nunca lanza.**

    Que la revisión se caiga no puede dejar al área sin poder subir: ante
    cualquier sorpresa el archivo queda como no reconocido, que es un rojo —
    nunca un verde de más."""
    chequeo = _CHEQUEOS.get(fuente)
    if chequeo is None:
        return {'estado': ARCHIVO_INCORRECTO, 'espera': ESPERA.get(fuente, fuente.upper()),
                'detalle': f'fuente desconocida: {fuente!r}'}

    try:
        filas = chequeo(contenido)
    except FormatoInvalido as e:
        return {'estado': FORMATO_INCORRECTO, 'detalle': str(e)}
    except Exception as e:  # noqa: BLE001 — todo lo demás es "no es este archivo"
        return {'estado': ARCHIVO_INCORRECTO, 'espera': ESPERA.get(fuente, fuente.upper()),
                'detalle': f'{type(e).__name__}: {e}'}

    return {'estado': OK, 'filas': filas}
