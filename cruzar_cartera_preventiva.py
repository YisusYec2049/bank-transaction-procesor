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

Antes (hasta el 13 de julio) leía directo de consolidated_transactions, sin
pasar por ninguna validación de identidad — un pago con identification
coincidente por casualidad (ej. un NIT de intermediario, o una extracción
incorrecta) podía generar un cruce falso en cartera_preventiva. Ahora solo se
consideran pagos que cruzar.py ya confirmó como identificados correctamente.
Depende de que cruzar.py haya corrido antes en el mismo ciclo — el cron del
VPS ya lo garantiza (cruzar_cartera_preventiva.py corre 1 minuto después de
sync_cartera.py && cruzar.py, ver cron del 14 de julio).

Recalcula TODO desde cero en cada corrida (no incremental): no protege
ediciones manuales porque todavía no hay ninguna sobre este reporte.

Lógica (confirmada por el usuario el 13 de julio):
  - Llave de búsqueda: cartera_preventiva.cruce_access (documento del deudor,
    normalizado con normalizar_nit para quitar el dígito de verificación de
    NIT, ej. "900497967-4" -> "900497967") contra
    cruce_cartera.identification (nunca trae el DV).
  - Para cada documento, las cuotas pendientes se ordenan por
    fecha_vencimiento ascendente y los pagos por payment_date ascendente.
    Cada pago se aplica en cascada FIFO: primero a la cuota más antigua no
    cubierta todavía; si sobra, el resto pasa a la siguiente cuota más
    antigua; si no alcanza a cubrirla, la cuota queda con diferencia
    negativa (saldo pendiente) y el siguiente pago (si lo hay) sigue
    completándola antes de avanzar. Esto ya cubre, sin lógica aparte:
      - pago parcial (nota 7 de REGLAS CRUCE CON CARTERA.md)
      - un pago cubre 2+ cuotas de una vez (confirmado)
      - una cuota se termina de pagar con una segunda transacción separada,
        ambas quedan "contra" esa misma cuota (nota 10)
  - Si una cuota recibe más de un pago, los campos descriptivos (fecha_pago,
    medio_pago, codigo_transaccion_1/2, correo_elec) reflejan el ÚLTIMO pago
    que la tocó (el que la completa o el más reciente si sigue parcial);
    valor_pago acumula la suma de todos los pagos aplicados a esa cuota. Es
    una interpretación razonable a falta de que el Excel original tenga
    espacio para más de una transacción por fila — pendiente de confirmar
    con el usuario si necesita otra representación.

Pendiente, explícitamente diferido por el usuario: qué hacer cuando un pago
cierra TODAS las cuotas pendientes de una inscripción de una sola vez (más
allá de la cascada aritmética ya implementada) — no se agrega nada especial
todavía.

CRUCE (columna cruce_cartera.cruce) — "cruce a la inversa":
además de la cascada de arriba (que resuelve cartera_preventiva), este script
rellena cruce_cartera.cruce para las filas 'cruzado'.

CORREGIDO (15 de julio): antes esto se calculaba ANTES de la cascada y solo
miraba si el `incp` del pago existía en algún lado de cartera_preventiva.inscrip
— sin importar si ese pago específico se había aplicado realmente a una cuota.
Eso hacía que un pago que quedó como puro excedente ("SOBRANTE", ver
_asignar_pagos) igual saliera con `cruce` lleno, solo porque su documento
tenía OTRAS cuotas en Cartera Preventiva. El usuario aclaró el orden correcto:
primero se corre la cascada contra Cartera Preventiva, y solo con ese
resultado (qué pago se aplicó de verdad a qué cuota, identificado vs no
identificado) se calcula `cruce`. Un pago que no se aplicó a ninguna cuota
(excedente, o documento sin cuotas en Cartera Preventiva) queda con `cruce`
NULL, aunque su documento sí tenga otras cuotas ahí.

`cruce` sigue sin distinguir pago manual/automático (pendiente que el usuario
explique esa distinción) y sigue siendo puramente informativo (no crea ni
modifica `estado_cruce`/`excepcion_motivo` de cruce_cartera). Detectar los
casos "no identificado" es responsabilidad de un filtro en financial-platform
(cruzado + incp presente + cruce vacío) — con este fix, ese filtro pasa a ser
el panel real de "excepciones de cruce con Cartera Preventiva": pagos que sí
se identificaron en cruce_cartera pero no se pudieron aplicar a ninguna cuota
real.
"""

import logging
import os
import sys

from dotenv import load_dotenv

from utils.parser import normalizar_nit
from utils.supabase import select_all, update_cruce_valores, upsert_cartera_preventiva

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%S',
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

_FECHA_MAX = '9999-12-31'


def _normalizar_documento(valor) -> str:
    return normalizar_nit(str(valor or '').strip())


def _asignar_pagos(cuotas: list[dict], pagos: list[dict]) -> tuple[list[dict], dict[str, float]]:
    """FIFO: cuotas por fecha_vencimiento asc, pagos por payment_date asc.
    Devuelve (resultado, aplicado_por_pago):
      - resultado: una fila por cada cuota que recibió al menos un pago (ver
        docstring del módulo para el caso de múltiples pagos).
      - aplicado_por_pago: {matching_key del pago -> monto que se alcanzó a
        aplicar a alguna cuota}. Un pago que cae completo en excedente (no
        queda ninguna cuota a la cual aplicarlo) no aparece aquí — sirve para
        que _calcular_cruce_inverso sepa qué pagos quedaron "identificados"
        contra Cartera Preventiva de verdad, no solo por existir el documento."""
    cuotas_ordenadas = sorted(cuotas, key=lambda c: c.get('fecha_vencimiento') or _FECHA_MAX)
    pagos_ordenados   = sorted(pagos, key=lambda p: p.get('payment_date') or _FECHA_MAX)

    acumulado:  dict = {}  # id de cuota -> monto aplicado
    ultimo_pago: dict = {}  # id de cuota -> último pago que la tocó
    aplicado_por_pago: dict[str, float] = {}  # matching_key del pago -> monto aplicado a alguna cuota
    idx = 0
    excedente = 0.0  # plata que sobra tras cubrir TODAS las cuotas conocidas del documento
    for pago in pagos_ordenados:
        restante = float(pago.get('payment_amount') or 0)
        if restante <= 0:
            continue
        matching_key = pago['matching_key']
        while restante > 0 and idx < len(cuotas_ordenadas):
            cuota = cuotas_ordenadas[idx]
            cuota_id = cuota['id']
            valor_cuota = float(cuota.get('valor_cuota') or 0)
            ya_aplicado = acumulado.get(cuota_id, 0.0)
            saldo = valor_cuota - ya_aplicado
            if saldo <= 0:
                idx += 1
                continue
            aplicar = min(restante, saldo)
            acumulado[cuota_id] = ya_aplicado + aplicar
            ultimo_pago[cuota_id] = pago
            aplicado_por_pago[matching_key] = aplicado_por_pago.get(matching_key, 0.0) + aplicar
            restante -= aplicar
            if acumulado[cuota_id] >= valor_cuota:
                idx += 1
        if restante > 0:
            # no quedan cuotas conocidas para este documento — en vez de
            # perder este monto en silencio, se acumula y se deja señalado
            # en correo_elec de la última cuota cubierta (ver más abajo).
            excedente += restante

    resultado = []
    cuotas_por_id = {c['id']: c for c in cuotas_ordenadas}
    for cuota_id, monto in acumulado.items():
        pago = ultimo_pago[cuota_id]
        valor_cuota = float(cuotas_por_id[cuota_id].get('valor_cuota') or 0)
        resultado.append({
            'id':                   cuota_id,
            'fecha_pago':           pago.get('payment_date'),
            'medio_pago':           pago.get('payment_method'),
            'valor_pago':           round(monto, 2),
            'codigo_transaccion_1': pago.get('transaction_code_1'),
            'codigo_transaccion_2': pago.get('transaction_code_2'),
            'correo_elec':          pago.get('email'),
            'diferencia':           round(monto - valor_cuota, 2),
        })

    if excedente > 0 and resultado:
        # La última cuota en `resultado` es, por construcción del bucle FIFO
        # de arriba, la última que se alcanzó a cubrir antes de quedarse sin
        # cuotas — el lugar natural para señalar el sobrante de ese documento.
        monto_fmt = f'{excedente:,.0f}'.replace(',', '.')
        ultima = resultado[-1]
        correo_original = ultima['correo_elec'] or ''
        ultima['correo_elec'] = f'{correo_original} | SOBRANTE ${monto_fmt} sin cuota registrada'.strip(' |')

    return resultado, aplicado_por_pago


def _calcular_cruce_inverso(pagos_rows: list[dict], cuotas_por_doc: dict[str, list[dict]],
                             aplicado_por_pago: dict[str, float]) -> list[dict]:
    """Cruce a la inversa: para cada pago ya 'cruzado', confirma si REALMENTE
    se aplicó a alguna cuota de Cartera Preventiva durante la cascada FIFO
    (no si su documento simplemente tiene cuotas ahí). Devuelve una lista de
    {matching_key, cruce} lista para actualizar en cruce_cartera (cruce=None
    cuando no se aplicó nada, para limpiar corridas anteriores) — para TODOS
    los pagos 'cruzado', se hayan aplicado o no."""
    updates = []
    for p in pagos_rows:
        matching_key = p['matching_key']
        doc = str(p.get('identification') or '').strip()
        cuotas = cuotas_por_doc.get(doc)
        cruce = cuotas[0].get('cliente') if cuotas and aplicado_por_pago.get(matching_key, 0.0) > 0 else None
        updates.append({'matching_key': matching_key, 'cruce': cruce})
    return updates


def main():
    load_dotenv()

    supabase_url = os.environ.get('SUPABASE_URL', '')
    srk          = os.environ.get('SUPABASE_SERVICE_ROLE_KEY', '')
    if not supabase_url or not srk:
        log.error('SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY no configurados.')
        sys.exit(1)

    log.info('Cargando cartera_preventiva...')
    cuotas_rows = select_all(supabase_url, srk, 'cartera_preventiva',
                              select='id,cruce_access,fecha_vencimiento,valor_cuota,inscrip,cliente')

    log.info('Cargando cruce_cartera (solo estado_cruce=cruzado)...')
    pagos_rows = select_all(
        supabase_url, srk, 'cruce_cartera',
        select='matching_key,identification,payment_date,transaction_code_1,transaction_code_2,'
               'email,payment_method,payment_amount,estado_cruce,incp',
    )
    pagos_rows = [p for p in pagos_rows if p.get('estado_cruce') == 'cruzado']

    cuotas_por_doc: dict[str, list[dict]] = {}
    for c in cuotas_rows:
        doc = _normalizar_documento(c.get('cruce_access'))
        if not doc:
            continue
        cuotas_por_doc.setdefault(doc, []).append(c)

    pagos_por_doc: dict[str, list[dict]] = {}
    for p in pagos_rows:
        doc = str(p.get('identification') or '').strip()
        if not doc or not p.get('payment_amount'):
            continue
        pagos_por_doc.setdefault(doc, []).append(p)

    log.info('%d documentos con cuotas pendientes, %d documentos con pagos.',
              len(cuotas_por_doc), len(pagos_por_doc))

    log.info('Calculando cruce contra Cartera Preventiva (cascada FIFO)...')
    resultado = []
    aplicado_por_pago: dict[str, float] = {}
    for doc, cuotas in cuotas_por_doc.items():
        pagos = pagos_por_doc.get(doc)
        if not pagos:
            continue
        cuotas_resultado, aplicado = _asignar_pagos(cuotas, pagos)
        resultado.extend(cuotas_resultado)
        aplicado_por_pago.update(aplicado)

    if resultado:
        batch_size = 500
        for i in range(0, len(resultado), batch_size):
            upsert_cartera_preventiva(supabase_url, srk, resultado[i:i + batch_size])
        log.info('cruzar_cartera_preventiva.py: %d cuotas actualizadas.', len(resultado))
    else:
        log.info('Sin cuotas para actualizar.')

    log.info('Calculando cruce a la inversa (con base en lo que realmente se aplicó arriba)...')
    cruce_updates = _calcular_cruce_inverso(pagos_rows, cuotas_por_doc, aplicado_por_pago)
    if cruce_updates:
        con_match = sum(1 for u in cruce_updates if u.get('cruce'))
        update_cruce_valores(supabase_url, srk, cruce_updates)
        log.info('Cruce inverso: %d filas actualizadas (%d identificados, %d sin identificar).',
                  len(cruce_updates), con_match, len(cruce_updates) - con_match)


if __name__ == '__main__':
    main()
