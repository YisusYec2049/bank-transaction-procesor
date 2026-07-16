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
  - Tolerancia de redondeo ±100 pesos (confirmado por el usuario): un saldo
    de $100 o menos se considera cuota cubierta, no genera línea nueva —
    los desfases de 1-2 pesos en WOMPI son redondeo real (Sistema
    Financiero vs. lo que cobró el link), no error.
  - `correo_elec` (4.6): si el pago es WOMPI automático (encontrado en
    ReportePagosWompi, `cruce_cartera.metodo_de_pago` distinto de
    "PAGOS MANUALES") se muestra "WOMPI (Automático Genera Link)" en vez
    del correo. Si además hay SOBRANTE, se concatena con " | " (confirmado
    por el usuario).
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
                             upsert_pago_asociaciones)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%S',
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

# Confirmado por el usuario el 16 de julio.
TOLERANCIA_REDONDEO = 100
WOMPI_LINK_LABEL = 'WOMPI (Automático Genera Link)'
PAGOS_MANUALES_LABEL = 'PAGOS MANUALES'
_FECHA_MAX = '9999-12-31'


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


def _generar_llave_saldo(llave_base: str, llaves_existentes: set[str]) -> str:
    """Llave nueva para la línea de saldo pendiente de un pago parcial
    (Fase 4.4) — mismo patrón "(duplicado)"/"(duplicado N)" de Fase 1.2,
    con la etiqueta "(saldo)"."""
    n = 1
    candidato = f'{llave_base} (saldo)'
    while candidato in llaves_existentes:
        n += 1
        candidato = f'{llave_base} (saldo {n})'
    return candidato


def _aplicar_pagos_inscripcion(cuotas_abiertas: list[dict], pagos_nuevos: list[dict],
                                tolerancia: float = TOLERANCIA_REDONDEO):
    """FIFO de pagos_nuevos (ordenados por payment_date) contra
    cuotas_abiertas (ordenadas por fecha_vencimiento) de UNA sola
    inscripción (4.1). Cada cuota tocada se resuelve por completo en esta
    misma pasada: si el pago no la cubre del todo (más allá de la
    tolerancia), se cierra por lo recibido y el resto queda para una línea
    nueva (4.4) — no se deja "abierta" esperando un pago futuro.

    Devuelve (cierres, parcial, asociaciones, excedente):
      - cierres: [{'cuota', 'monto_aplicado', 'ultimo_pago'}] cuotas
        cubiertas del todo (dentro de tolerancia).
      - parcial: misma forma, o None — la ÚLTIMA cuota tocada que se quedó
        sin pagos para completarla (genera línea de saldo). A lo sumo una.
      - asociaciones: [{'matching_key', 'cuota_id', 'monto'}].
      - excedente: plata que sobró tras cubrir TODAS las cuotas conocidas
        de la inscripción (SOBRANTE, informativo)."""
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
            if saldo <= tolerancia:
                idx += 1
                continue
            aplicar = min(restante, saldo)
            acumulado[cuota_id] = ya + aplicar
            ultimo_pago_por_cuota[cuota_id] = pago
            asociaciones.append({'matching_key': matching_key, 'cuota_id': cuota_id, 'monto': round(aplicar, 2)})
            restante -= aplicar
            if valor_cuota - acumulado[cuota_id] <= tolerancia:
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
        if valor_cuota - monto <= tolerancia:
            cierres.append(info)
        else:
            parcial = info

    return cierres, parcial, asociaciones, excedente


def _fila_cierre(info: dict, hoy: str, cerrar_al_monto_recibido: bool = False) -> dict:
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
    }
    if cerrar_al_monto_recibido:
        # 4.4: la cuota ORIGINAL de un pago parcial se ajusta a lo
        # realmente recibido (no al monto original) y queda cerrada con
        # diferencia=0 — el saldo real pasa a la línea nueva.
        fila['valor_cuota']    = monto
        fila['valor_a_cobrar'] = monto
        fila['diferencia']     = 0
    else:
        valor_cuota = float(cuota.get('valor_cuota') or 0)
        fila['diferencia'] = round(monto - valor_cuota, 2)
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
                                 select='llave,cerrado_manual')
    llaves_cerradas_manual = {r['llave'] for r in overrides_rows if r.get('cerrado_manual')}

    asociaciones_rows = select_all(supabase_url, srk, 'pago_asociaciones',
                                    select='id,matching_key,llave')

    log.info('Cargando cartera_preventiva...')
    cuotas_rows = select_all(
        supabase_url, srk, 'cartera_preventiva',
        select='id,llave,cruce_access,fecha_vencimiento,valor_cuota,inscrip,cliente,'
               'sistema_financiero,moneda,programa,fecha_pago',
    )
    id_por_llave      = {c['llave']: c['id'] for c in cuotas_rows if c.get('llave')}
    llave_por_id       = {c['id']: c['llave'] for c in cuotas_rows}
    cliente_por_llave = {c['llave']: c.get('cliente') for c in cuotas_rows if c.get('llave')}
    llaves_existentes  = set(llave_por_id.values())

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
        reset_rows = [
            {'id': id_por_llave[llave], 'fecha_pago': None, 'medio_pago': None, 'valor_pago': None,
             'codigo_transaccion_1': None, 'codigo_transaccion_2': None, 'correo_elec': None,
             'diferencia': None, 'fecha_cruce': None}
            for llave in llaves_a_resetear if llave in id_por_llave
        ]
        upsert_cartera_preventiva(supabase_url, srk, reset_rows)
        # Reflejar el reset en la copia local para que esta misma corrida
        # ya considere estas cuotas como pendientes.
        for c in cuotas_rows:
            if c.get('llave') in llaves_a_resetear:
                c['fecha_pago'] = None
        log.info('%d asociación(es) huérfana(s) borradas, %d cuota(s) reseteada(s) para reproceso.',
                  len(ids_a_borrar), len(reset_rows))

    asociaciones_vigentes = [r for r in asociaciones_rows if r['id'] not in ids_a_borrar]
    matching_keys_ya_asociados = {r['matching_key'] for r in asociaciones_vigentes}
    llaves_por_pago: dict[str, list[str]] = {}
    for r in asociaciones_vigentes:
        llaves_por_pago.setdefault(r['matching_key'], []).append(r['llave'])

    # 4.1/4.2: cuotas pendientes = sin pago identificado y sin cierre manual.
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

    actualizaciones_cierre: list[dict] = []
    lineas_nuevas: list[dict] = []
    nuevas_asociaciones: list[dict] = []
    docs_2_mas_inscripciones = 0
    docs_procesados = 0

    for doc, pagos_doc in pagos_nuevos_por_doc.items():
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
            p for p in pagos_doc
            if _base_inscripcion(p.get('incp')) == inscripcion_objetivo
        ]
        if not pagos_para_inscripcion:
            continue

        cierres, parcial, asociaciones, excedente = _aplicar_pagos_inscripcion(
            cuotas_inscripcion, pagos_para_inscripcion)

        cierres_doc = [_fila_cierre(info, hoy) for info in cierres]
        if parcial:
            cierres_doc.append(_fila_cierre(parcial, hoy, cerrar_al_monto_recibido=True))
            nueva_llave = _generar_llave_saldo(parcial['cuota']['llave'], llaves_existentes)
            llaves_existentes.add(nueva_llave)
            lineas_nuevas.append(_fila_linea_saldo(parcial, nueva_llave))

        if excedente > 0 and cierres_doc:
            monto_fmt = f'{excedente:,.0f}'.replace(',', '.')
            correo_original = cierres_doc[-1]['correo_elec'] or ''
            cierres_doc[-1]['correo_elec'] = f'{correo_original} | SOBRANTE ${monto_fmt} sin cuota registrada'.strip(' |')

        actualizaciones_cierre.extend(cierres_doc)
        for a in asociaciones:
            nuevas_asociaciones.append({
                'matching_key': a['matching_key'],
                'llave':        llave_por_id[a['cuota_id']],
                'monto':        a['monto'],
                'origen':       'automatico',
            })
        docs_procesados += 1

    log.info('%d documento(s) procesados, %d con 2+ inscripciones debiendo (sin auto-aplicar).',
              docs_procesados, docs_2_mas_inscripciones)

    # Orden de escritura: líneas nuevas -> cierres -> asociaciones. Si algo
    # falla a mitad de camino, la asociación (que marca el pago como "ya
    # procesado") es lo ÚLTIMO en escribirse, así un reintento reprocesa
    # limpio en vez de dejar una línea de saldo huérfana sin su cierre.
    if lineas_nuevas:
        insert_cartera_preventiva_lineas(supabase_url, srk, lineas_nuevas)
    if actualizaciones_cierre:
        batch_size = 500
        for i in range(0, len(actualizaciones_cierre), batch_size):
            upsert_cartera_preventiva(supabase_url, srk, actualizaciones_cierre[i:i + batch_size])
    if nuevas_asociaciones:
        upsert_pago_asociaciones(supabase_url, srk, nuevas_asociaciones)

    log.info('cruzar_cartera_preventiva.py: %d cuota(s) cerradas, %d línea(s) de saldo nuevas, '
              '%d asociación(es) nuevas.',
              len(actualizaciones_cierre), len(lineas_nuevas), len(nuevas_asociaciones))

    # Cruce a la inversa (informativo, ver docstring del módulo).
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
