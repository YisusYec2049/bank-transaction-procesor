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
`cruzado`, confirma si tiene al menos una asociación real en
`pago_asociaciones` (vigente, no huérfana) y, si la tiene, trae el cliente
de esa cuota. Se recalcula sobre TODOS los pagos cruzados en cada corrida.
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
- **Falta `>= $50.000`**: la cuota cierra con lo recibido (`diferencia=0`) y
  se crea una CUOTA NUEVA (mismo patrón de llave que antes,
  `_generar_llave_saldo`) con `notificacion='FALTA DE PAGO'` por el faltante.
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

`_fila_cierre` sigue devolviendo el MISMO set de claves siempre en cada POST
(bug crítico PGRST102 del 16/07 — leer su docstring). El orden de escritura
(`pago_asociaciones` primero) también se conserva por la misma razón.
────────────────────────────────────────────────────────────────────────────
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
                             upsert_pago_asociaciones, upsert_cartera_saldos_favor)

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
    Cae a `valor_cuota` solo si `valor_a_cobrar` viene NULL."""
    valor = cuota.get('valor_a_cobrar')
    if valor is None:
        valor = cuota.get('valor_cuota')
    return float(valor or 0)


def _es_wompi_automatico(pago: dict) -> bool:
    payment_method = str(pago.get('payment_method') or '').upper()
    metodo = str(pago.get('metodo_de_pago') or '')
    return payment_method.startswith('WOMPI') and bool(metodo) and metodo != PAGOS_MANUALES_LABEL


def _correo_elec_para(pago: dict) -> str:
    if _es_wompi_automatico(pago):
        return WOMPI_LINK_LABEL
    return pago.get('email') or ''


def _generar_llave_saldo(llave_base: str, matching_key: str) -> str:
    """Llave DETERMINÍSTICA para la cuota nueva de "FALTA DE PAGO" (o del
    saldo pendiente de un pago parcial), derivada del `matching_key` del pago
    que la origina — no de "buscar el primer sufijo libre" (bug crítico
    corregido el 16 de julio: esa búsqueda nunca era estable entre corridas,
    ver git history). Reprocesar el MISMO pago siempre calcula la MISMA
    llave, y el upsert por `llave` en insert_cartera_preventiva_lineas
    actualiza esa fila en vez de crear una nueva."""
    return f'{llave_base} (saldo {matching_key})'


def _fila_reset(cuota_id) -> dict:
    """Dict de reset a NULL de las columnas de resultado del cruce para una
    cuota — usado tanto por el reset de asociaciones huérfanas como por el
    reset de cuotas que perdieron todas sus asociaciones por un descarte, o
    que se reabren al apagar `cerrado_manual`.

    ADVERTENCIA (bug PGRST102, ver `_fila_cierre`): este dict tiene un set de
    claves DISTINTO al de `_fila_cierre` (no incluye `valor_cuota`/
    `valor_a_cobrar`). Nunca mezclar filas de `_fila_reset` en el mismo array
    que filas de `_fila_cierre` en una misma llamada a
    `upsert_cartera_preventiva` — escribirlas en una lista/POST aparte."""
    return {'id': cuota_id, 'fecha_pago': None, 'medio_pago': None, 'valor_pago': None,
            'codigo_transaccion_1': None, 'codigo_transaccion_2': None, 'correo_elec': None,
            'diferencia': None, 'fecha_cruce': None, 'notificacion': None,
            'es_wompi_automatico': None}


def _fila_cierre_cartera(cuota: dict, fecha_pago_manual: str, hoy: str) -> dict:
    """Cierre MANUAL de una cuota puntual (regla #8, "Cerrar cartera") — la
    persona la marca desde financial-platform y elige la fecha. No viene de
    un pago real: `valor_pago` = `valor_a_cobrar` (el saldo pendiente actual
    de la cuota, confirmado por el usuario — no `valor_cuota`), `medio_pago`
    literal `'Cartera'`, `notificacion='CARTERA'`. Mismo set de claves que
    `_fila_cierre` (12), para poder mezclarse en el mismo array/POST sin
    disparar PGRST102."""
    valor_a_cobrar = cuota.get('valor_a_cobrar')
    return {
        'id':                   cuota['id'],
        'fecha_pago':           fecha_pago_manual,
        'medio_pago':           'Cartera',
        'valor_pago':           valor_a_cobrar,
        'codigo_transaccion_1': None,
        'codigo_transaccion_2': None,
        'correo_elec':          None,
        'fecha_cruce':          hoy,
        'notificacion':         'CARTERA',
        'diferencia':           0,
        'valor_cuota':          cuota.get('valor_cuota'),
        'valor_a_cobrar':       valor_a_cobrar,
        'es_wompi_automatico':  None,
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
      - excedente: plata que sobró tras cubrir TODAS las cuotas conocidas
        de la inscripción (saldo a favor, ver §2 de la spec)."""
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
            excedente += restante

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

    return cierres, parcial, asociaciones, excedente


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
    }
    if cerrar_al_monto_recibido:
        # La cuota ORIGINAL de un faltante >= $50.000 se ajusta a lo
        # realmente recibido (no al monto original) y queda cerrada con
        # diferencia=0 — el faltante real pasa a la cuota nueva "FALTA DE
        # PAGO".
        fila['valor_cuota']    = monto
        fila['valor_a_cobrar'] = monto
        fila['diferencia']     = 0
    else:
        fila['diferencia']     = round(monto - _saldo_a_cobrar(cuota), 2)
        fila['valor_cuota']    = cuota.get('valor_cuota')
        fila['valor_a_cobrar'] = cuota.get('valor_a_cobrar')
    return fila


def _fila_linea_saldo(parcial: dict, nueva_llave: str, notificacion: str | None = None) -> dict:
    cuota = parcial['cuota']
    saldo = round(_saldo_a_cobrar(cuota) - parcial['monto_aplicado'], 2)
    return {
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
    }


def _cerrar_o_faltante(info: dict, hoy: str) -> tuple[dict, dict | None]:
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
        fila = _fila_cierre(info, hoy, cerrar_al_monto_recibido=True)
        nueva_llave = _generar_llave_saldo(cuota['llave'], info['ultimo_pago']['matching_key'])
        linea_nueva = _fila_linea_saldo(info, nueva_llave, notificacion='FALTA DE PAGO')
        return fila, linea_nueva
    return _fila_cierre(info, hoy, cerrar_al_monto_recibido=False), None


def _procesar_inscripcion(cuotas_inscripcion: list[dict], pagos_para: list[dict], hoy: str):
    """Corre el FIFO de UNA inscripción y arma las filas de escritura según
    el modelo "Saldo a Favor Manual + FALTA DE PAGO" (21 de julio).

    No escribe nada en Supabase — solo calcula. Devuelve:
      (cierres_filas, linea_nueva_o_None, asociaciones, saldo_favor_nuevo_o_None)
    """
    cierres, parcial, asociaciones, excedente = _aplicar_pagos_inscripcion(cuotas_inscripcion, pagos_para)

    cierres_filas = [_fila_cierre(info, hoy) for info in cierres]
    linea_nueva = None
    if parcial:
        fila, linea_nueva = _cerrar_o_faltante(parcial, hoy)
        cierres_filas.append(fila)

    saldo_favor_nuevo = None
    if excedente > 0 and cierres_filas:
        # El excedente lo absorbe la ÚLTIMA cuota que toca el pago (nunca la
        # parcial: excedente y parcial son mutuamente excluyentes dentro de
        # una misma llamada — si sobra plata es porque TODAS las cuotas ya
        # se cerraron). Queda como `diferencia` POSITIVA informativa — NUNCA
        # se suma a `valor_pago` ni se auto-aplica a otra cuota.
        ultima_cuota = cierres[-1]['cuota']
        ultimo_pago = cierres[-1]['ultimo_pago']
        cierres_filas[-1]['diferencia'] = round(excedente, 2)
        saldo_favor_nuevo = {
            'inscrip':      ultima_cuota.get('inscrip'),
            'cliente':      ultima_cuota.get('cliente'),
            'documento':    _normalizar_documento(ultima_cuota.get('cruce_access')),
            'correo':       _normalizar_correo(ultima_cuota.get('correo')),
            'monto':        round(excedente, 2),
            'disponible':   round(excedente, 2),
            'origen':       'sobrante',
            'llave_origen': ultima_cuota['llave'],
            'matching_key': ultimo_pago['matching_key'],
            'fecha':        ultimo_pago.get('payment_date'),
            'aplicado':     False,
        }

    return cierres_filas, linea_nueva, asociaciones, saldo_favor_nuevo


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
                                 select='llave,cerrado_manual,fecha_pago_manual')
    llaves_cerradas_manual = {r['llave'] for r in overrides_rows if r.get('cerrado_manual')}
    cerrados_manual_por_llave = {r['llave']: r for r in overrides_rows if r.get('cerrado_manual')}

    asociaciones_rows = select_all(supabase_url, srk, 'pago_asociaciones',
                                    select='id,matching_key,llave,monto,origen')

    log.info('Cargando cartera_preventiva...')
    cuotas_rows = select_all(
        supabase_url, srk, 'cartera_preventiva',
        select='id,llave,cruce_access,correo,fecha_vencimiento,valor_cuota,valor_a_cobrar,inscrip,'
               'cliente,sistema_financiero,moneda,programa,fecha_pago,valor_pago,fecha_cruce,'
               'diferencia,notificacion',
    )
    id_por_llave      = {c['llave']: c['id'] for c in cuotas_rows if c.get('llave')}
    llave_por_id      = {c['id']: c['llave'] for c in cuotas_rows}
    cliente_por_llave = {c['llave']: c.get('cliente') for c in cuotas_rows if c.get('llave')}

    # Regla #8 — "Cerrar cartera": cierre MANUAL de una cuota puntual, fuera
    # del proceso (fin-platform escribe cerrado_manual=true +
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
            valor_a_cobrar = cuota.get('valor_a_cobrar')
            ya_reflejado = (
                cuota.get('notificacion') == 'CARTERA'
                and cuota.get('fecha_pago') == fecha_manual
                and cuota.get('valor_pago') is not None
                and valor_a_cobrar is not None
                and round(float(cuota['valor_pago']), 2) == round(float(valor_a_cobrar), 2)
            )
            if ya_reflejado:
                continue
            fila = _fila_cierre_cartera(cuota, fecha_manual, hoy)
            cierres_cartera.append(fila)
            cuota['fecha_pago']    = fecha_manual
            cuota['valor_pago']    = valor_a_cobrar
            cuota['diferencia']    = 0
            cuota['notificacion']  = 'CARTERA'
            cuota['fecha_cruce']   = hoy
            cerrados_cartera += 1
        elif cuota.get('notificacion') == 'CARTERA':
            # cerrado_manual se apagó (reabrir) o nunca tuvo fecha puesta —
            # sin fecha no se cierra (P1), así que si quedó marcada CARTERA
            # de una corrida anterior, se resetea a pendiente.
            resets_varios.append(_fila_reset(cuota['id']))
            cuota['fecha_pago']   = None
            cuota['valor_pago']   = None
            cuota['diferencia']   = None
            cuota['fecha_cruce']  = None
            cuota['notificacion'] = None
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
        reset_rows = [_fila_reset(id_por_llave[llave]) for llave in llaves_a_resetear if llave in id_por_llave]
        upsert_cartera_preventiva(supabase_url, srk, reset_rows)
        for c in cuotas_rows:
            if c.get('llave') in llaves_a_resetear:
                c['fecha_pago'] = None
                c['valor_pago'] = None
                c['diferencia'] = None
                c['fecha_cruce'] = None
                c['notificacion'] = None
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

    # §3.5 — Pase de reconciliación manual: toda cuota con al menos una
    # asociación origen='manual' se recalcula a partir de la SUMA de TODAS
    # sus asociaciones vigentes (mismo umbral que el FIFO automático).
    log.info('Reconciliando asociaciones manuales...')
    llaves_con_asoc_manual = {a['llave'] for a in asociaciones_vigentes if a.get('origen') == 'manual'}
    reconciliadas = 0
    for llave in llaves_con_asoc_manual:
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
        valor_pago_actual = cuota.get('valor_pago')
        if (cuota.get('fecha_cruce') and valor_pago_actual is not None
                and round(float(valor_pago_actual), 2) == suma):
            continue  # ya refleja esta suma, nada que hacer (idempotencia)

        ultimo_pago = _pago_mas_reciente(asociaciones_cuota, pagos_por_matching_key)
        info = {'cuota': cuota, 'monto_aplicado': suma, 'ultimo_pago': ultimo_pago}
        fila, linea = _cerrar_o_faltante(info, hoy)
        actualizaciones_cierre.append(fila)
        if linea:
            lineas_nuevas.append(linea)
        cuota['valor_pago'] = suma
        cuota['fecha_pago'] = fila.get('fecha_pago')
        cuota['fecha_cruce'] = hoy
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
        if llave in asociaciones_por_llave:
            continue  # todavía tiene asociación(es) vigente(s)
        resets_varios.append(_fila_reset(cuota['id']))
        cuota['fecha_pago'] = None
        cuota['valor_pago'] = None
        cuota['diferencia'] = None
        cuota['fecha_cruce'] = None
        cuota['notificacion'] = None
        reseteadas_descarte += 1

    if reconciliadas or reseteadas_descarte:
        log.info('%d cuota(s) reconciliadas por asociación manual, %d reseteada(s) por descarte.',
                  reconciliadas, reseteadas_descarte)

    # §3.6: pagos_nuevos = cruzados con INCP, sin ninguna asociación vigente
    # (auto o manual) NI ninguna fila en el ledger (sobrante o descarte) con
    # ese matching_key — esto último es lo que impide que un pago descartado
    # se vuelva a cruzar en el próximo cron.
    matching_keys_asociados = {a['matching_key'] for a in asociaciones_vigentes}
    matching_keys_en_ledger = {c['matching_key'] for c in saldos_favor_rows if c.get('matching_key')}
    pagos_nuevos = [
        p for p in pagos_cruzados
        if p['matching_key'] not in matching_keys_asociados
        and p['matching_key'] not in matching_keys_en_ledger
    ]
    log.info('%d pagos cruzados totales, %d nuevos (sin asociación/ledger previo).',
              len(pagos_cruzados), len(pagos_nuevos))

    # §4.1/4.2: cuotas pendientes = sin pago identificado, sin cierre manual
    # y con saldo real por cobrar.
    #
    # `_saldo_a_cobrar(c) > 0` saca las cuotas que el proceso manual ya
    # dejó en cero (`valor_a_cobrar` = 0 porque `PAGO` cubrió la cuota
    # entera). Sin este filtro entran a la cascada por su `valor_cuota`
    # completo y se comen plata que le tocaba a la cuota siguiente —
    # medido el 21 de julio: 899 de 2.628 cuotas estaban en esa situación.
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

        cierres_filas, linea_nueva, asociaciones, saldo_favor = _procesar_inscripcion(
            cuotas_inscripcion, pagos_para_inscripcion, hoy)

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
    if lineas_nuevas:
        insert_cartera_preventiva_lineas(supabase_url, srk, lineas_nuevas)
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

    # §3.3.2 (P3): sincronizar la diferencia positiva de la cuota ORIGEN de
    # cada saldo a favor tipo 'sobrante' con su `disponible` restante.
    # fin-platform decrementa `disponible` al asociar; acá solo se refleja,
    # nunca se recalcula. Va DESPUÉS de los cierres (no depende de su orden,
    # es idempotente y de solo lectura sobre el ledger cargado al inicio).
    sync_diferencia = []
    for cr in saldos_favor_rows:
        if cr.get('origen') != 'sobrante' or not cr.get('llave_origen'):
            continue
        cuota_id = id_por_llave.get(cr['llave_origen'])
        if cuota_id is None:
            continue
        disponible = round(float(cr.get('disponible') or 0), 2)
        cuota_origen = next((c for c in cuotas_rows if c['id'] == cuota_id), None)
        actual = cuota_origen.get('diferencia') if cuota_origen else None
        if actual is not None and round(float(actual), 2) == disponible:
            continue
        sync_diferencia.append({'id': cuota_id, 'diferencia': disponible})
    if sync_diferencia:
        upsert_cartera_preventiva(supabase_url, srk, sync_diferencia)
        log.info('Saldo a favor: %d cuota(s) origen sincronizadas con su disponible restante.',
                  len(sync_diferencia))

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
