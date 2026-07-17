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
    `financial-platform`, todavía sin construir — este repo va primero
    porque sin el cambio de acá no existe ni un solo caso real contra el
    cual construirlo, ver spec §3.3).
  - Pago parcial (4.4): la cuota que no se cubre del todo se cierra con lo
    que recibió (`diferencia=0`) y el saldo pasa a una LÍNEA NUEVA
    (`llave` + " (saldo)"/" (saldo N)", mismo patrón que los duplicados de
    Fase 1.2) — ya no queda "abierta con diferencia negativa" esperando un
    pago futuro.
  - `cartera_preventiva_overrides` (cierre manual desde la UI) se respeta:
    una cuota con `cerrado_manual=true` nunca entra a la cascada ni se
    toca acá.
  - `correo_elec` (4.6): si el pago es WOMPI automático (encontrado en
    ReportePagosWompi, `cruce_cartera.metodo_de_pago` distinto de
    "PAGOS MANUALES") se muestra "WOMPI (Automático Genera Link)" en vez
    del correo.
  - `fecha_cruce` (4.7): se llena con la fecha de esta corrida en toda cuota
    que la corrida toque (cierre o línea nueva de saldo se deja sin fecha
    hasta que reciba su propio pago).

Asociaciones huérfanas: si un pago que ya estaba asociado deja de estar
`cruzado` en `cruce_cartera` (ej. Fase 2 lo detectó como cesantías/pago por
llave después y lo apartó, borrándolo de `cruce_cartera`), su asociación en
`pago_asociaciones` queda huérfana. Este script la detecta, la borra (junto
con cualquier otra asociación de la misma cuota, para no dejar un estado
mixto) y resetea la cuota a "sin pago identificado" para que se reprocese
con lo que quede vigente. No estaba en el diseño original de Fase 4 (que
asume que "solo se recalcula cuando entra un pago nuevo o alguien edita a
mano"), pero es una consecuencia directa de combinar Fase 2 (retroactivo)
con esta arquitectura incremental — sin esto, una cuota así quedaría
"resuelta" para siempre con datos de un pago que ya no cuenta.

CRUCE (columna cruce_cartera.cruce) — "cruce a la inversa": para cada pago
`cruzado`, confirma si tiene al menos una asociación real en
`pago_asociaciones` (vigente, no huérfana) y, si la tiene, trae el cliente
de esa cuota. Se recalcula sobre TODOS los pagos cruzados en cada corrida
(no solo los nuevos) porque es barato y así siempre refleja el estado
completo, incluidas asociaciones de corridas anteriores. Puramente
informativo — no toca estado_cruce/excepcion_motivo de cruce_cartera.

Sobrantes, Excedentes y Condonación (17 de julio) — REEMPLAZA la Fase 8
("bloque G Montos": `_calcular_notificacion`, tolerancia ±100, texto
"SOBRANTE $X sin cuota registrada" en `correo_elec`). Ver
`~/Documents/Y15U5/Spec - Sobrantes-Excedentes (matching-test).md`.

Modelo: cada inscripción está en modo A (en curso, default) o modo B
(última cuota — activado a mano por un humano en financial-platform,
`cartera_preventiva_overrides.es_ultima_cuota`). La aplicación INICIAL de
un pago es SIEMPRE modo A (§4.3) — el humano marca B recién después de ver
el resultado en pantalla. El paso a modo B (o el regreso a A si se apaga el
flag) lo hace un paso de RECONCILIACIÓN aparte (§4.4), en la corrida
siguiente, que resetea la inscripción completa y la reprocesa desde cero
con el modo correcto.

La tolerancia de redondeo ±100 se ELIMINA — los montos son exactos, lo que
sobre o falte (aunque sea $1) se clasifica según la matriz:

| Situación (tras FIFO)      | Modo A (no es última)          | Modo B (es última)     |
|-----------------------------|---------------------------------|--------------------------|
| Falta < $50.000             | diferencia negativa, sin línea  | CONDONAR (notif=CONDONADO, sin línea) |
| Falta = $50.000 exacto      | cierra dif=0 + línea nueva      | CONDONAR                |
| Falta > $50.000             | cierra dif=0 + línea nueva      | cierra dif=0 + línea nueva (igual que A) |
| Sobra >= $1                 | SOBRANTE: crédito a cartera_saldos_favor, notif=SOBRANTE, NO se suma a valor_pago | EXCEDENTE: se suma a valor_pago, diferencia positiva, notif=EXCEDENTE |

`cartera_saldos_favor` (tabla nueva, `sql/017`) guarda esos créditos — se
anteponen como pagos sintéticos (ordenados por fecha, más viejo primero) en
la próxima corrida que toque esa misma inscripción, y se consumen por FIFO
igual que un pago real. Un crédito, una vez que entra a una cascada, se da
por completamente consumido (`aplicado=true`) — si sobra algo tras esa
cascada, ese remanente se registra como un crédito NUEVO (misma mecánica de
excedente), nunca se deja el crédito viejo "parcialmente" vigente.

`notificacion` se reusa (ya existía desde `sql/014`, Fase 8): ahora solo
tres valores posibles — 'CONDONADO' / 'SOBRANTE' / 'EXCEDENTE' — o NULL
(cierre limpio, o faltante en modo A < $50.000, identificable por
`diferencia < 0`).

BUG CRÍTICO corregido (16 de julio, tarde) — Fase 4 nunca había aplicado un
pago en producción desde su deploy: `pago_asociaciones` en 0 filas,
`fecha_cruce` en 0 filas, y 74 filas basura en `cartera_preventiva` con
llaves encadenadas tipo `2619PN46206 (saldo 9) (saldo 32) (saldo 15)`. Tres
bugs combinados, los tres corregidos:
  1. `actualizaciones_cierre` mezclaba filas de `_fila_cierre()` con
     distinto set de claves (9 claves en un cierre normal, 11 en uno con
     `cerrar_al_monto_recibido=True`, +1 con `notificacion` de Fase 8) en un
     mismo POST — PostgREST exige el mismo set de claves en todo el array
     (`PGRST102: All object keys must match`) y respondía 400. `_fila_cierre`
     ahora siempre devuelve el mismo set; los campos que no cambian de
     verdad se rellenan con el valor QUE YA TIENE la cuota, nunca `None`
     (mandar `None` los habría borrado de verdad). `utils/supabase.py`
     ahora loguea `resp.text` en cada `raise_for_status` — este 400 quedó
     invisible en el log durante horas, solo se veía "HTTPError: 400".
  2. Orden de escritura invertido: `pago_asociaciones` (el marcador de "este
     pago ya se procesó") pasa a escribirse PRIMERO, no al final — antes,
     si algo fallaba después de crear la línea de saldo pero antes de
     escribir la asociación, el reintento recalculaba desde cero contra un
     `cartera_preventiva` que ya había cambiado, reaplicando el mismo pago
     una y otra vez.
  3. `_generar_llave_saldo` dejó de "buscar el primer sufijo libre" (nunca
     estable entre corridas: cada reintento del mismo pago encontraba el
     slot anterior ocupado y generaba uno nuevo) y ahora deriva la llave del
     `matching_key` del pago que la origina — determinística, y el upsert
     por `llave` ya existente la actualiza en vez de duplicarla en cualquier
     reproceso.
Limpieza de las 74 filas basura: ver `sql/015_limpieza_lineas_saldo_bug.sql`
(entregado al usuario, no corrido desde acá).
"""

import logging
import os
import sys
from datetime import datetime

import pytz
from dotenv import load_dotenv

from utils.parser import normalizar_nit, normalizar_sufijo
from utils.supabase import (select_all, delete_by_keys, update_cruce_valores,
                             upsert_cartera_preventiva, insert_cartera_preventiva_lineas,
                             upsert_pago_asociaciones, upsert_cartera_saldos_favor,
                             marcar_saldos_favor_aplicados)

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

# Sobrantes/Excedentes (17 de julio): umbral que separa "ruido de redondeo"
# de "deuda/sobrante real", validado contra los 26 excedentes reales de
# producción — el mayor por debajo es $45.000, el menor por encima es
# $192.349, no hay nada en el medio. Se usa con distinta estrictitud según
# el modo: A dispara línea nueva con `>=` (falta exacta de $50.000 YA es
# demasiada para dejarla como simple diferencia); B dispara CONDONAR con
# `<=` (una falta de exactamente $50.000, siendo la última cuota, se
# perdona).
UMBRAL_LINEA_NUEVA = 50000


def _normalizar_documento(valor) -> str:
    return normalizar_nit(str(valor or '').strip())


def _base_inscripcion(valor) -> str:
    v = str(valor or '').strip()
    return normalizar_sufijo(v) or v


def _es_wompi_automatico(pago: dict) -> bool:
    payment_method = str(pago.get('payment_method') or '').upper()
    metodo = str(pago.get('metodo_de_pago') or '')
    return payment_method.startswith('WOMPI') and bool(metodo) and metodo != PAGOS_MANUALES_LABEL


def _correo_elec_para(pago: dict) -> str:
    if _es_wompi_automatico(pago):
        return WOMPI_LINK_LABEL
    return pago.get('email') or ''


def _generar_llave_saldo(llave_base: str, matching_key: str) -> str:
    """Llave DETERMINÍSTICA para la línea de saldo pendiente de un pago
    parcial, derivada del `matching_key` del pago que la origina — no de
    "buscar el primer sufijo libre" (bug crítico corregido el 16 de julio,
    tarde: esa búsqueda nunca era estable entre corridas. Si el mismo pago
    se reprocesaba tras un fallo a mitad de la escritura —ver docstring de
    "Orden de escritura" en main()—, el sufijo "(saldo)" ya estaba ocupado
    por el intento anterior y se generaba uno nuevo cada vez, produciendo
    una línea de saldo distinta por cada reintento en vez de reusar la
    misma — así se generaron las 74 filas basura encadenadas "(saldo
    9)"/"(saldo 32)"/"(saldo 15)" de la corrida real). Con la llave derivada
    del pago, reprocesar el MISMO pago siempre calcula la MISMA llave, y el
    upsert por `llave` en insert_cartera_preventiva_lineas actualiza esa
    fila en vez de crear una nueva — colisión estructuralmente imposible
    entre pagos distintos, porque matching_key ya es único por diseño en
    todo el sistema."""
    return f'{llave_base} (saldo {matching_key})'


def _fila_reset(cuota_id) -> dict:
    """Dict de reset a NULL de las columnas de resultado del cruce para una
    cuota — reusado tanto por el reset de asociaciones huérfanas como por la
    reconciliación de modo A/B (§4.4)."""
    return {'id': cuota_id, 'fecha_pago': None, 'medio_pago': None, 'valor_pago': None,
            'codigo_transaccion_1': None, 'codigo_transaccion_2': None, 'correo_elec': None,
            'diferencia': None, 'fecha_cruce': None, 'notificacion': None}


def _mismatch_a_b(cuota_frontera: dict, lineas_saldo: list[dict]) -> bool:
    """True si la cuota marcada `es_ultima_cuota` todavía muestra un
    resultado calculado en modo A (SOBRANTE, diferencia negativa sin
    CONDONADO, o una línea de saldo <= UMBRAL_LINEA_NUEVA que en modo B
    debería haberse condonado en vez de generar línea nueva) — dispara el
    reset + reproceso en modo_final=True (§4.4)."""
    if cuota_frontera.get('notificacion') == 'SOBRANTE':
        return True
    diferencia = cuota_frontera.get('diferencia')
    if diferencia is not None and diferencia < 0 and cuota_frontera.get('notificacion') != 'CONDONADO':
        return True
    for linea in lineas_saldo:
        if float(linea.get('valor_cuota') or 0) <= UMBRAL_LINEA_NUEVA:
            return True
    return False


def _mismatch_b_a(cuota: dict) -> bool:
    """True si una cuota NO marcada `es_ultima_cuota` todavía muestra un
    resultado de modo B (CONDONADO/EXCEDENTE) — el toggle se apagó, dispara
    reset + reproceso en modo_final=False (§4.4)."""
    return cuota.get('notificacion') in ('CONDONADO', 'EXCEDENTE')


def _aplicar_pagos_inscripcion(cuotas_abiertas: list[dict], pagos_nuevos: list[dict]):
    """FIFO de pagos_nuevos (ordenados por payment_date — los créditos
    sintéticos de cartera_saldos_favor entran mezclados aquí, con su propia
    fecha, así que naturalmente se consumen primero si son más viejos que
    el resto) contra cuotas_abiertas (ordenadas por fecha_vencimiento) de
    UNA sola inscripción. Montos EXACTOS (17 de julio, se elimina la
    tolerancia ±100 que existía antes): una cuota solo se considera cerrada
    cuando el acumulado la cubre por completo, ni un peso menos.

    Cada cuota tocada se resuelve por completo en esta misma pasada: si el
    dinero disponible no la cubre del todo, se cierra por lo recibido y el
    resto queda para que `_procesar_inscripcion` decida, según la matriz
    A/B, si eso es una simple diferencia negativa, una línea nueva o un
    CONDONADO.

    Devuelve (cierres, parcial, asociaciones, excedente):
      - cierres: [{'cuota', 'monto_aplicado', 'ultimo_pago'}] cuotas
        cubiertas EXACTAMENTE.
      - parcial: misma forma, o None — la ÚLTIMA cuota tocada que se quedó
        sin dinero para completarla. A lo sumo una.
      - asociaciones: [{'matching_key', 'cuota_id', 'monto'}].
      - excedente: plata que sobró tras cubrir TODAS las cuotas conocidas
        de la inscripción (SOBRANTE/EXCEDENTE, según el modo)."""
    cuotas_ordenadas = sorted(cuotas_abiertas, key=lambda c: c.get('fecha_vencimiento') or _FECHA_MAX)
    pagos_ordenados = sorted(pagos_nuevos, key=lambda p: p.get('payment_date') or _FECHA_MAX)

    acumulado: dict = {}
    ultimo_pago_por_cuota: dict = {}
    asociaciones = []
    idx = 0
    excedente = 0.0

    for pago in pagos_ordenados:
        restante = float(pago.get('payment_amount') or 0)
        if restante <= 0:
            continue
        matching_key = pago['matching_key']
        while restante > 0 and idx < len(cuotas_ordenadas):
            cuota = cuotas_ordenadas[idx]
            cuota_id = cuota['id']
            valor_cuota = float(cuota.get('valor_cuota') or 0)
            ya = acumulado.get(cuota_id, 0.0)
            saldo = valor_cuota - ya
            if saldo <= 0:
                idx += 1
                continue
            aplicar = min(restante, saldo)
            acumulado[cuota_id] = ya + aplicar
            ultimo_pago_por_cuota[cuota_id] = pago
            asociaciones.append({'matching_key': matching_key, 'cuota_id': cuota_id, 'monto': round(aplicar, 2)})
            restante -= aplicar
            if valor_cuota - acumulado[cuota_id] <= 0:
                idx += 1
        if restante > 0:
            excedente += restante

    cierres, parcial = [], None
    for cuota in cuotas_ordenadas:
        cuota_id = cuota['id']
        if cuota_id not in acumulado:
            continue
        monto = acumulado[cuota_id]
        valor_cuota = float(cuota.get('valor_cuota') or 0)
        info = {'cuota': cuota, 'monto_aplicado': monto, 'ultimo_pago': ultimo_pago_por_cuota[cuota_id]}
        if valor_cuota - monto <= 0:
            cierres.append(info)
        else:
            parcial = info

    return cierres, parcial, asociaciones, excedente


def _fila_cierre(info: dict, hoy: str, cerrar_al_monto_recibido: bool = False) -> dict:
    """Bug crítico corregido (16 de julio, tarde): TODAS las filas que
    terminan en `actualizaciones_cierre` se postean juntas en un solo POST
    (`upsert_cartera_preventiva`, un array JSON) — y PostgREST exige que
    cada objeto de ese array tenga exactamente el mismo set de claves, o
    responde 400 (`PGRST102: All object keys must match`). Esta función
    siempre devuelve el mismo set de claves; para las que no cambian de
    verdad (`valor_cuota`/`valor_a_cobrar` en un cierre normal), se manda el
    valor QUE YA TIENE la cuota — nunca None, porque None SÍ los borraría
    (es un upsert real, no un no-op)."""
    cuota = info['cuota']
    ultimo_pago = info['ultimo_pago']
    monto = round(info['monto_aplicado'], 2)
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
    }
    if cerrar_al_monto_recibido:
        # La cuota ORIGINAL de un pago parcial que sí genera línea nueva se
        # ajusta a lo realmente recibido (no al monto original) y queda
        # cerrada con diferencia=0 — el saldo real pasa a la línea nueva.
        fila['valor_cuota']    = monto
        fila['valor_a_cobrar'] = monto
        fila['diferencia']     = 0
    else:
        valor_cuota = float(cuota.get('valor_cuota') or 0)
        fila['diferencia']     = round(monto - valor_cuota, 2)
        fila['valor_cuota']    = cuota.get('valor_cuota')
        fila['valor_a_cobrar'] = cuota.get('valor_a_cobrar')
    return fila


def _fila_linea_saldo(parcial: dict, nueva_llave: str) -> dict:
    cuota = parcial['cuota']
    valor_cuota_original = float(cuota.get('valor_cuota') or 0)
    saldo = round(valor_cuota_original - parcial['monto_aplicado'], 2)
    return {
        'llave':               nueva_llave,
        'sistema_financiero':  cuota.get('sistema_financiero'),
        'inscrip':             cuota.get('inscrip'),
        'cliente':             cuota.get('cliente'),
        'moneda':              cuota.get('moneda'),
        'fecha_vencimiento':   cuota.get('fecha_vencimiento'),
        'programa':            cuota.get('programa'),
        'cruce_access':        cuota.get('cruce_access'),
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
    }


def _procesar_inscripcion(cuotas_inscripcion: list[dict], pagos_para: list[dict],
                           creditos_para: list[dict], modo_final: bool, hoy: str):
    """Corre el FIFO (créditos de cartera_saldos_favor como pagos sintéticos,
    mezclados con los pagos reales) de UNA inscripción y arma las filas de
    escritura según la matriz A/B (ver docstring del módulo, sección
    "Sobrantes, Excedentes y Condonación").

    `modo_final=False` (modo A) es lo único que usa el flujo normal (§4.3,
    "la aplicación inicial de un pago es SIEMPRE modo A"). `modo_final=True`
    (modo B) solo lo llama la reconciliación (§4.4), sobre una inscripción
    ya reseteada por completo.

    No escribe nada en Supabase — solo calcula. Devuelve:
      (cierres_filas, linea_nueva_o_None, asociaciones, ids_creditos_a_marcar_aplicados, saldo_favor_nuevo_o_None)
    """
    pagos_sinteticos = [{
        'matching_key':        c['matching_key'],
        'payment_date':        c.get('fecha'),
        'payment_amount':      c['monto'],
        'payment_method':      None,
        'transaction_code_1':  None,
        'transaction_code_2':  None,
        'email':               None,
        'metodo_de_pago':      None,
    } for c in creditos_para]

    cierres, parcial, asociaciones, excedente = _aplicar_pagos_inscripcion(
        cuotas_inscripcion, pagos_sinteticos + pagos_para)

    cierres_filas = [_fila_cierre(info, hoy) for info in cierres]
    linea_nueva = None

    if parcial:
        cuota = parcial['cuota']
        s = round(float(cuota.get('valor_cuota') or 0) - parcial['monto_aplicado'], 2)
        crea_linea = (s >= UMBRAL_LINEA_NUEVA) if not modo_final else (s > UMBRAL_LINEA_NUEVA)
        if crea_linea:
            fila = _fila_cierre(parcial, hoy, cerrar_al_monto_recibido=True)
            cierres_filas.append(fila)
            nueva_llave = _generar_llave_saldo(cuota['llave'], parcial['ultimo_pago']['matching_key'])
            linea_nueva = _fila_linea_saldo(parcial, nueva_llave)
        else:
            fila = _fila_cierre(parcial, hoy, cerrar_al_monto_recibido=False)
            if modo_final:
                fila['notificacion'] = 'CONDONADO'
            cierres_filas.append(fila)

    saldo_favor_nuevo = None
    if excedente > 0 and cierres_filas:
        # El excedente lo absorbe la ÚLTIMA cuota que toca el pago (nunca la
        # parcial: excedente y parcial son mutuamente excluyentes dentro de
        # una misma llamada — si sobra plata es porque TODAS las cuotas ya
        # se cerraron, ver docstring de _aplicar_pagos_inscripcion).
        ultima_fila = cierres_filas[-1]
        valor_cuota_ultima = float(cierres[-1]['cuota'].get('valor_cuota') or 0)
        if modo_final:
            ultima_fila['valor_pago'] = round(ultima_fila['valor_pago'] + excedente, 2)
            ultima_fila['diferencia'] = round(ultima_fila['valor_pago'] - valor_cuota_ultima, 2)
            ultima_fila['notificacion'] = 'EXCEDENTE'
        else:
            ultima_fila['notificacion'] = 'SOBRANTE'
            ultimo_pago = cierres[-1]['ultimo_pago']
            saldo_favor_nuevo = {
                'inscrip':      cierres[-1]['cuota'].get('inscrip'),
                'cliente':      cierres[-1]['cuota'].get('cliente'),
                'monto':        round(excedente, 2),
                'llave_origen': cierres[-1]['cuota']['llave'],
                'matching_key': ultimo_pago['matching_key'],
                'fecha':        ultimo_pago.get('payment_date'),
                'aplicado':     False,
            }

    # Un crédito que entra a una cascada se da por completamente consumido
    # (aplicado=true) — si sobra algo tras la cascada, ese remanente ya
    # quedó registrado arriba como un crédito NUEVO (saldo_favor_nuevo).
    ids_creditos_aplicados = [c['id'] for c in creditos_para]

    return cierres_filas, linea_nueva, asociaciones, ids_creditos_aplicados, saldo_favor_nuevo


def _reconciliar_inscripcion(supabase_url, srk, doc, inscripcion_base, modo_objetivo, hoy,
                              cuotas_insc, lineas_saldo, llaves_cerradas_manual,
                              asociaciones_vigentes, creditos_rows, pagos_cruzados, llave_por_id,
                              actualizaciones_cierre, lineas_nuevas, nuevas_asociaciones,
                              saldos_favor_nuevos, creditos_a_marcar_aplicados):
    """§4.4: resetea POR COMPLETO una inscripción (asociaciones automáticas,
    créditos, líneas de saldo sintéticas, columnas de resultado de sus
    cuotas) y la reprocesa desde cero con `modo_objetivo` (True=B, False=A)
    — nunca reutiliza créditos ni asociaciones viejas, para no arrastrar
    estado mixto entre los dos modos. Las cuotas con `cerrado_manual=true`
    en overrides se dejan intactas (mismo criterio que en el flujo normal:
    nunca entran a la cascada)."""
    cuotas_normales = [
        c for c in cuotas_insc
        if c not in lineas_saldo and c.get('llave') not in llaves_cerradas_manual
    ]
    llaves_insc = {c['llave'] for c in cuotas_insc}

    ids_asoc_borrar = [a['id'] for a in asociaciones_vigentes
                        if a['llave'] in llaves_insc and a.get('origen') == 'automatico']
    if ids_asoc_borrar:
        delete_by_keys(supabase_url, srk, 'pago_asociaciones', 'id', ids_asoc_borrar)
        asociaciones_vigentes[:] = [a for a in asociaciones_vigentes if a['id'] not in ids_asoc_borrar]

    creditos_insc = [c for c in creditos_rows if _base_inscripcion(c.get('inscrip')) == inscripcion_base]
    if creditos_insc:
        delete_by_keys(supabase_url, srk, 'cartera_saldos_favor', 'id', [c['id'] for c in creditos_insc])
        ids_creditos_borrar = {c['id'] for c in creditos_insc}
        creditos_rows[:] = [c for c in creditos_rows if c['id'] not in ids_creditos_borrar]

    if lineas_saldo:
        delete_by_keys(supabase_url, srk, 'cartera_preventiva', 'id', [c['id'] for c in lineas_saldo])

    reset_rows = [_fila_reset(c['id']) for c in cuotas_normales]
    if reset_rows:
        upsert_cartera_preventiva(supabase_url, srk, reset_rows)
    for c in cuotas_normales:
        c['fecha_pago'] = None
        c['diferencia'] = None
        c['notificacion'] = None

    pagos_insc = [p for p in pagos_cruzados if _base_inscripcion(p.get('incp')) == inscripcion_base]

    cierres_filas, linea_nueva, asociaciones, ids_creditos_ok, saldo_favor = _procesar_inscripcion(
        cuotas_normales, pagos_insc, [], modo_objetivo, hoy)

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
    if saldo_favor:
        saldos_favor_nuevos.append(saldo_favor)
    creditos_a_marcar_aplicados.extend(ids_creditos_ok)

    log.info('Reconciliada inscripción %s (doc %s) -> modo %s: %d cierre(s), %d asociación(es).',
              inscripcion_base, doc, 'B' if modo_objetivo else 'A', len(cierres_filas), len(asociaciones))


def main():
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
                                 select='llave,cerrado_manual,es_ultima_cuota')
    llaves_cerradas_manual = {r['llave'] for r in overrides_rows if r.get('cerrado_manual')}
    es_ultima_llaves = {r['llave'] for r in overrides_rows if r.get('es_ultima_cuota')}

    asociaciones_rows = select_all(supabase_url, srk, 'pago_asociaciones',
                                    select='id,matching_key,llave,origen')

    log.info('Cargando cartera_preventiva...')
    cuotas_rows = select_all(
        supabase_url, srk, 'cartera_preventiva',
        select='id,llave,cruce_access,fecha_vencimiento,valor_cuota,valor_a_cobrar,inscrip,'
               'cliente,sistema_financiero,moneda,programa,fecha_pago,diferencia,notificacion',
    )
    id_por_llave      = {c['llave']: c['id'] for c in cuotas_rows if c.get('llave')}
    llave_por_id      = {c['id']: c['llave'] for c in cuotas_rows}
    cliente_por_llave = {c['llave']: c.get('cliente') for c in cuotas_rows if c.get('llave')}

    cuotas_por_doc_todas: dict[str, list[dict]] = {}
    for c in cuotas_rows:
        doc = _normalizar_documento(c.get('cruce_access'))
        if doc:
            cuotas_por_doc_todas.setdefault(doc, []).append(c)
    doc_por_inscripcion: dict[str, str] = {}
    for doc, cuotas in cuotas_por_doc_todas.items():
        for c in cuotas:
            base = _base_inscripcion(c.get('inscrip'))
            if base:
                doc_por_inscripcion.setdefault(base, doc)

    log.info('Cargando créditos a favor (cartera_saldos_favor)...')
    creditos_rows = select_all(supabase_url, srk, 'cartera_saldos_favor',
                                select='id,inscrip,cliente,monto,llave_origen,matching_key,fecha,aplicado')

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
        reset_rows = [_fila_reset(id_por_llave[llave]) for llave in llaves_a_resetear if llave in id_por_llave]
        upsert_cartera_preventiva(supabase_url, srk, reset_rows)
        # Reflejar el reset en la copia local para que esta misma corrida
        # ya considere estas cuotas como pendientes.
        for c in cuotas_rows:
            if c.get('llave') in llaves_a_resetear:
                c['fecha_pago'] = None
                c['diferencia'] = None
                c['notificacion'] = None
        log.info('%d asociación(es) huérfana(s) borradas, %d cuota(s) reseteada(s) para reproceso.',
                  len(ids_a_borrar), len(reset_rows))

    asociaciones_vigentes = [r for r in asociaciones_rows if r['id'] not in ids_a_borrar]

    # §4.4 — Reconciliación de modo A/B (es_ultima_cuota), AL INICIO, antes
    # de procesar pagos nuevos (ver docstring del módulo, "Sobrantes,
    # Excedentes y Condonación"). Los outputs se acumulan en las mismas
    # listas que usa el flujo normal §4.3, para escribirse juntos al final.
    actualizaciones_cierre: list[dict] = []
    lineas_nuevas: list[dict] = []
    nuevas_asociaciones: list[dict] = []
    saldos_favor_nuevos: list[dict] = []
    creditos_a_marcar_aplicados: list[int] = []
    docs_reconciliados: set[str] = set()

    log.info('Reconciliando modo A/B (es_ultima_cuota)...')
    for llave in es_ultima_llaves:
        cuota_id = id_por_llave.get(llave)
        if cuota_id is None:
            continue
        cuota_frontera = next((c for c in cuotas_rows if c['id'] == cuota_id), None)
        if not cuota_frontera or cuota_frontera.get('fecha_pago') is None:
            continue  # todavía no la toca ningún pago, nada que reconciliar
        doc = _normalizar_documento(cuota_frontera.get('cruce_access'))
        inscripcion_base = _base_inscripcion(cuota_frontera.get('inscrip'))
        if not doc or not inscripcion_base or doc in docs_reconciliados:
            continue
        cuotas_insc = [c for c in cuotas_por_doc_todas.get(doc, [])
                       if _base_inscripcion(c.get('inscrip')) == inscripcion_base]
        lineas_saldo = [c for c in cuotas_insc if ' (saldo' in (c.get('llave') or '')]
        if _mismatch_a_b(cuota_frontera, lineas_saldo):
            _reconciliar_inscripcion(
                supabase_url, srk, doc, inscripcion_base, True, hoy,
                cuotas_insc, lineas_saldo, llaves_cerradas_manual,
                asociaciones_vigentes, creditos_rows, pagos_cruzados, llave_por_id,
                actualizaciones_cierre, lineas_nuevas, nuevas_asociaciones,
                saldos_favor_nuevos, creditos_a_marcar_aplicados,
            )
            docs_reconciliados.add(doc)

    for cuota in list(cuotas_rows):
        llave = cuota.get('llave') or ''
        if not llave or llave in es_ultima_llaves or ' (saldo' in llave:
            continue
        if not _mismatch_b_a(cuota):
            continue
        doc = _normalizar_documento(cuota.get('cruce_access'))
        inscripcion_base = _base_inscripcion(cuota.get('inscrip'))
        if not doc or not inscripcion_base or doc in docs_reconciliados:
            continue
        cuotas_insc = [c for c in cuotas_por_doc_todas.get(doc, [])
                       if _base_inscripcion(c.get('inscrip')) == inscripcion_base]
        lineas_saldo = [c for c in cuotas_insc if ' (saldo' in (c.get('llave') or '')]
        _reconciliar_inscripcion(
            supabase_url, srk, doc, inscripcion_base, False, hoy,
            cuotas_insc, lineas_saldo, llaves_cerradas_manual,
            asociaciones_vigentes, creditos_rows, pagos_cruzados, llave_por_id,
            actualizaciones_cierre, lineas_nuevas, nuevas_asociaciones,
            saldos_favor_nuevos, creditos_a_marcar_aplicados,
        )
        docs_reconciliados.add(doc)

    if docs_reconciliados:
        log.info('%d inscripción(es) reconciliadas (cambio de modo A/B).', len(docs_reconciliados))

    # matching_keys "reclamados": lo que ya venía de antes MÁS lo que la
    # reconciliación acaba de calcular (todavía no persistido en Supabase,
    # pero sí en `nuevas_asociaciones`) — evita que el flujo normal §4.3
    # vuelva a tocar esos mismos pagos con modo_final=False y deshaga lo
    # que la reconciliación acaba de resolver.
    matching_keys_ya_asociados = {r['matching_key'] for r in asociaciones_vigentes}
    matching_keys_ya_asociados.update(a['matching_key'] for a in nuevas_asociaciones)

    # §4.1/4.2: cuotas pendientes = sin pago identificado y sin cierre manual.
    cuotas_pendientes = [
        c for c in cuotas_rows
        if c.get('fecha_pago') is None and c.get('llave') not in llaves_cerradas_manual
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

    pagos_nuevos = [p for p in pagos_cruzados if p['matching_key'] not in matching_keys_ya_asociados]
    log.info('%d pagos cruzados totales, %d nuevos (sin asociación previa).',
              len(pagos_cruzados), len(pagos_nuevos))

    pagos_nuevos_por_doc: dict[str, list[dict]] = {}
    for p in pagos_nuevos:
        doc = str(p.get('identification') or '').strip()
        if doc and p.get('payment_amount'):
            pagos_nuevos_por_doc.setdefault(doc, []).append(p)

    # Créditos pendientes de consumir (excluye los que la reconciliación
    # acaba de borrar/recrear más arriba) — se anteponen a los pagos nuevos
    # de la misma inscripción como pagos sintéticos (§4.3), aunque ese
    # documento no tenga NINGÚN pago nuevo esta corrida (el crédito puede
    # estar esperando a que llegue una cuota nueva del próximo archivo de
    # Cartera Preventiva).
    creditos_pendientes = [c for c in creditos_rows if not c.get('aplicado')]
    creditos_por_doc: dict[str, list[dict]] = {}
    for cr in creditos_pendientes:
        base = _base_inscripcion(cr.get('inscrip'))
        doc = doc_por_inscripcion.get(base)
        if doc:
            creditos_por_doc.setdefault(doc, []).append(cr)

    docs_2_mas_inscripciones = 0
    docs_procesados = 0
    docs_a_procesar = set(pagos_nuevos_por_doc) | set(creditos_por_doc)

    for doc in docs_a_procesar:
        if doc in docs_reconciliados:
            continue
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
        creditos_para_inscripcion = [
            c for c in creditos_por_doc.get(doc, [])
            if _base_inscripcion(c.get('inscrip')) == inscripcion_objetivo
        ]
        if not pagos_para_inscripcion and not creditos_para_inscripcion:
            continue

        # Flujo normal (§4.3): SIEMPRE modo A — el humano recién marca modo
        # B después de ver el resultado; la reconciliación (§4.4, arriba) es
        # la única que llama esto con modo_final=True.
        cierres_filas, linea_nueva, asociaciones, ids_creditos_ok, saldo_favor = _procesar_inscripcion(
            cuotas_inscripcion, pagos_para_inscripcion, creditos_para_inscripcion,
            modo_final=False, hoy=hoy)

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
        if saldo_favor:
            saldos_favor_nuevos.append(saldo_favor)
        creditos_a_marcar_aplicados.extend(ids_creditos_ok)
        docs_procesados += 1

    log.info('%d documento(s) procesados, %d con 2+ inscripciones debiendo (sin auto-aplicar).',
              docs_procesados, docs_2_mas_inscripciones)

    # Orden de escritura (ver docstring del módulo, bug crítico 16/07):
    # asociaciones y créditos "reclamados" PRIMERO — en cuanto esas
    # escrituras confirman, el pago/crédito queda reclamado para siempre y
    # un fallo posterior en líneas/cierres/saldos nuevos ya no se repite en
    # cada corrida.
    if nuevas_asociaciones:
        upsert_pago_asociaciones(supabase_url, srk, nuevas_asociaciones)
    if creditos_a_marcar_aplicados:
        marcar_saldos_favor_aplicados(supabase_url, srk, creditos_a_marcar_aplicados)
    if lineas_nuevas:
        insert_cartera_preventiva_lineas(supabase_url, srk, lineas_nuevas)
    if actualizaciones_cierre:
        batch_size = 500
        for i in range(0, len(actualizaciones_cierre), batch_size):
            upsert_cartera_preventiva(supabase_url, srk, actualizaciones_cierre[i:i + batch_size])
    if saldos_favor_nuevos:
        upsert_cartera_saldos_favor(supabase_url, srk, saldos_favor_nuevos)

    log.info('cruzar_cartera_preventiva.py: %d cuota(s) cerradas, %d línea(s) de saldo nuevas, '
              '%d asociación(es) nuevas, %d crédito(s) nuevo(s), %d crédito(s) consumido(s).',
              len(actualizaciones_cierre), len(lineas_nuevas), len(nuevas_asociaciones),
              len(saldos_favor_nuevos), len(creditos_a_marcar_aplicados))

    # Cruce a la inversa (informativo, ver docstring del módulo).
    llaves_por_pago: dict[str, list[str]] = {}
    for r in asociaciones_vigentes:
        llaves_por_pago.setdefault(r['matching_key'], []).append(r['llave'])
    for a in nuevas_asociaciones:
        llaves_por_pago.setdefault(a['matching_key'], []).append(a['llave'])

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


if __name__ == '__main__':
    main()
