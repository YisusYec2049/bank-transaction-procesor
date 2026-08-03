#!/opt/matching-test/venv/bin/python3
"""
cruzar_cartera_preventiva.py — cruza cartera_preventiva (cuotas pendientes)
contra los pagos ya resueltos de cruce_cartera.

Es el ÚLTIMO paso de la cadena (confirmado por el usuario el 14 de julio):
1. consolidated_transactions (pagos crudos de los bancos)
2. cruce_cartera: INCP + CORREO(2)                         ← cruzar.py
3. cruce_cartera: NOMBRE + MÉTODO DE PAGO + CI (WOMPI)      ← cruzar.py
4. cartera_preventiva, usando SOLO los pagos que en el paso ← este script
   anterior quedaron con estado_cruce='cruzado' (superaron
   todas las validaciones de identidad de arriba)

Fase 4 del rediseño (16 de julio) — arquitectura INCREMENTAL, el cambio de
fondo respecto a las versiones anteriores de este script:

  - `cartera_preventiva` deja de recalcularse desde cero en cada corrida.
    Solo se procesan pagos NUEVOS (que todavía no tienen ninguna fila en
    `pago_asociaciones`) — el resto de la tabla se deja tal cual.
  - La asociación pago→cuota se guarda y se queda (`pago_asociaciones`, N
    filas por pago si se reparte entre varias cuotas).
  - Cada pago se aplica SOLO a las cuotas cuya `inscrip` == el INCP resuelto
    de ese pago en `cruce_cartera` (4.1) — antes se agrupaba por documento y
    se repartía sobre CUALQUIER inscripción de la persona.
  - Si el documento tiene 2+ inscripciones con cuotas pendientes, el sistema
    NO aplica nada automáticamente (4.3) — ni siquiera si el INCP resolvió
    limpio y sin ambigüedad. Queda para asociación manual (panel en
    `financial-platform`).
  - `cartera_preventiva_overrides.cerrado_manual` se respeta: una cuota
    cerrada a mano nunca entra a la cascada ni se toca acá.
  - `correo_elec` (4.6): si el pago es WOMPI automático (encontrado en
    ReportePagosWompi, `cruce_cartera.metodo_de_pago` distinto de
    "PAGOS MANUALES") se muestra "WOMPI (Automático Genera Link)" en vez
    del correo.
  - `fecha_cruce` (4.7): se llena con la fecha de esta corrida en toda cuota
    que la corrida toque.

Asociaciones huérfanas: si un pago que ya estaba asociado deja de estar
`cruzado` en `cruce_cartera` (ej. Fase 2 lo detectó como cesantías/pago por
llave después y lo apartó, borrándolo de `cruce_cartera`), su asociación en
`pago_asociaciones` queda huérfana. Este script la detecta, la borra (junto
con cualquier otra asociación de la misma cuota, para no dejar un estado
mixto) y resetea la cuota a "sin pago identificado" para que se reprocese
con lo que quede vigente.

CRUCE (columna cruce_cartera.cruce) — "cruce a la inversa": para cada pago
`cruzado`, confirma si terminó aplicado a una cuota y, si sí, trae el cliente
de esa cuota. Cuentan dos fuentes: una asociación real en `pago_asociaciones`
(vigente, no huérfana) o el propio Excel de cartera, que desde el 21 de julio
trae en `codigo_transaccion_1` el `matching_key` de los pagos que el proceso
manual ya cobró. Se recalcula sobre TODOS los pagos cruzados en cada corrida.
Puramente informativo — no toca estado_cruce/excepcion_motivo.

────────────────────────────────────────────────────────────────────────────
Montos: modelo "Saldo a Favor Manual + FALTA DE PAGO" (21 de julio, Spec A de
"Automatización de Cartera") — REEMPLAZA por completo el modelo A/B de
Sobrantes/Excedentes/Condonación (`363bac2`, 17 de julio). El modelo A/B, el
botón "es la última cuota", `_mismatch_a_b`/`_mismatch_b_a`/
`_reconciliar_inscripcion`, y la auto-aplicación FIFO de créditos
(`cartera_saldos_favor` como "pagos sintéticos") quedan ELIMINADOS. La razón:
un pago solo puede sobrar o faltar; lo que sobra ya NO se mueve solo — el
pipeline deja de decidir por el usuario qué hacer con el saldo a favor.

Reglas nuevas:

- **Falta `< $50.000`** (UMBRAL_LINEA_NUEVA): la cuota cierra con lo recibido
  y queda con `diferencia` NEGATIVA informativa, sin línea nueva,
  `notificacion=NULL`.
- **Falta `>= $50.000`**: la cuota ORIGINAL —la que no se pagó completa— queda
  marcada `notificacion='FALTA DE PAGO'` con `diferencia` NEGATIVA (el monto
  que quedó a deber), y se crea una CUOTA NUEVA por ese faltante (mismo patrón
  de llave, `_generar_llave_saldo`) SIN notificación y con `diferencia` vacía
  (todavía no tiene pago). La marca y la deuda viven en la original, no en la
  nueva.
- **Sobra `>= $1`** (sin importar el monto, sin distinción SOBRANTE/
  EXCEDENTE): la ÚLTIMA cuota cubierta cierra con `diferencia` POSITIVA
  informativa (el saldo a favor "vive" ahí) y se crea/actualiza un registro
  en el ledger `cartera_saldos_favor` (`origen='sobrante'`, `disponible`).
  **Nunca se auto-aplica a ninguna otra cuota.**
- **Asociar / Descartar** ese saldo a favor es 100% manual desde
  `financial-platform` — escribe directo a `pago_asociaciones` y
  `cartera_saldos_favor`. Este script solo LEE esas escrituras y las HONRA:
    - Pase de reconciliación manual (antes del FIFO automático): para toda
      cuota con al menos una asociación `origen='manual'`, recalcula su
      cierre/faltante a partir de la SUMA de todas sus asociaciones vigentes
      (mismo umbral de $50.000 que el FIFO automático).
    - Sincroniza la `diferencia` positiva de la cuota ORIGEN de cada saldo a
      favor tipo `sobrante` con el `disponible` restante del ledger (baja a
      medida que fin-platform lo va asociando; el pipeline nunca decrementa
      `disponible`, solo lo refleja).
    - Un pago con una fila `origen='descarte'` en el ledger queda EXCLUIDO
      para siempre de la auto-aplicación — así el cron no lo vuelve a cruzar
      con la cuota de la que se descartó. El descarte congela el PAR
      (pago↔cuota), no la cuota: un pago nuevo del mismo cliente sí puede
      cruzarla por FIFO normal.
    - Una cuota que pierde TODAS sus asociaciones (por un descarte) se
      resetea a pendiente si no queda ninguna asociación vigente que la
      cubra.

────────────────────────────────────────────────────────────────────────────
Requerimientos del 23 de julio (`requeriments.md`, puntos #1, #2 y #3):

- **Cerrar una cuota escribe `pago`** (#3). "Cerrada" para el usuario es que
  el mismo número viva en `Valor Pago` y en `Pago`, con
  `valor_a_cobrar = valor_cuota - pago` — así representa el Sistema
  Financiero una cuota cobrada (verificado: la fórmula se cumple en 1.000 de
  1.000 filas del Excel). Cuando la persona pagó justo, `valor_a_cobrar` cae
  a cero solo; cuando pagó de menos, el faltante queda a la vista, que es lo
  que el equipo usa para repartir la deuda en la cartera siguiente.
  `pago` ACUMULA sobre el abono que traía el Excel, y `pago_confirmado`
  guarda cuánto puso el cierre para poder deshacerlo sin borrar ese abono.
  Aplicar un pago NO cierra: cerrar es un acto de una persona.

- **Descartar recalcula y deshace el cierre** (#1). El pase de reconciliación
  pasó a cubrir TODA cuota cuya suma de asociaciones vigentes ya no coincida
  con lo que muestra — antes solo miraba las de asociación manual, y el reset
  solo las que perdían todas sus asociaciones, así que una cuota con dos
  pagos automáticos a la que se le descartaba uno no la tocaba nadie nunca.

- **`notificacion` es un aviso VIVO de plata sin repartir** (#2). Mientras el
  sobrante de un pago siga entero, la cuota que lo recibió dice cuánto pagó
  la persona medido en cuotas (`1 CUOTA + ABONO`, `PAGA DOS CUOTAS`, …). En
  cuanto se reparte una parte, el texto desaparece. Se recalcula entero en
  cada corrida; ver `_etiqueta_notificacion`.

`_fila_cierre` sigue devolviendo el MISMO set de claves siempre en cada POST
(bug crítico PGRST102 del 16/07 — leer su docstring). El orden de escritura
(`pago_asociaciones` primero) también se conserva por la misma razón.
────────────────────────────────────────────────────────────────────────────
"""

import argparse
import logging
import os
import sys
from datetime import datetime

import pytz
from dotenv import load_dotenv

from utils import dry_run
from utils.parser import normalizar_nit, normalizar_sufijo
from utils.supabase import (
    delete_by_keys,
    insert_cartera_preventiva_lineas,
    select_all,
    update_cruce_valores,
    upsert_cartera_preventiva,
    upsert_cartera_saldos_favor,
    upsert_pago_asociaciones,
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%S',
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

WOMPI_LINK_LABEL = 'WOMPI (Automático Genera Link)'
PAGOS_MANUALES_LABEL = 'PAGOS MANUALES'
_FECHA_MAX = '9999-12-31'

# Umbral que separa "ruido de redondeo" de "deuda/sobrante real", validado
# contra datos reales de producción el 17 de julio (el mayor faltante por
# debajo es $45.000, el menor por encima es $192.349 — no hay nada en el
# medio). Único umbral desde el 21 de julio (antes A/B usaba `>=`/`<=`
# distinto según el modo; ahora siempre `>=`).
UMBRAL_LINEA_NUEVA = 50000

# Umbral de la columna `notificacion` (23 de julio, punto #2). Distinto y muy
# por debajo del de FALTA DE PAGO a propósito: aquí no se decide si se abre
# una cuota nueva, solo si vale la pena AVISAR que hay plata sin repartir.
# Con $50.000 se habrían perdido 9 de los 11 casos reales con plata de verdad.
# Ver `_etiqueta_notificacion` para los dos usos que tiene este número.
UMBRAL_NOTIFICACION = 1000

_NUMERO_EN_LETRA = {2: 'DOS', 3: 'TRES', 4: 'CUATRO', 5: 'CINCO', 6: 'SEIS',
                     7: 'SIETE', 8: 'OCHO', 9: 'NUEVE', 10: 'DIEZ'}

# Etiquetas que escribe `_etiqueta_notificacion`. La pasada de notificación
# solo puede LIMPIAR filas cuyo texto pertenezca a esta familia — nunca pisa
# 'FALTA DE PAGO' (que vive en la cuota nueva del faltante) ni 'CARTERA'
# (cierre manual), que son de otros dueños.
def _es_etiqueta_de_sobrante(valor) -> bool:
    v = str(valor or '').strip().upper()
    return bool(v) and (v.startswith('PAGA ') or v.endswith('+ ABONO'))


def _dia_bogota(marca, tz) -> str:
    """Día (YYYY-MM-DD, hora Colombia) de una marca de tiempo de Supabase.

    Supabase las guarda en UTC, así que cortar la cadena a 10 caracteres
    parte el día: una carga hecha a las 21:12 de Colombia queda registrada
    como `2026-07-22T02:12+00:00` — el día siguiente. Pasó de verdad (las
    dos últimas cargas del 21 de julio), y con el corte crudo esas cargas y
    los repartos de esa misma tarde caerían en días distintos.
    """
    if not marca:
        return ''
    try:
        return datetime.fromisoformat(str(marca).replace('Z', '+00:00')).astimezone(tz).strftime('%Y-%m-%d')
    except ValueError:
        return str(marca)[:10]


def _normalizar_documento(valor) -> str:
    return normalizar_nit(str(valor or '').strip())


def _normalizar_correo(valor) -> str | None:
    v = str(valor or '').strip().lower()
    return v or None


def _base_inscripcion(valor) -> str:
    v = str(valor or '').strip()
    return normalizar_sufijo(v) or v


def _saldo_a_cobrar(cuota: dict) -> float:
    """Monto que la cuota debe HOY de verdad.

    El Excel de cartera trae la columna `PAGO` — abonos que el proceso
    manual ya registró en el Sistema Financiero — y `valor_a_cobrar` =
    `valor_cuota` - `pago`. Cobrar contra `valor_cuota` ignora ese abono:
    se cobra de más y el saldo que queda sale inflado exactamente por lo
    ya pagado (caso real 21 de julio, llave 3680PN46220: cuota 873.636 con
    500.000 abonados; un pago de 300.000 dejó saldo de 573.636 en vez de
    73.636).

    `_fila_cierre_cartera` (cierre manual) ya usaba `valor_a_cobrar` por
    esta misma razón; acá se unifica el criterio para el FIFO automático.
    Cae a `valor_cuota` solo si `valor_a_cobrar` viene NULL.

    CIERRE CONFIRMADO (23 de julio, punto #3): cerrar una cuota escribe en
    `pago` lo que está en `valor_pago` y baja `valor_a_cobrar` por la fórmula
    del Sistema Financiero (`valor_a_cobrar = valor_cuota - pago`). Es decir
    que en una cuota ya cerrada `valor_a_cobrar` NO es el saldo que la cuota
    tenía cuando se le aplicó el pago — es lo que quedó después del cierre.
    Como el cierre baja `valor_a_cobrar` exactamente en `pago_confirmado`,
    el saldo original se recupera sumándolo de vuelta. Sin esto, recalcular
    una cuota cerrada (ej. tras un descarte) mediría el pago contra un saldo
    en cero y daría una `diferencia` igual al pago entero."""
    valor = cuota.get('valor_a_cobrar')
    if valor is None:
        valor = cuota.get('valor_cuota')
    return float(valor or 0) + float(cuota.get('pago_confirmado') or 0)


def _es_wompi_automatico(pago: dict) -> bool:
    payment_method = str(pago.get('payment_method') or '').upper()
    metodo = str(pago.get('metodo_de_pago') or '')
    return payment_method.startswith('WOMPI') and bool(metodo) and metodo != PAGOS_MANUALES_LABEL


def _correo_elec_para(pago: dict) -> str:
    if _es_wompi_automatico(pago):
        return WOMPI_LINK_LABEL
    return pago.get('email') or ''


def _generar_llave_saldo(llave_base: str, pago: dict,
                          llaves_cobradas: frozenset = frozenset()) -> str:
    """Llave DETERMINÍSTICA para la cuota nueva de "FALTA DE PAGO" (o del
    saldo pendiente de un pago parcial), derivada del pago que la origina —
    no de "buscar el primer sufijo libre" (bug crítico corregido el 16 de
    julio: esa búsqueda nunca era estable entre corridas, ver git history).
    Reprocesar el MISMO pago siempre calcula la MISMA llave, y el upsert por
    `llave` en insert_cartera_preventiva_lineas actualiza esa fila en vez de
    crear una nueva.

    Lleva la FECHA del pago entre paréntesis (formato pedido por el usuario
    el 21 de julio, igual al que usa el Excel de cartera para sus propias
    líneas partidas). Cae al `matching_key` si el pago no trae fecha, que es
    el único caso donde la fecha no serviría para identificar nada.

    `llaves_cobradas` son las llaves que YA corresponden a una cuota cobrada
    (las líneas partidas que el Excel trae con su pago anotado, ver
    `_desambiguar_llaves` en activar_cartera.py). Usan el mismo formato
    `llave (fecha)`, así que un pago nuevo fechado el mismo día que uno
    viejo de la misma cuota generaría una llave idéntica — y como la
    escritura es un upsert por llave, PISARÍA esa línea cobrada y se
    perdería el registro del abono. Cuando eso pasa se agrega el
    `matching_key` detrás. La comparación es solo contra cuotas COBRADAS a
    propósito: una línea de saldo previa nuestra (sin pago todavía) tiene
    que seguir dando la MISMA llave, o el reproceso del mismo pago crearía
    una fila nueva en vez de actualizar la suya."""
    marca = pago.get('payment_date') or pago.get('matching_key')
    candidata = f'{llave_base} ({marca})'
    if candidata in llaves_cobradas:
        return f'{llave_base} ({marca} {pago.get("matching_key")})'
    return candidata


def _pago_sin_confirmar(cuota: dict) -> float:
    """El `pago` que la cuota tenía ANTES de cerrarse, o sea el abono que
    trajo el Excel del proceso manual. `pago` mezcla dos cosas desde el 23 de
    julio (punto #3): lo que registró el proceso manual y lo que escribió el
    cierre. `pago_confirmado` guarda cuánto puso el cierre, y restarlo
    devuelve el valor original — sin esto, deshacer un cierre borraría
    también el abono del Excel."""
    return round(float(cuota.get('pago') or 0) - float(cuota.get('pago_confirmado') or 0), 2)


def _fila_reset(cuota: dict) -> dict:
    """Dict de reset a NULL de las columnas de resultado del cruce para una
    cuota — usado tanto por el reset de asociaciones huérfanas como por el
    reset de cuotas que perdieron todas sus asociaciones por un descarte, o
    que se reabren al apagar `cerrado_manual`.

    DESHACE EL CIERRE (23 de julio, punto #1): si la cuota estaba cerrada, se
    devuelve `pago` a lo que traía el Excel y `valor_a_cobrar` al saldo que
    le corresponde. Confirmado por el usuario: una cuota a la que se le
    descarta el pago no puede quedar marcada como pagada sin tener con qué.

    ADVERTENCIA (bug PGRST102, ver `_fila_cierre`): este dict tiene un set de
    claves DISTINTO al de `_fila_cierre`. Nunca mezclar filas de `_fila_reset`
    en el mismo array que filas de `_fila_cierre` en una misma llamada a
    `upsert_cartera_preventiva` — escribirlas en una lista/POST aparte."""
    pago_excel = _pago_sin_confirmar(cuota)
    return {'id': cuota['id'], 'fecha_pago': None, 'medio_pago': None, 'valor_pago': None,
            'codigo_transaccion_1': None, 'codigo_transaccion_2': None, 'correo_elec': None,
            'diferencia': None, 'fecha_cruce': None, 'notificacion': None,
            'es_wompi_automatico': None,
            'pago': pago_excel or None,
            'pago_confirmado': None,
            'valor_a_cobrar': round(float(cuota.get('valor_cuota') or 0) - pago_excel, 2)}


def _fila_cierre_cartera(cuota: dict, fecha_pago_manual: str, hoy: str) -> dict:
    """Cierre MANUAL de una cuota puntual ("Cerrar Cuota", antes "Cerrar
    cartera") — la persona la marca desde financial-platform y elige la
    fecha. No viene de un pago real: `valor_pago` = el saldo pendiente de la
    cuota (confirmado por el usuario — no `valor_cuota`), `medio_pago`
    literal `'Cartera'`, `notificacion='CARTERA'`.

    Cerrar también escribe `pago` (23 de julio, punto #3): el usuario ve una
    cuota cerrada cuando el mismo número vive en `Valor Pago` y en `Pago`.
    Es como el Sistema Financiero representa una cuota cobrada — verificado
    contra 1.000 filas del Excel, todas cumplen `valor_a_cobrar =
    valor_cuota - pago` y 987 tienen `pago == valor_pago`.

    `pago` ACUMULA en vez de reemplazar: si la cuota ya traía un abono del
    proceso manual, pisarlo lo borraría y la cuota volvería a mostrar deuda
    ya pagada. `pago_confirmado` deja constancia de cuánto puso este cierre
    para poder deshacerlo (ver `_fila_reset`).

    Mismo set de claves que `_fila_cierre` (15), para poder mezclarse en el
    mismo array/POST sin disparar PGRST102."""
    valor_cuota = float(cuota.get('valor_cuota') or 0)
    saldo       = _saldo_a_cobrar(cuota)
    pago_nuevo  = round(_pago_sin_confirmar(cuota) + saldo, 2)
    return {
        'id':                   cuota['id'],
        'fecha_pago':           fecha_pago_manual,
        'medio_pago':           'Cartera',
        'valor_pago':           saldo,
        'codigo_transaccion_1': None,
        'codigo_transaccion_2': None,
        'correo_elec':          None,
        'fecha_cruce':          hoy,
        'notificacion':         'CARTERA',
        'diferencia':           0,
        'valor_cuota':          valor_cuota,
        'valor_a_cobrar':       round(valor_cuota - pago_nuevo, 2),
        'es_wompi_automatico':  None,
        'pago':                 pago_nuevo,
        'pago_confirmado':      saldo,
    }


def _aplicar_pagos_inscripcion(cuotas_abiertas: list[dict], pagos_nuevos: list[dict]):
    """FIFO de pagos_nuevos (ordenados por payment_date) contra
    cuotas_abiertas (ordenadas por fecha_vencimiento) de UNA sola
    inscripción. Montos EXACTOS: una cuota solo se considera cerrada cuando
    el acumulado la cubre por completo, ni un peso menos.

    Cada cuota tocada se resuelve por completo en esta misma pasada: si el
    dinero disponible no la cubre del todo, se cierra por lo recibido y el
    resto queda para que quien llame decida (diferencia negativa o línea
    nueva "FALTA DE PAGO", según el umbral).

    Devuelve (cierres, parcial, asociaciones, excedente):
      - cierres: [{'cuota', 'monto_aplicado', 'ultimo_pago'}] cuotas
        cubiertas EXACTAMENTE.
      - parcial: misma forma, o None — la ÚLTIMA cuota tocada que se quedó
        sin dinero para completarla. A lo sumo una.
      - asociaciones: [{'matching_key', 'cuota_id', 'monto'}].
      - excedentes: [{'pago', 'monto'}] — plata que sobró tras cubrir TODAS
        las cuotas conocidas de la inscripción (saldo a favor, ver §2 de la
        spec), SEPARADA POR PAGO. Se devuelve así, y no como un total, porque
        el saldo a favor tiene que quedar a nombre del pago del que sobró de
        verdad: con dos pagos en la misma corrida y uno sobrando entero, el
        total se registraba bajo el pago que cerró la última cuota (caso real
        4166PN, 22 de julio — dos pagos de $524.688, uno cubrió la cuota y el
        otro quedó libre, pero el ledger los atribuía los dos al primero)."""
    cuotas_ordenadas = sorted(cuotas_abiertas, key=lambda c: c.get('fecha_vencimiento') or _FECHA_MAX)
    pagos_ordenados = sorted(pagos_nuevos, key=lambda p: p.get('payment_date') or _FECHA_MAX)

    acumulado: dict = {}
    ultimo_pago_por_cuota: dict = {}
    asociaciones = []
    idx = 0
    excedentes: list[dict] = []

    for pago in pagos_ordenados:
        restante = float(pago.get('payment_amount') or 0)
        if restante <= 0:
            continue
        matching_key = pago['matching_key']
        while restante > 0 and idx < len(cuotas_ordenadas):
            cuota = cuotas_ordenadas[idx]
            cuota_id = cuota['id']
            saldo_cuota = _saldo_a_cobrar(cuota)
            ya = acumulado.get(cuota_id, 0.0)
            saldo = saldo_cuota - ya
            if saldo <= 0:
                idx += 1
                continue
            aplicar = min(restante, saldo)
            acumulado[cuota_id] = ya + aplicar
            ultimo_pago_por_cuota[cuota_id] = pago
            asociaciones.append({'matching_key': matching_key, 'cuota_id': cuota_id, 'monto': round(aplicar, 2)})
            restante -= aplicar
            if saldo_cuota - acumulado[cuota_id] <= 0:
                idx += 1
        if restante > 0:
            excedentes.append({'pago': pago, 'monto': round(restante, 2)})

    cierres, parcial = [], None
    for cuota in cuotas_ordenadas:
        cuota_id = cuota['id']
        if cuota_id not in acumulado:
            continue
        monto = acumulado[cuota_id]
        info = {'cuota': cuota, 'monto_aplicado': monto, 'ultimo_pago': ultimo_pago_por_cuota[cuota_id]}
        if _saldo_a_cobrar(cuota) - monto <= 0:
            cierres.append(info)
        else:
            parcial = info

    return cierres, parcial, asociaciones, excedentes


def _fila_cierre(info: dict, hoy: str, cerrar_al_monto_recibido: bool = False) -> dict:
    """Bug crítico corregido (16 de julio): TODAS las filas que terminan en
    `actualizaciones_cierre` se postean juntas en un solo POST
    (`upsert_cartera_preventiva`, un array JSON) — y PostgREST exige que
    cada objeto de ese array tenga exactamente el mismo set de claves, o
    responde 400 (`PGRST102: All object keys must match`). Esta función
    siempre devuelve el mismo set de claves; para las que no cambian de
    verdad se manda el valor QUE YA TIENE la cuota, nunca None."""
    cuota = info['cuota']
    ultimo_pago = info['ultimo_pago']
    monto = round(info['monto_aplicado'], 2)
    # Aplicar un pago NO confirma la cuota: confirmar (escribir `pago`) es un
    # acto de una persona, con "Cerrar Cuota" o "Cerrar Cartera" (23 de julio,
    # punto #3). Y si la cuota venía confirmada y se está recalculando —el
    # único motivo es que sus asociaciones cambiaron, o sea un descarte— el
    # cierre se DESHACE: `pago` vuelve al abono del Excel y `valor_a_cobrar`
    # al saldo que le corresponde. Confirmado por el usuario en el punto #1.
    pago_excel = _pago_sin_confirmar(cuota)
    valor_cuota = float(cuota.get('valor_cuota') or 0)
    fila = {
        'id':                   cuota['id'],
        'fecha_pago':           ultimo_pago.get('payment_date'),
        'medio_pago':           ultimo_pago.get('payment_method'),
        'valor_pago':           monto,
        'codigo_transaccion_1': ultimo_pago.get('transaction_code_1'),
        'codigo_transaccion_2': ultimo_pago.get('transaction_code_2'),
        'correo_elec':          _correo_elec_para(ultimo_pago),
        'fecha_cruce':          hoy,
        'notificacion':         None,
        'es_wompi_automatico':  _es_wompi_automatico(ultimo_pago),
        # `valor_cuota`/`valor_a_cobrar` NO se pisan con lo recibido (21 de
        # julio): son el espejo del Excel, y sobrescribirlos hacía ilegible la
        # fila — el caso real mostraba "Valor Cuota 300.000 / Pago 500.000"
        # cuando la cuota de verdad era de 873.636. Lo recibido se ve en
        # `valor_pago`. Se recalcula por la fórmula del Sistema Financiero
        # para que un cierre deshecho quede consistente; sin confirmación de
        # por medio da exactamente el mismo valor que ya tenía.
        'valor_cuota':          valor_cuota,
        'valor_a_cobrar':       round(valor_cuota - pago_excel, 2),
        'pago':                 pago_excel or None,
        'pago_confirmado':      None,
    }
    if cerrar_al_monto_recibido:
        # La cuota ORIGINAL de un faltante >= $50.000 queda cerrada con
        # diferencia=0 — el faltante real pasa a la cuota nueva "FALTA DE
        # PAGO".
        fila['diferencia'] = 0
    else:
        fila['diferencia'] = round(monto - _saldo_a_cobrar(cuota), 2)
    return fila


def _fila_linea_saldo(parcial: dict, nueva_llave: str, notificacion: str | None = None) -> dict:
    cuota = parcial['cuota']
    saldo = round(_saldo_a_cobrar(cuota) - parcial['monto_aplicado'], 2)
    return {
        # Llave de la cuota que originó esta línea. Solo circula en memoria
        # (`_sincronizar_lineas_falta_de_pago` la usa para saber si la
        # original ya tiene línea); se quita antes de escribir a Supabase.
        '_base':               cuota.get('llave'),
        'llave':               nueva_llave,
        'sistema_financiero':  cuota.get('sistema_financiero'),
        'inscrip':             cuota.get('inscrip'),
        'cliente':             cuota.get('cliente'),
        'moneda':              cuota.get('moneda'),
        'fecha_vencimiento':   cuota.get('fecha_vencimiento'),
        'programa':            cuota.get('programa'),
        'cruce_access':        cuota.get('cruce_access'),
        'correo':              cuota.get('correo'),
        'valor_cuota':         saldo,
        'valor_a_cobrar':      saldo,
        'pago':                None,
        'fecha_pago':          None,
        'medio_pago':          None,
        'valor_pago':          None,
        'codigo_transaccion_1': None,
        'codigo_transaccion_2': None,
        'correo_elec':         None,
        'diferencia':          None,
        'notificacion':        notificacion,
        'es_wompi_automatico': None,
        'pago_confirmado':     None,
    }


def _sincronizar_lineas_falta_de_pago(
        cuotas_rows: list[dict],
        actualizaciones_cierre: list[dict],
        lineas_nuevas: list[dict],
        llaves_cerradas_manual: set,
        asociaciones_por_llave: dict) -> tuple[list[dict], list[str]]:
    """La línea de FALTA DE PAGO no es una cuota independiente: es el REFLEJO
    de lo que quedó debiendo su cuota original (regla del usuario, 30 de
    julio). Mientras la original siga ABIERTA:

      * si su `diferencia` baja porque le asociaron plata, la línea baja con
        ella;
      * si lo que falta cae por debajo de `UMBRAL_LINEA_NUEVA`, la línea
        DESAPARECE y el faltante queda solo como `diferencia` en la original,
        igual que cuando nace pequeño;
      * la original tiene UNA sola línea, venga la plata del pago que venga.
        Sin esto, asociar plata de un pago con otra fecha creaba una segunda
        línea (la llave se deriva del pago) y la primera se quedaba con el
        número viejo, cobrando dos veces la misma deuda.

    Cuando la original se CIERRA —"Cerrar Cuota" o el cierre de cartera del
    día, que escriben `pago_confirmado`— la línea se INDEPENDIZA: deja de ser
    un reflejo y pasa a ser la deuda que se arrastra al mes siguiente. Desde
    ese momento esta función no la vuelve a tocar.

    Guardas al borrar (una línea es una fila de cartera como cualquier otra,
    y puede tener trabajo humano encima): no se borra si tiene pago aplicado,
    asociaciones o cierre manual — en ese caso se avisa en el log en vez de
    llevarse por delante algo que alguien hizo.

    Devuelve (updates, llaves_a_borrar). No escribe nada.
    """
    final_por_id = {f['id']: f for f in actualizaciones_cierre if f.get('id') is not None}
    por_llave = {c['llave']: c for c in cuotas_rows if c.get('llave')}

    def _campo(cuota: dict, nombre: str):
        """El valor que la cuota va a tener al terminar esta corrida."""
        fin = final_por_id.get(cuota['id'])
        if fin is not None and nombre in fin:
            return fin[nombre]
        return cuota.get(nombre)

    # Líneas VIVAS por cuota original. Una línea del Excel (cuota partida en
    # abonos por el proceso manual) usa el mismo formato `llave (fecha)` pero
    # llega con `fecha_pago` puesto — esas no son nuestras y no se tocan.
    lineas_por_original: dict[str, list[dict]] = {}
    for c in cuotas_rows:
        llave = c.get('llave') or ''
        corte = llave.rfind(' (')
        if corte <= 0:
            continue
        base = llave[:corte]
        if base in por_llave and _campo(c, 'fecha_pago') is None:
            lineas_por_original.setdefault(base, []).append(c)

    updates: list[dict] = []
    a_borrar: list[str] = []
    bases = set(lineas_por_original) | {
        ln['_base'] for ln in lineas_nuevas if ln.get('_base') in por_llave
    }
    for base in sorted(bases):
        original = por_llave.get(base)
        if original is None:
            continue
        if (_campo(original, 'pago_confirmado') is not None
                or base in llaves_cerradas_manual):
            continue  # cerrada: la línea ya es independiente

        diferencia = _campo(original, 'diferencia')
        falta = round(-float(diferencia), 2) if diferencia is not None and float(diferencia) < 0 else 0.0
        existentes = sorted(lineas_por_original.get(base, []), key=lambda c: c['id'])

        if falta >= UMBRAL_LINEA_NUEVA and existentes:
            # Ya hay línea: se ajusta esa y no se crea ninguna otra.
            viva = existentes[0]
            if round(float(viva.get('valor_a_cobrar') or 0), 2) != falta:
                updates.append({'id': viva['id'], 'valor_cuota': falta, 'valor_a_cobrar': falta})
                viva['valor_cuota'] = viva['valor_a_cobrar'] = falta
            sobrantes = existentes[1:]
        else:
            sobrantes = existentes

        for linea in sobrantes:
            llave = linea['llave']
            if asociaciones_por_llave.get(llave):
                log.warning('Línea de deuda %s no se borra: tiene pago(s) asociado(s).', llave)
                continue
            if llave in llaves_cerradas_manual or _campo(linea, 'pago_confirmado') is not None:
                log.warning('Línea de deuda %s no se borra: está cerrada a mano.', llave)
                continue
            a_borrar.append(llave)

    # Una línea a punto de crearse para una original que ya tiene la suya (o
    # que ya no llega al umbral) no debe llegar a escribirse.
    ya_cubiertas = {b for b in bases if lineas_por_original.get(b)}
    lineas_nuevas[:] = [
        ln for ln in lineas_nuevas
        if ln.get('_base') not in ya_cubiertas and ln['llave'] not in a_borrar
    ]
    return updates, a_borrar


def _cerrar_o_faltante(info: dict, hoy: str,
                        llaves_cobradas: frozenset = frozenset()) -> tuple[dict, dict | None]:
    """A partir de un cierre exacto o parcial (`info` con 'cuota',
    'monto_aplicado', 'ultimo_pago'), arma (fila_cierre, linea_nueva_o_None)
    aplicando el umbral único de FALTA DE PAGO (`>= UMBRAL_LINEA_NUEVA`).
    Usado tanto por el FIFO automático como por el pase de reconciliación
    manual (§3.5) — ambos deben comportarse igual ante un faltante."""
    cuota = info['cuota']
    monto = info['monto_aplicado']
    s = round(_saldo_a_cobrar(cuota) - monto, 2)
    if s <= 0:
        return _fila_cierre(info, hoy), None
    if s >= UMBRAL_LINEA_NUEVA:
        # El faltante (>= $50.000) genera una cuota NUEVA por la deuda, pero la
        # marca 'FALTA DE PAGO' y el monto que quedó a deber viven en la cuota
        # ORIGINAL —la que no se pagó completa—, que es a la que le faltó el
        # pago. `diferencia` negativa = deuda (misma convención que el faltante
        # < $50.000). La cuota nueva queda como una cuota pendiente normal: sin
        # notificación y con `diferencia` vacía (todavía no tiene ningún pago).
        fila = _fila_cierre(info, hoy, cerrar_al_monto_recibido=True)
        fila['notificacion'] = 'FALTA DE PAGO'
        fila['diferencia'] = -s
        nueva_llave = _generar_llave_saldo(cuota['llave'], info['ultimo_pago'], llaves_cobradas)
        linea_nueva = _fila_linea_saldo(info, nueva_llave, notificacion=None)
        return fila, linea_nueva
    return _fila_cierre(info, hoy, cerrar_al_monto_recibido=False), None


def _procesar_inscripcion(cuotas_inscripcion: list[dict], pagos_para: list[dict], hoy: str,
                           llaves_cobradas: frozenset = frozenset()):
    """Corre el FIFO de UNA inscripción y arma las filas de escritura según
    el modelo "Saldo a Favor Manual + FALTA DE PAGO" (21 de julio).

    No escribe nada en Supabase — solo calcula. Devuelve:
      (cierres_filas, linea_nueva_o_None, asociaciones, saldos_favor_nuevos)
    """
    cierres, parcial, asociaciones, excedentes = _aplicar_pagos_inscripcion(cuotas_inscripcion, pagos_para)

    cierres_filas = [_fila_cierre(info, hoy) for info in cierres]
    linea_nueva = None
    if parcial:
        fila, linea_nueva = _cerrar_o_faltante(parcial, hoy, llaves_cobradas)
        cierres_filas.append(fila)

    saldos_favor_nuevos: list[dict] = []
    if excedentes and cierres_filas:
        # El excedente lo absorbe la ÚLTIMA cuota que toca el pago (nunca la
        # parcial: excedente y parcial son mutuamente excluyentes dentro de
        # una misma llamada — si sobra plata es porque TODAS las cuotas ya
        # se cerraron). Queda como `diferencia` POSITIVA informativa — NUNCA
        # se suma a `valor_pago` ni se auto-aplica a otra cuota.
        #
        # La cuota es una sola, pero el LEDGER lleva una fila por pago: cada
        # peso queda a nombre del pago del que sobró de verdad. Antes se
        # escribía una sola fila con el total a nombre de `cierres[-1]`, y con
        # dos pagos en la misma corrida eso mentía sobre de quién era la plata
        # (caso 4166PN del 22 de julio) y dejaba al segundo pago sin rastro de
        # haberse usado, así que cada corrida lo volvía a mirar como nuevo.
        ultima_cuota = cierres[-1]['cuota']
        cierres_filas[-1]['diferencia'] = round(sum(e['monto'] for e in excedentes), 2)
        for e in excedentes:
            saldos_favor_nuevos.append({
                'inscrip':      ultima_cuota.get('inscrip'),
                'cliente':      ultima_cuota.get('cliente'),
                'documento':    _normalizar_documento(ultima_cuota.get('cruce_access')),
                'correo':       _normalizar_correo(ultima_cuota.get('correo')),
                'monto':        e['monto'],
                'disponible':   e['monto'],
                'origen':       'sobrante',
                'llave_origen': ultima_cuota['llave'],
                'matching_key': e['pago']['matching_key'],
                'fecha':        e['pago'].get('payment_date'),
                'aplicado':     False,
            })

    return cierres_filas, linea_nueva, asociaciones, saldos_favor_nuevos


def _etiqueta_notificacion(valor_cuota: float, sobrante: float,
                            cuotas_cubiertas: int = 1) -> str | None:
    """Texto de la columna `notificacion` para un pago que dejó plata sin
    repartir (23 de julio, punto #2). Describe CUÁNTO pagó la persona medido
    en cuotas, no cuántas cuotas alcanzó a cerrar el sistema: la cartera
    suele tener registrada una sola cuota pendiente, así que "pagó tres
    cuotas" se ve como una cuota cubierta + un sobrante grande esperando que
    alguien lo asigne. Ese aviso es justamente para quien revisa.

    Devuelve None cuando no hay nada que avisar (sobrante por debajo del
    umbral, o sea redondeo de las pasarelas).

    `UMBRAL_NOTIFICACION` hace dos trabajos:
      1. piso para avisar — medido sobre los 23 sobrantes reales del 23/07,
         con $1.000 se marcan 11 (de $1.364 a $2.081.818) y quedan mudos 12
         (de $1, $2 y $980);
      2. tolerancia al contar cuotas enteras — caso real GP & A SAS: cuota
         $325.317 con sobrante $325.296, o sea 21 pesos por debajo de una
         cuota entera. Contando estricto salía "1 CUOTA + ABONO" cuando
         cualquiera diría que pagó dos.

    `cuotas_cubiertas` son las cuotas que ESE pago tiene asociadas hoy. Hasta
    el 28 de julio se asumía 1 y el aviso quedaba corto cuando un pago cerraba
    dos o más: el caso 16079640 cerró la cuota de junio y la de julio y dejó
    $4.090.910 sueltos, y decía "PAGA CINCO CUOTAS" cuando la persona pagó
    seis. Sale la misma cuenta por los dos caminos — si después se le descarta
    el pago a una de las dos, esa cuota deja de contarse pero su plata vuelve
    al saldo disponible y reaparece como cuota entera."""
    if valor_cuota <= 0 or sobrante < UMBRAL_NOTIFICACION:
        return None
    enteras = int((sobrante + UMBRAL_NOTIFICACION) // valor_cuota)
    resto   = sobrante - enteras * valor_cuota
    total   = max(int(cuotas_cubiertas), 1) + enteras
    if resto >= UMBRAL_NOTIFICACION:
        return f'{total} CUOTA{"S" if total > 1 else ""} + ABONO'
    return f'PAGA {_NUMERO_EN_LETRA.get(total, str(total))} CUOTAS'


def verificar_cuadre(supabase_url: str, srk: str, pagos_por_matching_key: dict,
                     tolerancia: float = 1.0) -> list[dict]:
    """Comprueba, pago por pago, que la plata no se haya perdido ni duplicado.

    La regla es una sola y no depende de ninguna lógica de este archivo:

        lo aplicado a cuotas  +  lo que queda disponible  ==  lo que entró

    Este chequeo, hecho a mano, es el que destapó el bug del ledger el 28 de
    julio: de 278 asociaciones y 44 saldos, **una sola** no cerraba — un pago
    cuyo excedente se había registrado a nombre de otro pago de la misma
    corrida. Nadie lo habría visto mirando la pantalla, porque los totales
    cuadraban; solo no cuadraba el detalle. Que corriera solo desde el
    principio habría ahorrado ese hallazgo.

    Se relee de la base a propósito, en vez de calcularlo con lo que quedó en
    memoria: lo que interesa saber es qué quedó **escrito**, no qué creía el
    código que iba a escribir. Son dos consultas chicas (cientos de filas) al
    final de una corrida que dura minutos.

    Devuelve la lista de descuadres. No lanza excepción ni corta la corrida:
    para cuando esto corre, el trabajo ya está hecho, y tumbar el proceso no
    desharía nada — solo escondería el resto del log.

    Medido contra producción el 2026-08-02 antes de instalarlo: 154 pagos con
    movimiento, **0 descuadres**. La línea base está limpia, así que cualquier
    cosa que reporte de aquí en adelante es nueva.
    """
    from collections import defaultdict

    asociaciones = select_all(supabase_url, srk, 'pago_asociaciones',
                              select='matching_key,llave,monto')
    saldos = select_all(supabase_url, srk, 'cartera_saldos_favor',
                        select='matching_key,disponible')

    aplicado: dict = defaultdict(float)
    for a in asociaciones:
        aplicado[a['matching_key']] += float(a.get('monto') or 0)

    disponible: dict = defaultdict(float)
    for s in saldos:
        disponible[s['matching_key']] += float(s.get('disponible') or 0)

    descuadres = []
    for mk in set(aplicado) | set(disponible):
        pago = pagos_por_matching_key.get(mk)
        if pago is None:
            # Un movimiento cuyo pago ya no está en el cruce: el pago se apartó
            # o se corrigió después de repartirse, y su plata quedó colgando.
            descuadres.append({
                'matching_key': mk, 'motivo': 'sin pago en cruce_cartera',
                'entro': None, 'aplicado': aplicado[mk], 'disponible': disponible[mk],
            })
            continue

        entro = float(pago.get('payment_amount') or 0)
        total = aplicado[mk] + disponible[mk]
        if abs(total - entro) > tolerancia:
            descuadres.append({
                'matching_key': mk,
                'motivo': 'sobra' if total > entro else 'falta',
                'entro': entro, 'aplicado': aplicado[mk],
                'disponible': disponible[mk], 'diferencia': total - entro,
            })

    if not descuadres:
        log.info('Cuadre verificado: %d pago(s) con movimiento, todo cuadra.',
                 len(set(aplicado) | set(disponible)))
        return []

    log.error('*** CUADRE: %d pago(s) NO cuadran — revisar antes de confiar en '
              'estos números ***', len(descuadres))
    for d in descuadres[:20]:
        log.error('  %s (%s): entró=%s aplicado=%.0f disponible=%.0f',
                  d['matching_key'], d['motivo'], d['entro'],
                  d['aplicado'], d['disponible'])
    if len(descuadres) > 20:
        log.error('  ... y %d más.', len(descuadres) - 20)
    return descuadres


def _pago_mas_reciente(asociaciones: list[dict], pagos_por_matching_key: dict) -> dict:
    """De un grupo de asociaciones vigentes de una misma cuota, el pago con
    `payment_date` más reciente — usado para llenar fecha_pago/medio_pago/
    correo_elec/etc. de la cuota en el pase de reconciliación manual (§3.5),
    igual criterio que "el último pago que la tocó" del resto del script."""
    mejor = None
    for a in asociaciones:
        p = pagos_por_matching_key.get(a['matching_key'])
        if p is None:
            continue
        if mejor is None or (p.get('payment_date') or '') >= (mejor.get('payment_date') or ''):
            mejor = p
    return mejor or {}


def main():
    parser = argparse.ArgumentParser(description='Aplica los pagos identificados sobre las cuotas pendientes.')
    dry_run.agregar_flags(parser)
    args = parser.parse_args()
    dry_run.desde_args(args, 'preventiva')

    load_dotenv()

    supabase_url = os.environ.get('SUPABASE_URL', '')
    srk          = os.environ.get('SUPABASE_SERVICE_ROLE_KEY', '')
    if not supabase_url or not srk:
        log.error('SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY no configurados.')
        sys.exit(1)

    tz_bogota = pytz.timezone('America/Bogota')
    hoy = datetime.now(tz_bogota).strftime('%Y-%m-%d')

    log.info('Cargando overrides y asociaciones existentes...')
    overrides_rows = select_all(supabase_url, srk, 'cartera_preventiva_overrides',
                                 select='llave,cerrado_manual,fecha_pago_manual,valor_cuota_manual')
    llaves_cerradas_manual = {r['llave'] for r in overrides_rows if r.get('cerrado_manual')}
    cerrados_manual_por_llave = {r['llave']: r for r in overrides_rows if r.get('cerrado_manual')}
    overrides_por_llave = {r['llave']: r for r in overrides_rows if r.get('llave')}

    asociaciones_rows = select_all(supabase_url, srk, 'pago_asociaciones',
                                    select='id,matching_key,llave,monto,origen')

    log.info('Cargando cartera_preventiva...')
    cuotas_rows = select_all(
        supabase_url, srk, 'cartera_preventiva',
        select='id,llave,cruce_access,correo,fecha_vencimiento,valor_cuota,valor_a_cobrar,inscrip,'
               'cliente,sistema_financiero,moneda,programa,fecha_pago,valor_pago,fecha_cruce,'
               'diferencia,notificacion,codigo_transaccion_1,pago,pago_confirmado',
    )
    # Códigos de pago de las carteras ANTERIORES, ya archivadas (30 de julio).
    # `codigo_transaccion_1` es la memoria de lo que el proceso manual ya
    # cobró, y vive en el Excel de cartera — así que se pierde entera al
    # cambiar de versión si solo se mira la activa. El archivo de agosto
    # llegó SIN un solo código (el de julio traía 1.581): sin esto, 581 pagos
    # de julio se reaplicarían sobre las cuotas nuevas, y 113 de ellos
    # ($99.812.515) ya los había cobrado una persona a mano.
    #
    # Es la misma regla que ya rige para lo que repartió el pipeline ("un
    # pago se reparte UNA vez en su vida", ver más abajo), extendida a lo que
    # cobró el equipo: si la deuda sigue viva en la cartera nueva se resuelve
    # a mano, no reaplicando plata que ya se usó.
    # OJO con de dónde sale cada código: en esta columna escriben DOS manos.
    # El Excel anota ahí el pago que cobró el proceso manual, pero el propio
    # pipeline también la llena al aplicar un pago (`_fila_cuota_cerrada`).
    # Tomar las dos haría que un pago repartido esta mañana quedara bloqueado
    # por su propio rastro al archivarse la cartera saliente — justo la
    # pérdida que evita la excepción "repartido hoy" de más abajo. Medido: 4
    # de las 22 asociaciones vivas se autobloqueaban así.
    #
    # `fecha_cruce` es la marca de "esto lo tocó el pipeline" (21 de julio):
    # las filas que NO la tienen son las que trae el Excel, y son las únicas
    # que cuentan como memoria del proceso manual.
    log.info('Cargando códigos de pago de carteras archivadas...')
    # El filtro lo hace la base, no Python. De esta tabla solo interesan las
    # filas que traen código y que NO tocó el pipeline (`fecha_cruce` vacía),
    # y son una fracción: 6.269 de 18.503 el 2026-08-02, que se reducen a
    # 1.559 códigos distintos.
    #
    # Traerlas todas y descartarlas acá costaba 8,7 s de los 18 s de la corrida
    # — la lectura más cara del script. Filtrando en la base son 3,1 s, con el
    # mismo conjunto de códigos (verificado). Y la diferencia crece sola: esta
    # tabla acumula una cartera entera por cada versión archivada, para
    # siempre, mientras que lo que hace falta leer casi no crece.
    codigos_archivo_rows = select_all(
        supabase_url, srk, 'cartera_preventiva_archivo',
        select='codigo_transaccion_1',
        filtros={'fecha_cruce': 'is.null', 'codigo_transaccion_1': 'not.is.null'},
    )

    # Llaves que ya pertenecen a una cuota COBRADA (incluidas las líneas
    # partidas que el Excel trae cerradas con su pago anotado).
    # `_generar_llave_saldo` las esquiva para no pisarlas con una línea de
    # saldo nueva que caiga en la misma llave base + misma fecha.
    llaves_cobradas = frozenset(
        c['llave'] for c in cuotas_rows if c.get('llave') and c.get('fecha_pago')
    )

    id_por_llave      = {c['llave']: c['id'] for c in cuotas_rows if c.get('llave')}
    llave_por_id      = {c['id']: c['llave'] for c in cuotas_rows}
    cliente_por_llave = {c['llave']: c.get('cliente') for c in cuotas_rows if c.get('llave')}

    # Valor de cuota corregido a mano desde fin-platform
    # (`cartera_preventiva_overrides.valor_cuota_manual`). La app lo guarda
    # desde el 16 de julio y le promete al usuario "se reflejará en el
    # próximo cruce", pero NADIE lo leía — el campo no aparecía ni una vez en
    # este repo. Se aplica acá, antes que todo lo demás, para que el cierre y
    # la cascada usen el valor corregido. `valor_a_cobrar` se recalcula por la
    # fórmula del Sistema Financiero sobre el abono que trajo el Excel.
    ajustes_valor_cuota: list[dict] = []
    for cuota in cuotas_rows:
        override = overrides_por_llave.get(cuota.get('llave') or '')
        nuevo = override.get('valor_cuota_manual') if override else None
        if nuevo is None:
            continue
        nuevo = round(float(nuevo), 2)
        if round(float(cuota.get('valor_cuota') or 0), 2) == nuevo:
            continue
        pago_excel = _pago_sin_confirmar(cuota)
        cuota['valor_cuota']    = nuevo
        cuota['valor_a_cobrar'] = round(nuevo - pago_excel, 2)
        ajustes_valor_cuota.append({'id': cuota['id'], 'valor_cuota': nuevo,
                                     'valor_a_cobrar': cuota['valor_a_cobrar']})
    if ajustes_valor_cuota:
        upsert_cartera_preventiva(supabase_url, srk, ajustes_valor_cuota)
        log.info('%d cuota(s) con valor corregido a mano aplicado.', len(ajustes_valor_cuota))

    # "Cerrar Cuota" (antes "Cerrar cartera"): cierre MANUAL de una cuota
    # puntual, fuera del proceso (fin-platform escribe cerrado_manual=true +
    # fecha_pago_manual). Estas filas van en listas propias porque
    # `_fila_cierre_cartera` y `_fila_reset` no comparten el mismo set de
    # claves entre sí (ver docstrings) — cada una se postea en su propio
    # array más abajo.
    cierres_cartera: list[dict] = []
    resets_varios: list[dict] = []
    cerrados_cartera = 0
    reabiertos_cartera = 0
    for cuota in cuotas_rows:
        llave = cuota.get('llave') or ''
        if not llave:
            continue
        override = cerrados_manual_por_llave.get(llave)
        fecha_manual = override.get('fecha_pago_manual') if override else None
        if fecha_manual:
            saldo = _saldo_a_cobrar(cuota)
            # Idempotencia: `_saldo_a_cobrar` ya devuelve el saldo que la
            # cuota tenía ANTES de cerrarse (le suma `pago_confirmado`), así
            # que en una cuota ya cerrada esta comparación sigue dando igual
            # corrida tras corrida. `pago_confirmado` en NULL fuerza una
            # pasada más para las cerradas antes del 23 de julio, que todavía
            # no tienen el `pago` escrito.
            ya_reflejado = (
                cuota.get('notificacion') == 'CARTERA'
                and cuota.get('fecha_pago') == fecha_manual
                and cuota.get('valor_pago') is not None
                and cuota.get('pago_confirmado') is not None
                and round(float(cuota['valor_pago']), 2) == round(saldo, 2)
            )
            if ya_reflejado:
                continue
            fila = _fila_cierre_cartera(cuota, fecha_manual, hoy)
            cierres_cartera.append(fila)
            cuota['fecha_pago']      = fecha_manual
            cuota['valor_pago']      = fila['valor_pago']
            cuota['diferencia']      = 0
            cuota['notificacion']    = 'CARTERA'
            cuota['fecha_cruce']     = hoy
            cuota['pago']            = fila['pago']
            cuota['pago_confirmado'] = fila['pago_confirmado']
            cuota['valor_a_cobrar']  = fila['valor_a_cobrar']
            cerrados_cartera += 1
        elif cuota.get('notificacion') == 'CARTERA':
            # cerrado_manual se apagó (reabrir) o nunca tuvo fecha puesta —
            # sin fecha no se cierra (P1), así que si quedó marcada CARTERA
            # de una corrida anterior, se resetea a pendiente. El reset
            # también deshace el cierre (`pago`/`valor_a_cobrar`).
            fila_reset = _fila_reset(cuota)
            resets_varios.append(fila_reset)
            cuota['fecha_pago']      = None
            cuota['valor_pago']      = None
            cuota['diferencia']      = None
            cuota['fecha_cruce']     = None
            cuota['notificacion']    = None
            cuota['pago']            = fila_reset['pago']
            cuota['pago_confirmado'] = None
            cuota['valor_a_cobrar']  = fila_reset['valor_a_cobrar']
            reabiertos_cartera += 1
    if cerrados_cartera or reabiertos_cartera:
        log.info('Cartera manual (regla #8): %d cuota(s) cerradas, %d reabierta(s).',
                  cerrados_cartera, reabiertos_cartera)

    log.info('Cargando ledger de saldo a favor (cartera_saldos_favor)...')
    saldos_favor_rows = select_all(
        supabase_url, srk, 'cartera_saldos_favor',
        select='id,inscrip,cliente,documento,correo,monto,disponible,origen,llave_origen,'
               'matching_key,fecha,aplicado',
    )

    log.info('Cargando cruce_cartera (solo estado_cruce=cruzado)...')
    pagos_rows = select_all(
        supabase_url, srk, 'cruce_cartera',
        select='matching_key,identification,payment_date,transaction_code_1,transaction_code_2,'
               'email,payment_method,payment_amount,estado_cruce,incp,metodo_de_pago',
    )
    pagos_cruzados = [p for p in pagos_rows if p.get('estado_cruce') == 'cruzado' and p.get('incp')]
    pagos_por_matching_key = {p['matching_key']: p for p in pagos_cruzados}

    # Asociaciones huérfanas: el pago ya no está 'cruzado' (ver docstring).
    # Se borran junto con cualquier otra asociación de la misma cuota, y la
    # cuota se resetea para reprocesarse desde cero con lo que quede vigente.
    ids_a_borrar, llaves_a_resetear = [], set()
    for r in asociaciones_rows:
        if r['matching_key'] not in pagos_por_matching_key:
            ids_a_borrar.append(r['id'])
            llaves_a_resetear.add(r['llave'])
    if ids_a_borrar:
        for r in asociaciones_rows:
            if r['llave'] in llaves_a_resetear and r['id'] not in ids_a_borrar:
                ids_a_borrar.append(r['id'])

        delete_by_keys(supabase_url, srk, 'pago_asociaciones', 'id', ids_a_borrar)
        reset_rows = []
        for c in cuotas_rows:
            if c.get('llave') in llaves_a_resetear:
                fila_reset = _fila_reset(c)
                reset_rows.append(fila_reset)
                c['fecha_pago'] = None
                c['valor_pago'] = None
                c['diferencia'] = None
                c['fecha_cruce'] = None
                c['notificacion'] = None
                c['pago'] = fila_reset['pago']
                c['pago_confirmado'] = None
                c['valor_a_cobrar'] = fila_reset['valor_a_cobrar']
        upsert_cartera_preventiva(supabase_url, srk, reset_rows)
        log.info('%d asociación(es) huérfana(s) borradas, %d cuota(s) reseteada(s) para reproceso.',
                  len(ids_a_borrar), len(reset_rows))

    asociaciones_vigentes = [r for r in asociaciones_rows if r['id'] not in ids_a_borrar]
    asociaciones_por_llave: dict[str, list[dict]] = {}
    for a in asociaciones_vigentes:
        asociaciones_por_llave.setdefault(a['llave'], []).append(a)

    actualizaciones_cierre: list[dict] = list(cierres_cartera)
    lineas_nuevas: list[dict] = []
    nuevas_asociaciones: list[dict] = []
    saldos_favor_nuevos: list[dict] = []

    # Pase de reconciliación: toda cuota se recalcula a partir de la SUMA de
    # TODAS sus asociaciones vigentes (mismo umbral que el FIFO automático).
    #
    # AMPLIADO el 23 de julio (punto #1): antes solo miraba las cuotas con al
    # menos una asociación `origen='manual'`, y el reset de más abajo solo
    # cubre las que pierden TODAS sus asociaciones. Entre las dos quedaba un
    # hueco: una cuota cubierta por DOS pagos automáticos a la que se le
    # descarta uno no la tocaba nadie — seguía mostrando la suma de los dos
    # para siempre, y ninguna corrida posterior la volvía a mirar.
    #
    # Es seguro ampliarlo a todas porque el chequeo de idempotencia de abajo
    # descarta las que ya reflejan su suma, que son la enorme mayoría.
    log.info('Reconciliando cuotas contra sus asociaciones vigentes...')
    reconciliadas = 0
    for llave in list(asociaciones_por_llave.keys()):
        if llave in llaves_cerradas_manual:
            continue
        cuota_id = id_por_llave.get(llave)
        if cuota_id is None:
            continue
        cuota = next((c for c in cuotas_rows if c['id'] == cuota_id), None)
        if not cuota:
            continue
        asociaciones_cuota = asociaciones_por_llave.get(llave, [])
        if not asociaciones_cuota:
            continue
        suma = round(sum(a['monto'] for a in asociaciones_cuota), 2)
        # Idempotencia: si la cuota ya refleja esta suma no hay nada que
        # hacer... salvo que esté DEBIENDO y su `diferencia` no lo diga.
        #
        # Caso real (3 de agosto, doc 1020838689): a una cuota cubierta por
        # dos pagos se le descartó uno. fin-platform dejó `valor_pago` en lo
        # que quedó aplicado —correcto— y en `diferencia` la suma de los
        # saldos a favor del cliente, o sea un número POSITIVO. Mirando solo
        # `valor_pago` esta pasada se saltaba la fila, y la cuota quedaba
        # debiendo $11.561 mostrando "saldo a favor $275.200": la pantalla
        # decía que sobraba plata justo donde faltaba, y por eso tampoco
        # ofrecía asociarla.
        #
        # Solo se fuerza el recálculo cuando la cuota está CORTA. Si está
        # cubierta, la `diferencia` positiva es el saldo a favor sin repartir
        # y la escribe su propia pasada (§3.3.2) — pisarla acá la borraría en
        # cada corrida.
        faltante = round(_saldo_a_cobrar(cuota) - suma, 2)
        diferencia_actual = cuota.get('diferencia')
        refleja_faltante = faltante <= 0 or (
            diferencia_actual is not None
            and round(float(diferencia_actual), 2) == -faltante)
        valor_pago_actual = cuota.get('valor_pago')
        if (cuota.get('fecha_cruce') and valor_pago_actual is not None
                and round(float(valor_pago_actual), 2) == suma
                and refleja_faltante):
            continue  # ya refleja esta suma, nada que hacer (idempotencia)

        ultimo_pago = _pago_mas_reciente(asociaciones_cuota, pagos_por_matching_key)
        info = {'cuota': cuota, 'monto_aplicado': suma, 'ultimo_pago': ultimo_pago}
        fila, linea = _cerrar_o_faltante(info, hoy, llaves_cobradas)
        actualizaciones_cierre.append(fila)
        if linea:
            lineas_nuevas.append(linea)
        cuota['valor_pago']      = suma
        cuota['fecha_pago']      = fila.get('fecha_pago')
        cuota['fecha_cruce']     = hoy
        # El cierre reescribe `notificacion`; sin reflejarlo en memoria, la
        # pasada de avisos compara contra el valor viejo, cree que ya está
        # puesto y no lo vuelve a escribir — la fila queda sin aviso hasta la
        # corrida siguiente, que lo repone. Oscilaba entre dos corridas.
        cuota['notificacion']    = fila.get('notificacion')
        # El recálculo deshace el cierre: si a la cuota se le descartó un
        # pago, no puede seguir marcada como pagada (punto #1).
        cuota['pago']            = fila['pago']
        cuota['pago_confirmado'] = None
        cuota['valor_a_cobrar']  = fila['valor_a_cobrar']
        reconciliadas += 1

    # Cuotas que perdieron TODAS sus asociaciones vigentes por un descarte
    # (fin-platform borró pago_asociaciones(P, A)) se resetean a pendiente.
    reseteadas_descarte = 0
    for cuota in cuotas_rows:
        llave = cuota.get('llave') or ''
        if not llave or llave in llaves_cerradas_manual or ' (saldo' in llave:
            continue
        if cuota.get('fecha_pago') is None:
            continue
        if cuota.get('fecha_cruce') is None:
            # El pago NO lo aplicó este pipeline: viene del propio Excel de
            # cartera, que desde el 21 de julio trae sus columnas de pago
            # (una cuota partida en abonos ya cobrada). Esas filas nunca
            # tuvieron `pago_asociaciones`, así que sin esta guarda la regla
            # de abajo las leía como "asociación descartada" y las borraba.
            #
            # Pasó en producción el 21/07: una corrida barrió las 1.409
            # filas que el Excel traía cobradas, y como eso destruyó la
            # lista de pagos ya aplicados, la corrida siguiente del cron
            # volvió a aplicar esos pagos sobre cuotas que ya estaban
            # cerradas. `fecha_cruce` es la marca de "esto lo tocó el
            # pipeline" — el Excel no la trae.
            continue
        if llave in asociaciones_por_llave:
            continue  # todavía tiene asociación(es) vigente(s)
        fila_reset = _fila_reset(cuota)
        resets_varios.append(fila_reset)
        cuota['fecha_pago'] = None
        cuota['valor_pago'] = None
        cuota['diferencia'] = None
        cuota['fecha_cruce'] = None
        cuota['notificacion'] = None
        cuota['pago'] = fila_reset['pago']
        cuota['pago_confirmado'] = None
        cuota['valor_a_cobrar'] = fila_reset['valor_a_cobrar']
        reseteadas_descarte += 1

    if reconciliadas or reseteadas_descarte:
        log.info('%d cuota(s) reconciliadas contra sus asociaciones, %d reseteada(s) por descarte.',
                  reconciliadas, reseteadas_descarte)

    # §3.6: pagos_nuevos = cruzados con INCP, sin ninguna asociación vigente
    # (auto o manual) NI ninguna fila en el ledger (sobrante o descarte) con
    # ese matching_key — esto último es lo que impide que un pago descartado
    # se vuelva a cruzar en el próximo cron.
    matching_keys_asociados = {a['matching_key'] for a in asociaciones_vigentes}
    matching_keys_en_ledger = {c['matching_key'] for c in saldos_favor_rows if c.get('matching_key')}

    # Pagos que el Excel de cartera YA trae aplicados: su
    # `codigo_transaccion_1` es nuestro `matching_key` (verificado el 21 de
    # julio con el pago 7614). Sin esto el pipeline los volvía a aplicar
    # encima del saldo que ya venía neto, duplicando la plata y generando
    # una segunda línea de saldo por la misma deuda.
    # Se unen la cartera VIVA y las ARCHIVADAS: la memoria de lo cobrado a
    # mano no puede depender de que el Excel del mes en curso la traiga.
    matching_keys_en_excel = {
        str(c['codigo_transaccion_1']).strip()
        for c in (*cuotas_rows, *codigos_archivo_rows)
        if c.get('codigo_transaccion_1')
    }

    # Pagos ya aplicados bajo una VERSIÓN ANTERIOR de la cartera (28 de julio).
    # Cargar una cartera nueva archiva `pago_asociaciones` y
    # `cartera_saldos_favor` y deja las vivas vacías, así que sin esto el
    # pipeline pierde la memoria de lo que ya repartió y vuelve a aplicar
    # TODOS los pagos viejos sobre las cuotas nuevas. Medido el día que se
    # cargó la primera cartera nueva: de 41 cuotas resueltas esa corrida, 20
    # eran pagos de días anteriores re-aplicados, contra 23 que el proceso
    # manual había trabajado ese día — la diferencia que reportó el equipo.
    #
    # Regla del usuario: "lo que ya cruzó en cuotas anteriores no tiene nada
    # que ver". Un pago se reparte UNA vez en su vida; si la deuda sigue viva
    # en la cartera nueva, se resuelve a mano, no reaplicando plata vieja.
    #
    # EXCEPCIÓN, y es la que hace que esto no rompa el día del cambio: la
    # corrida de la mañana ya había aplicado los pagos DE HOY contra la
    # cartera saliente, así que al archivar quedaron mezclados con los viejos.
    # Filtrar solo por "está archivado" habría bloqueado también lo de hoy
    # (40 de 41 asociaciones).
    #
    # CRITERIO DE LA EXCEPCIÓN (corregido el 29 de julio). La primera versión
    # miraba la fecha del BANCO: se re-aplicaba si el pago entró al sistema
    # hoy y el banco lo reportaba de hoy o de ayer. Ese es el dato equivocado,
    # porque muchos pagos llegan tarde — medido sobre los 799 pagos cruzados,
    # 273 (34%) traen 2+ días de desfase entre que la persona paga y que el
    # archivo del banco llega, que son los fines de semana de WOMPI. Un pago
    # del viernes que entra el lunes, se aplica en la corrida de las 10:30 y
    # se archiva al mediodía porque cargaron cartera nueva quedaba bloqueado
    # ("es viejo") aunque lo hubiera repartido el pipeline dos horas antes:
    # desaparecía sin dejar rastro, con la cuota abierta y la plata en ningún
    # lado.
    #
    # Ahora el criterio es cuándo lo repartió EL SISTEMA, no cuándo pagó la
    # persona: se re-aplica lo que quedó archivado con fecha de reparto del
    # mismo día en que se cargó la cartera activa (`cartera_cargas`). Es el
    # dato exacto y ya estaba guardado. Verificado contra el swap del 28 de
    # julio: de 1.358 repartos archivados, 21 se hicieron ese mismo día —
    # justo los que había que conservar.
    #
    # Se compara contra la fecha de la CARGA ACTIVA, no contra `hoy`, para
    # que siga siendo correcto si la cartera se carga por la tarde y la
    # corrida siguiente es al otro día.
    archivo_asociaciones = select_all(supabase_url, srk, 'pago_asociaciones_archivo',
                                       select='matching_key,created_at')
    archivo_ledger       = select_all(supabase_url, srk, 'cartera_saldos_favor_archivo',
                                       select='matching_key,created_at')
    cargas_rows = select_all(supabase_url, srk, 'cartera_cargas', select='fecha,estado')
    dia_carga_activa = next(
        (_dia_bogota(c.get('fecha'), tz_bogota) for c in cargas_rows if c.get('estado') == 'activa'),
        '',
    ) or hoy

    matching_keys_archivados: set[str] = set()
    matching_keys_reaplicables: set[str] = set()
    for r in archivo_asociaciones + archivo_ledger:
        mk = r.get('matching_key')
        if not mk:
            continue
        matching_keys_archivados.add(mk)
        if _dia_bogota(r.get('created_at'), tz_bogota) == dia_carga_activa:
            matching_keys_reaplicables.add(mk)

    def _reaplicable(pago: dict) -> bool:
        """¿Un pago ya aplicado bajo una cartera anterior puede volver a
        entrar? Solo si el pipeline lo repartió el día en que se cargó la
        cartera que está activa hoy."""
        return pago['matching_key'] in matching_keys_reaplicables

    def _viejo_sin_oportunidad(pago: dict) -> bool:
        """¿Es un pago que ya estaba disponible cuando se cargó la cartera
        activa y no se aplicó ese día?

        REGLA DEL USUARIO (3 de agosto): la carga de cartera es LA
        oportunidad. Llega la cartera del mes, la plata de las semanas previas
        se acomoda ese día, y lo que no encontró cuota entonces deja de
        aplicarse solo — queda para que una persona lo asocie.

        Nace de un caso real (doc 1020838689): un pago del 16 de julio, de un
        ciclo de cartera anterior, se aplicó solo el 3 de agosto sobre una
        cuota de la cartera de agosto, mezclándose con el pago que sí
        correspondía. El área lo reportó como error, y con razón: qué se hace
        con la plata de una cartera vieja lo decide una persona.

        NO reemplaza a `_reaplicable`, que mira otra cosa: aquél bloquea lo
        que YA se repartió bajo una cartera anterior; éste bloquea lo que
        NUNCA se repartió y se quedó atrás. Hasta hoy ese caso entraba siempre
        (excepción deliberada del 28 de julio, para que la plata no se
        perdiera). Sigue sin perderse: el pago queda en `cruce_cartera` como
        `cruzado`, visible con el filtro "Sin cruce con Cartera Preventiva", y
        se asocia a mano desde financial-platform.

        Medido antes de escribirla, contra todo lo que el pipeline repartió:
        el día de la carga (30 de julio) se aplicaron 118 pagos anteriores a
        esa fecha —el flujo normal, que esta regla no toca porque solo actúa
        DESPUÉS del día de carga— y los repartos del 31 de julio (38) y del 3
        de agosto (102) son todos de pagos posteriores a la carga. Cero daño
        colateral; frenaba exactamente el caso reportado.

        Se compara contra el día de la CARGA ACTIVA, no contra una ventana de
        días, por el mismo motivo que `_reaplicable`: es el dato exacto y ya
        está guardado en `cartera_cargas`."""
        if hoy == dia_carga_activa:
            return False            # el día de la carga se acomoda todo
        fecha = pago.get('payment_date')
        if not fecha:
            return False            # sin fecha no se bloquea nada
        return str(fecha) < dia_carga_activa

    pagos_nuevos = [
        p for p in pagos_cruzados
        if p['matching_key'] not in matching_keys_asociados
        and p['matching_key'] not in matching_keys_en_ledger
        and p['matching_key'] not in matching_keys_en_excel
        and (p['matching_key'] not in matching_keys_archivados or _reaplicable(p))
        and not _viejo_sin_oportunidad(p)
    ]
    bloqueados = sum(1 for p in pagos_cruzados
                     if p['matching_key'] in matching_keys_archivados and not _reaplicable(p))
    viejos_frenados = sum(1 for p in pagos_cruzados
                          if p['matching_key'] not in matching_keys_asociados
                          and p['matching_key'] not in matching_keys_en_ledger
                          and p['matching_key'] not in matching_keys_en_excel
                          and _viejo_sin_oportunidad(p))
    log.info('%d pagos cruzados totales, %d nuevos (sin asociación/ledger previo). '
              '%d bloqueados por venir aplicados de una cartera anterior '
              '(carga activa del %s).',
              len(pagos_cruzados), len(pagos_nuevos), bloqueados, dia_carga_activa)
    if viejos_frenados:
        log.info('%d pago(s) anteriores a la carga activa NO se auto-aplican '
                  '(quedan para asociación manual).', viejos_frenados)

    # §4.1/4.2: cuotas pendientes = sin pago identificado, sin cierre manual
    # y con saldo real por cobrar.
    #
    # `_saldo_a_cobrar(c) > 0` saca las cuotas que el proceso manual ya
    # dejó en cero (`valor_a_cobrar` = 0 porque `PAGO` cubrió la cuota
    # entera). Sin este filtro entran a la cascada por su `valor_cuota`
    # completo y se comen plata que le tocaba a la cuota siguiente —
    # medido el 21 de julio: 899 de 2.628 cuotas estaban en esa situación.
    #
    # Las líneas que el Excel ya trae cobradas (una cuota partida en abonos
    # por el proceso manual) llegan con `fecha_pago` puesto desde el propio
    # Excel, así que este mismo filtro las deja fuera sin regla aparte.
    cuotas_pendientes = [
        c for c in cuotas_rows
        if c.get('fecha_pago') is None
        and c.get('llave') not in llaves_cerradas_manual
        and _saldo_a_cobrar(c) > 0
    ]
    cuotas_por_doc: dict[str, list[dict]] = {}
    for c in cuotas_pendientes:
        doc = _normalizar_documento(c.get('cruce_access'))
        if doc:
            cuotas_por_doc.setdefault(doc, []).append(c)

    # 4.3: inscripciones (base normalizada) con >=1 cuota pendiente, por documento.
    inscripciones_por_doc: dict[str, set[str]] = {
        doc: {_base_inscripcion(c.get('inscrip')) for c in cuotas if c.get('inscrip')}
        for doc, cuotas in cuotas_por_doc.items()
    }

    pagos_nuevos_por_doc: dict[str, list[dict]] = {}
    for p in pagos_nuevos:
        doc = str(p.get('identification') or '').strip()
        if doc and p.get('payment_amount'):
            pagos_nuevos_por_doc.setdefault(doc, []).append(p)

    docs_2_mas_inscripciones = 0
    docs_procesados = 0

    for doc in pagos_nuevos_por_doc:
        inscripciones = inscripciones_por_doc.get(doc, set())
        if not inscripciones:
            continue  # el documento no tiene ninguna cuota pendiente conocida

        if len(inscripciones) >= 2:
            # 4.3: 2+ inscripciones debiendo — no se aplica nada
            # automáticamente, queda para asociación manual.
            docs_2_mas_inscripciones += 1
            continue

        inscripcion_objetivo = next(iter(inscripciones))
        cuotas_inscripcion = [
            c for c in cuotas_por_doc[doc]
            if _base_inscripcion(c.get('inscrip')) == inscripcion_objetivo
        ]
        pagos_para_inscripcion = [
            p for p in pagos_nuevos_por_doc.get(doc, [])
            if _base_inscripcion(p.get('incp')) == inscripcion_objetivo
        ]
        if not pagos_para_inscripcion:
            continue

        cierres_filas, linea_nueva, asociaciones, saldos_favor = _procesar_inscripcion(
            cuotas_inscripcion, pagos_para_inscripcion, hoy, llaves_cobradas)

        actualizaciones_cierre.extend(cierres_filas)
        if linea_nueva:
            lineas_nuevas.append(linea_nueva)
        for a in asociaciones:
            nuevas_asociaciones.append({
                'matching_key': a['matching_key'],
                'llave':        llave_por_id[a['cuota_id']],
                'monto':        a['monto'],
                'origen':       'automatico',
            })
        saldos_favor_nuevos.extend(saldos_favor)
        docs_procesados += 1

    log.info('%d documento(s) procesados, %d con 2+ inscripciones debiendo (sin auto-aplicar).',
              docs_procesados, docs_2_mas_inscripciones)

    # Orden de escritura (ver docstring del módulo, bug crítico 16/07):
    # asociaciones PRIMERO — en cuanto esa escritura confirma, el pago queda
    # reclamado para siempre y un fallo posterior en el ledger/líneas/cierres
    # ya no se repite en cada corrida. Luego el ledger nuevo, luego las
    # líneas nuevas, luego los cierres.
    if nuevas_asociaciones:
        upsert_pago_asociaciones(supabase_url, srk, nuevas_asociaciones)
    if saldos_favor_nuevos:
        upsert_cartera_saldos_favor(supabase_url, srk, saldos_favor_nuevos)
    # La línea de FALTA DE PAGO sigue a la `diferencia` de su cuota original
    # mientras esa original siga abierta (30 de julio). Va ANTES de escribir
    # las líneas nuevas porque puede descartar alguna que ya no corresponde.
    sync_lineas, lineas_a_borrar = _sincronizar_lineas_falta_de_pago(
        cuotas_rows, actualizaciones_cierre, lineas_nuevas,
        llaves_cerradas_manual, asociaciones_por_llave)
    if sync_lineas:
        upsert_cartera_preventiva(supabase_url, srk, sync_lineas)
        log.info('Líneas de deuda ajustadas a la diferencia de su cuota original: %d.',
                  len(sync_lineas))
    if lineas_a_borrar:
        delete_by_keys(supabase_url, srk, 'cartera_preventiva', 'llave', lineas_a_borrar)
        log.info('Líneas de deuda eliminadas (el faltante bajó de %s): %d.',
                  UMBRAL_LINEA_NUEVA, len(lineas_a_borrar))
    if lineas_nuevas:
        insert_cartera_preventiva_lineas(
            supabase_url, srk,
            [{k: v for k, v in ln.items() if not k.startswith('_')} for ln in lineas_nuevas])
    if actualizaciones_cierre:
        batch_size = 500
        for i in range(0, len(actualizaciones_cierre), batch_size):
            upsert_cartera_preventiva(supabase_url, srk, actualizaciones_cierre[i:i + batch_size])
    if resets_varios:
        # Set de claves distinto al de `_fila_cierre` (ver docstring de
        # `_fila_reset`) — SIEMPRE en su propio POST, nunca mezclado con
        # `actualizaciones_cierre`.
        upsert_cartera_preventiva(supabase_url, srk, resets_varios)

    log.info('cruzar_cartera_preventiva.py: %d cuota(s) actualizadas, %d reseteada(s), '
              '%d línea(s) nueva(s), %d asociación(es) nueva(s), %d saldo(s) a favor nuevo(s).',
              len(actualizaciones_cierre), len(resets_varios), len(lineas_nuevas),
              len(nuevas_asociaciones), len(saldos_favor_nuevos))

    # ── Dónde se muestran el saldo a favor y su aviso (28 de julio) ────────
    # Los dos viven en la MISMA cuota, y cuál es esa cuota se RECALCULA en
    # cada corrida: es la última que hoy le sigue quedando asociada al pago.
    #
    # Antes el saldo a favor se plantaba en `llave_origen` —la cuota donde
    # cayó el pago la primera vez— y ese apunte no se movía nunca más. Si
    # después alguien le descartaba el pago a esa cuota, la plata quedaba
    # colgada de una cuota que ya no lo tenía y la que sí lo conservaba
    # quedaba muda. Caso real 16079640 (27 de julio): $4.090.910 visibles en
    # la cuota de julio —descartada y cerrada a mano por Cartera— mientras la
    # de junio, que es la que se quedó con el pago, no mostraba nada. El
    # aviso ya se movía así para los descartes; ahora las dos columnas siguen
    # la misma regla, venga la fila del ledger de un sobrante o de un
    # descarte, y da igual a cuál de las cuotas se le descarte el pago.
    #
    # `llave_origen` queda solo de respaldo, para el pago que se quedó sin
    # ninguna asociación vigente: la plata tiene que verse en algún lado.
    venc_por_llave = {c['llave']: (c.get('fecha_vencimiento') or _FECHA_MAX)
                       for c in cuotas_rows if c.get('llave')}
    llaves_vigentes_por_pago: dict[str, list[str]] = {}
    for a in list(asociaciones_vigentes) + list(nuevas_asociaciones):
        vistas = llaves_vigentes_por_pago.setdefault(a['matching_key'], [])
        if a['llave'] not in vistas:
            vistas.append(a['llave'])
    for vistas in llaves_vigentes_por_pago.values():
        vistas.sort(key=lambda llave: (venc_por_llave.get(llave, _FECHA_MAX), llave))

    # El ledger se agrupa POR PAGO, no fila por fila: un mismo pago puede
    # tener varias filas (su sobrante original más lo que le devuelva un
    # descarte) y todas describen la misma plata suelta de la misma persona.
    saldo_por_pago: dict[str, dict] = {}
    for cr in saldos_favor_rows:
        mk = cr.get('matching_key')
        if not mk:
            continue
        acum = saldo_por_pago.setdefault(
            mk, {'monto': 0.0, 'disponible': 0.0, 'llave_origen': cr.get('llave_origen')})
        acum['monto']      = round(acum['monto'] + float(cr.get('monto') or 0), 2)
        acum['disponible'] = round(acum['disponible'] + float(cr.get('disponible') or 0), 2)

    destino_por_pago: dict[str, str] = {}
    for mk, acum in saldo_por_pago.items():
        vigentes = llaves_vigentes_por_pago.get(mk) or []
        if vigentes:
            # Una cuota cerrada a mano por Cartera se declara saldada, y no es
            # sitio para mostrar plata suelta — era justo la pantalla
            # contradictoria del caso 16079640. Se baja a la anterior que sí
            # tenga el pago; si TODAS están cerradas a mano, se queda en la
            # última antes que dejar la plata invisible.
            abiertas = [k for k in vigentes if k not in llaves_cerradas_manual]
            destino = (abiertas or vigentes)[-1]
        else:
            destino = acum['llave_origen']
        if destino:
            destino_por_pago[mk] = destino

    # §3.3.2 (P3): la `diferencia` POSITIVA de la cuota destino es el saldo a
    # favor de ese pago que sigue sin repartir. fin-platform decrementa
    # `disponible` al asociar; acá solo se refleja, nunca se recalcula. Va
    # DESPUÉS de los cierres (no depende de su orden, es idempotente y de
    # solo lectura sobre el ledger cargado al inicio).
    diferencia_deseada: dict[str, float] = {}
    for mk, destino in destino_por_pago.items():
        if destino not in id_por_llave:
            continue
        diferencia_deseada[destino] = round(
            diferencia_deseada.get(destino, 0.0) + saldo_por_pago[mk]['disponible'], 2)

    # Candidatas a limpieza: toda cuota donde este saldo pudo haberse
    # mostrado antes de moverse. Se acota a cuotas que tocó el pipeline
    # (`fecha_cruce`) y a valores POSITIVOS: la diferencia positiva que trae
    # el propio Excel de cartera (144 filas al 28 de julio, ninguna con
    # `fecha_cruce`) es del proceso manual y no se toca, igual que las
    # negativas, que son de FALTA DE PAGO.
    candidatas_saldo: set[str] = set(diferencia_deseada)
    for mk, acum in saldo_por_pago.items():
        if acum['llave_origen']:
            candidatas_saldo.add(acum['llave_origen'])
        candidatas_saldo.update(llaves_vigentes_por_pago.get(mk) or [])

    sync_diferencia = []
    for llave in sorted(candidatas_saldo):
        cuota_id = id_por_llave.get(llave)
        if cuota_id is None:
            continue
        cuota = next((c for c in cuotas_rows if c['id'] == cuota_id), None)
        if not cuota:
            continue
        actual  = round(float(cuota.get('diferencia') or 0), 2)
        deseada = diferencia_deseada.get(llave)
        if deseada is not None:
            if actual == deseada:
                continue
            if actual < 0:
                # La cuota debe plata (faltante del pase de reconciliación).
                # Escribirle el saldo a favor encima convertiría una deuda en
                # un crédito a la vista. No pasa hoy (excedente y faltante son
                # excluyentes dentro de una misma corrida), pero si llegara a
                # pasar es preferible que se vea en el log a taparlo.
                log.warning('Saldo a favor de %s no se muestra: la cuota debe %s.',
                             llave, actual)
                continue
            sync_diferencia.append({'id': cuota_id, 'diferencia': deseada})
            cuota['diferencia'] = deseada
        elif actual > 0 and cuota.get('fecha_cruce'):
            sync_diferencia.append({'id': cuota_id, 'diferencia': 0})
            cuota['diferencia'] = 0
    if sync_diferencia:
        upsert_cartera_preventiva(supabase_url, srk, sync_diferencia)
        log.info('Saldo a favor: %d cuota(s) sincronizadas con el disponible de su pago.',
                  len(sync_diferencia))

    # ── Notificación de plata sin repartir (23 de julio, punto #2) ─────────
    # Es un aviso VIVO, no un texto que se escribe una vez: se recalcula
    # entero en cada corrida y se limpia cuando deja de aplicar.
    #
    # Regla del usuario: mientras el sobrante de un pago siga ENTERO y sin
    # asignar, la cuota que recibió ese pago avisa cuánto pagó la persona
    # medido en cuotas. En cuanto alguien reparte una parte —así sea una
    # sola— el texto desaparece y queda solo el saldo a favor actualizado.
    #
    # Los tres casos que pidió cubrir salen de la misma regla, sin código
    # aparte: se asigna parte del saldo → `disponible` baja y el texto se va;
    # se asigna todo → igual; el pago se repartió entre dos cuotas y a una se
    # le descarta → esa parte vuelve a ser plata sin repartir (fila
    # `origen='descarte'` en el ledger) y el texto se muda solo a la cuota que
    # conservó el pago, la misma donde queda el saldo a favor (ver el bloque
    # de arriba: los dos usan `destino_por_pago`).
    etiqueta_por_llave: dict[str, str] = {}
    for mk, acum in saldo_por_pago.items():
        if acum['monto'] <= 0 or acum['disponible'] < acum['monto']:
            continue  # ya se repartió algo: no se avisa nada
        destino = destino_por_pago.get(mk)
        cuota_id = id_por_llave.get(destino) if destino else None
        if cuota_id is None:
            continue
        cuota_destino = next((c for c in cuotas_rows if c['id'] == cuota_id), None)
        if not cuota_destino:
            continue
        # Cuántas cuotas cerró ESTE pago: sin esto el aviso cuenta de menos
        # cuando el pago cerró dos o más (caso 16079640: cerró la de junio y
        # la de julio y dejó $4.090.910 sueltos — "PAGA CINCO CUOTAS" cuando
        # la persona pagó seis).
        cubiertas = len(llaves_vigentes_por_pago.get(mk) or []) or 1
        etiqueta = _etiqueta_notificacion(_saldo_a_cobrar(cuota_destino),
                                           acum['disponible'], cubiertas)
        if etiqueta:
            etiqueta_por_llave[destino] = etiqueta

    sync_notificacion = []
    for cuota in cuotas_rows:
        llave = cuota.get('llave') or ''
        if not llave:
            continue
        actual  = cuota.get('notificacion')
        deseada = etiqueta_por_llave.get(llave)
        if deseada:
            # 'FALTA DE PAGO' y 'CARTERA' son de otros dueños y nunca compiten
            # por la misma fila (viven en la cuota nueva del faltante y en una
            # cuota cerrada a mano, que no tiene pago real). Si aun así
            # coincidieran, mandan ellas.
            if actual in ('FALTA DE PAGO', 'CARTERA') or actual == deseada:
                continue
            sync_notificacion.append({'id': cuota['id'], 'notificacion': deseada})
            cuota['notificacion'] = deseada
        elif _es_etiqueta_de_sobrante(actual):
            # Ya no aplica (se repartió el saldo, se descartó el pago, o el
            # sobrante bajó del umbral): se limpia. Solo se tocan las
            # etiquetas de esta familia, nunca las ajenas.
            sync_notificacion.append({'id': cuota['id'], 'notificacion': None})
            cuota['notificacion'] = None
    if sync_notificacion:
        upsert_cartera_preventiva(supabase_url, srk, sync_notificacion)
        log.info('Notificación de sobrante: %d cuota(s) actualizadas.', len(sync_notificacion))

    # Cruce a la inversa (informativo, ver docstring del módulo).
    llaves_por_pago: dict[str, list[str]] = {}
    for r in asociaciones_vigentes:
        llaves_por_pago.setdefault(r['matching_key'], []).append(r['llave'])
    for a in nuevas_asociaciones:
        llaves_por_pago.setdefault(a['matching_key'], []).append(a['llave'])

    # El Excel de cartera también registra pagos ya cobrados (columna CODIGO
    # TRANSACCION1 = nuestro `matching_key`). Cuentan como identificados: el
    # pago está aplicado, solo que su registro vive en el Excel y no en
    # `pago_asociaciones`. Sin esto la columna pasó a decir "sin identificar"
    # en 326 de 335 pagos (21 de julio, al empezar a respetar lo que el Excel
    # trae cobrado). Va DESPUÉS de las asociaciones propias a propósito:
    # `setdefault` deja ganar la nuestra cuando existen las dos.
    for c in cuotas_rows:
        codigo = str(c.get('codigo_transaccion_1') or '').strip()
        if codigo and c.get('llave'):
            llaves_por_pago.setdefault(codigo, []).append(c['llave'])

    log.info('Calculando cruce a la inversa...')
    cruce_updates = []
    for p in pagos_cruzados:
        mk = p['matching_key']
        llaves = llaves_por_pago.get(mk)
        cruce = cliente_por_llave.get(llaves[0]) if llaves else None
        cruce_updates.append({'matching_key': mk, 'cruce': cruce})

    if cruce_updates:
        con_match = sum(1 for u in cruce_updates if u.get('cruce'))
        update_cruce_valores(supabase_url, srk, cruce_updates)
        log.info('Cruce inverso: %d filas actualizadas (%d identificados, %d sin identificar).',
                  len(cruce_updates), con_match, len(cruce_updates) - con_match)

    # Lo último de la corrida: comprobar que la plata cuadra. Va al final
    # porque relee lo que quedó escrito, no lo que se pensaba escribir.
    if dry_run.activo():
        log.info('Cuadre: no se verifica en simulación (nada se escribió).')
    else:
        verificar_cuadre(supabase_url, srk, pagos_por_matching_key)


if __name__ == '__main__':
    main()
    if dry_run.activo():
        log.warning('%s', dry_run.resumen())
