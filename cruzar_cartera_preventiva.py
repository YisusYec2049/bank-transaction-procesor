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
"""

import logging
import os
import sys

from dotenv import load_dotenv

from utils.parser import normalizar_nit
from utils.supabase import select_all, upsert_cartera_preventiva

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


def _asignar_pagos(cuotas: list[dict], pagos: list[dict]) -> list[dict]:
    """FIFO: cuotas por fecha_vencimiento asc, pagos por payment_date asc.
    Devuelve una fila de resultado por cada cuota que recibió al menos un
    pago (ver docstring del módulo para el caso de múltiples pagos)."""
    cuotas_ordenadas = sorted(cuotas, key=lambda c: c.get('fecha_vencimiento') or _FECHA_MAX)
    pagos_ordenados   = sorted(pagos, key=lambda p: p.get('payment_date') or _FECHA_MAX)

    acumulado:  dict = {}  # id de cuota -> monto aplicado
    ultimo_pago: dict = {}  # id de cuota -> último pago que la tocó
    idx = 0
    for pago in pagos_ordenados:
        restante = float(pago.get('payment_amount') or 0)
        if restante <= 0:
            continue
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
            restante -= aplicar
            if acumulado[cuota_id] >= valor_cuota:
                idx += 1
        # si restante > 0 tras recorrer todas las cuotas pendientes conocidas,
        # el excedente no se asigna a nada (no hay más deuda registrada).

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
    return resultado


def main():
    load_dotenv()

    supabase_url = os.environ.get('SUPABASE_URL', '')
    srk          = os.environ.get('SUPABASE_SERVICE_ROLE_KEY', '')
    if not supabase_url or not srk:
        log.error('SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY no configurados.')
        sys.exit(1)

    log.info('Cargando cartera_preventiva...')
    cuotas_rows = select_all(supabase_url, srk, 'cartera_preventiva',
                              select='id,cruce_access,fecha_vencimiento,valor_cuota')

    log.info('Cargando cruce_cartera (solo estado_cruce=cruzado)...')
    pagos_rows = select_all(
        supabase_url, srk, 'cruce_cartera',
        select='identification,payment_date,transaction_code_1,transaction_code_2,'
               'email,payment_method,payment_amount,estado_cruce',
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

    resultado = []
    for doc, cuotas in cuotas_por_doc.items():
        pagos = pagos_por_doc.get(doc)
        if not pagos:
            continue
        resultado.extend(_asignar_pagos(cuotas, pagos))

    if not resultado:
        log.info('Sin cuotas para actualizar.')
        return

    batch_size = 500
    for i in range(0, len(resultado), batch_size):
        upsert_cartera_preventiva(supabase_url, srk, resultado[i:i + batch_size])

    log.info('cruzar_cartera_preventiva.py completado: %d cuotas actualizadas.', len(resultado))


if __name__ == '__main__':
    main()
