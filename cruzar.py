#!/opt/matching-test/venv/bin/python3
"""
cruzar.py — calcula el cruce de cartera sobre consolidated_transactions.

Implementado hasta ahora (columnas 10-11 del diseño de 20 columnas):
  - INCP:      identification vs cartera_inscrip.numero_id → id_inscripcion
  - CORREO(2): email vs la hoja "Ingresos PSE y PAYU" correspondiente al banco
               (BANCOLOMBIA 2576 / WOMPI / STRIPE_USA), primera coincidencia
               (replica BUSCARV de Excel: la primera fila que matchea gana).

Las columnas 12-19 (CRUCE, NOMBRE, ...) todavía no están definidas y quedan NULL.
Requiere haber corrido sync_cartera.py antes (o el mismo día) para que las tablas
mirror estén al día.

Excepciones (requieren revisión humana en financial-platform, no se resuelven
solas aquí):
  - sin_cruce:        ni INCP ni CORREO(2) encontraron resultado.
  - cruce_ambiguo:    la llave de búsqueda (identification o email) aparece en
                      la hoja de referencia con más de un valor distinto (ej.
                      una pareja que paga dos inscripciones con el mismo
                      correo). Excepción: si los valores distintos solo
                      difieren por el sufijo "PN" (mismo número, ej.
                      "3300"/"3300PN") no cuenta como ambigüedad — se
                      normaliza al valor con "PN".
  - cruce_discrepante: INCP y CORREO(2) encontraron resultado cada uno (sin
                      ambigüedad en ninguno), pero apuntan a inscripciones
                      distintas (ej. un correo familiar compartido donde el
                      documento del pagador cruza a su propia inscripción,
                      pero ese mismo correo en la hoja de Ingresos quedó
                      asociado a la inscripción de otro familiar). Antes esto
                      se guardaba en silencio como 'cruzado', usando ambos
                      valores tal cual sin ninguna señal de la discrepancia.

Sugerencia por fecha (CORREO(2) de BC2576/WOMPI/STRIPE_USA, las únicas hojas
con fecha por fila): cuando una llave sigue ambigua tras la normalización PN,
puede deberse a que la persona terminó un programa y se reinscribió a otro
(mismo correo, otro INCP). Dos señales, en orden de fuerza (ver
_sugerir_por_cadencia):
  1. Coincidencia de fecha (±3 días): el Excel de referencia a veces ya trae
     una fila para esta misma transacción (el equipo financiero la registra
     ahí aparte del pipeline automático) — si exactamente un candidato tiene
     una fecha así de cercana, es prácticamente el mismo evento.
  2. Cadencia mensual (15-60 días): si nada matchea por fecha exacta, se toma
     el candidato cuyo último pago cae ~1 mes antes, como continuación normal
     de cuotas.
Si exactamente un candidato califica por cualquiera de las dos señales, se usa
ese valor para pre-llenar CORREO(2) — pero la fila SIGUE marcada
`cruce_ambiguo`/`pendiente`, es solo una sugerencia para agilizar la revisión
manual, no una resolución automática. No se filtra por cantidad de cuotas ya
pagadas (el número de pagos por inscripción no es fijo: puede haber
renegociaciones a 5+ cuotas, o cierres anticipados en 3). cartera_inscrip
(INCP) no tiene fecha en su hoja de origen, así que esta regla no aplica a
esas ambigüedades.

Límite conocido: si una persona termina su última cuota de un programa y ese
mismo mes empieza a pagar otro programa distinto, el intervalo entre ambos
pagos es indistinguible de una cuota normal de continuación — la regla puede
sugerir el INCP viejo en ese caso. Como es solo sugerencia (no auto-resuelve),
la revisión manual en financial-platform sigue siendo quien decide.

Filas ya resueltas (estado_cruce = 'cruzado' o 'no_identificable') no se vuelven
a tocar en corridas futuras, así una corrección manual o un "no identificable"
marcado en financial-platform queda protegido.
"""

import logging
import os
import sys
from datetime import date, datetime as dt

from dotenv import load_dotenv

from utils.supabase import select_all, upsert_cruce

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%S',
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

CADENCIA_DIAS_MIN = 15
CADENCIA_DIAS_MAX = 60
COINCIDENCIA_DIAS = 3


def _normalizar_pn(valor: str) -> str:
    """Quita el sufijo "PN" (si lo tiene) para comparar el número base."""
    if valor.upper().endswith('PN'):
        return valor[:-2]
    return valor


def _parse_fecha(valor) -> date | None:
    if not valor:
        return None
    if isinstance(valor, date):
        return valor
    s = str(valor).strip()
    if not s:
        return None
    try:
        return dt.strptime(s[:10], '%Y-%m-%d').date()
    except ValueError:
        return None


def _sugerir_por_cadencia(historial_valor: dict[str, list], fecha_pago: date | None) -> str | None:
    """De los valores candidatos de una llave ambigua, sugiere cuál corresponde
    al pago que se está cruzando (ver docstring del módulo). Dos señales, en
    orden de fuerza:

    1. Coincidencia de fecha (±COINCIDENCIA_DIAS): el equipo financiero a veces
       ya registró esta misma transacción en el Excel de referencia aparte del
       pipeline automático. Si exactamente un candidato tiene una fila fechada
       cerca del pago actual, es prácticamente el mismo evento — señal fuerte.
    2. Cadencia mensual (CADENCIA_DIAS_MIN-CADENCIA_DIAS_MAX): si ninguno matchea
       por fecha exacta, se usa el candidato cuyo último pago cae ~1 mes antes,
       como continuación normal de cuotas.

    No filtra por cantidad de pagos ya registrados. Devuelve None si ninguna
    señal identifica un único candidato (sigue ambiguo)."""
    if fecha_pago is None:
        return None

    exactos = [
        valor for valor, fechas in historial_valor.items()
        if any(abs((fecha_pago - f).days) <= COINCIDENCIA_DIAS
               for f in fechas if f is not None)
    ]
    if len(exactos) == 1:
        return exactos[0]
    if len(exactos) > 1:
        return None

    candidatos = []
    for valor, fechas in historial_valor.items():
        fechas_validas = [f for f in fechas if f is not None]
        if not fechas_validas:
            continue
        dias = (fecha_pago - max(fechas_validas)).days
        if CADENCIA_DIAS_MIN <= dias <= CADENCIA_DIAS_MAX:
            candidatos.append(valor)
    return candidatos[0] if len(candidatos) == 1 else None


def _build_lookup(rows: list[dict], key_field: str, value_field: str,
                   lower: bool = False, fecha_field: str | None = None,
                   ) -> tuple[dict, set, dict]:
    """Primera coincidencia gana (replica BUSCARV de Excel).

    Devuelve además el conjunto de llaves ambiguas: aquellas donde la hoja de
    referencia trae 2+ valores distintos no vacíos para la misma llave (ej. un
    correo con dos números de inscripción diferentes). Esas NO se resuelven
    aquí, solo se señalan para revisión humana.

    Excepción confirmada por el usuario: si los valores de una llave solo
    difieren por el sufijo "PN" (mismo número, ej. "3300" y "3300PN"), no es
    una ambigüedad real — se normaliza al valor con "PN" y no se marca excepción.

    Si se pasa `fecha_field`, arma además un historial {key: {valor: [fechas]}}
    con las fechas de pago registradas por valor, usado por
    _sugerir_por_cadencia para las llaves que sigan ambiguas.
    """
    lookup: dict = {}
    valores_no_vacios: dict[str, set] = {}
    historial: dict[str, dict[str, list]] = {}
    for row in rows:
        key = str(row.get(key_field) or '').strip()
        if lower:
            key = key.lower()
        if not key:
            continue
        value = str(row.get(value_field) or '').strip()
        if key not in lookup:
            lookup[key] = value
        if value:
            valores_no_vacios.setdefault(key, set()).add(value)
            if fecha_field is not None:
                fecha = _parse_fecha(row.get(fecha_field))
                historial.setdefault(key, {}).setdefault(value, []).append(fecha)

    ambiguos = set()
    for key, valores in valores_no_vacios.items():
        if len(valores) <= 1:
            continue
        bases = {_normalizar_pn(v) for v in valores}
        if len(bases) == 1:
            lookup[key] = next(iter(bases)) + 'PN'
        else:
            ambiguos.add(key)

    return lookup, ambiguos, historial


def main():
    load_dotenv()

    supabase_url = os.environ.get('SUPABASE_URL', '')
    srk          = os.environ.get('SUPABASE_SERVICE_ROLE_KEY', '')
    if not supabase_url or not srk:
        log.error('SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY no configurados.')
        sys.exit(1)

    log.info('Cargando tablas de referencia...')
    inscrip_rows = select_all(supabase_url, srk, 'cartera_inscrip',
                               select='numero_id,id_inscripcion')
    bc2576_rows  = select_all(supabase_url, srk, 'cartera_ingresos_bancolombia_2576',
                               select='referencia_1,incp,fecha')
    wompi_rows   = select_all(supabase_url, srk, 'cartera_ingresos_wompi',
                               select='email,inscrip,fecha')
    stripe_rows  = select_all(supabase_url, srk, 'cartera_ingresos_stripe_usa',
                               select='email_cliente,incp,fecha')

    lookup_inscrip, ambiguos_inscrip, _                   = _build_lookup(inscrip_rows, 'numero_id', 'id_inscripcion')
    lookup_bc2576, ambiguos_bc2576, historial_bc2576      = _build_lookup(
        bc2576_rows, 'referencia_1', 'incp', fecha_field='fecha')
    lookup_wompi, ambiguos_wompi, historial_wompi         = _build_lookup(
        wompi_rows, 'email', 'inscrip', lower=True, fecha_field='fecha')
    lookup_stripe, ambiguos_stripe, historial_stripe      = _build_lookup(
        stripe_rows, 'email_cliente', 'incp', lower=True, fecha_field='fecha')

    log.info('Referencias cargadas: inscrip=%d, bc2576=%d, wompi=%d, stripe=%d',
              len(lookup_inscrip), len(lookup_bc2576), len(lookup_wompi), len(lookup_stripe))

    log.info('Cargando estado_cruce existente...')
    existentes = select_all(supabase_url, srk, 'cruce_cartera', select='matching_key,estado_cruce')
    llaves_terminadas = {
        r['matching_key'] for r in existentes
        if r.get('estado_cruce') in ('cruzado', 'no_identificable')
    }
    log.info('%d filas ya resueltas (cruzado/no_identificable), se saltan.', len(llaves_terminadas))

    log.info('Cargando consolidated_transactions...')
    transacciones = select_all(
        supabase_url, srk, 'consolidated_transactions',
        select='identification,payment_date,transaction_code_1,transaction_code_2,'
               'email,payment_method,program,phone,payment_amount,matching_key',
    )
    transacciones = [t for t in transacciones if t.get('matching_key') not in llaves_terminadas]
    log.info('%d transacciones a cruzar.', len(transacciones))

    resultado = []
    for t in transacciones:
        identification = str(t.get('identification') or '').strip()
        email          = str(t.get('email') or '').strip()
        email_lower    = email.lower()
        payment_method = str(t.get('payment_method') or '').upper()

        incp         = lookup_inscrip.get(identification, '')
        incp_ambiguo = identification in ambiguos_inscrip

        correo_2            = ''
        correo_2_ambiguo    = False
        correo_2_historial  = {}
        if payment_method == 'BANCOLOMBIA':
            correo_2           = lookup_bc2576.get(email, '')
            correo_2_ambiguo   = email in ambiguos_bc2576
            correo_2_historial = historial_bc2576.get(email, {})
        elif payment_method.startswith('WOMPI'):
            correo_2           = lookup_wompi.get(email_lower, '')
            correo_2_ambiguo   = email_lower in ambiguos_wompi
            correo_2_historial = historial_wompi.get(email_lower, {})
        elif payment_method == 'STRIPE_USA':
            correo_2           = lookup_stripe.get(email_lower, '')
            correo_2_ambiguo   = email_lower in ambiguos_stripe
            correo_2_historial = historial_stripe.get(email_lower, {})

        if correo_2_ambiguo:
            sugerido = _sugerir_por_cadencia(correo_2_historial, _parse_fecha(t.get('payment_date')))
            if sugerido:
                correo_2 = sugerido

        if incp_ambiguo or correo_2_ambiguo:
            excepcion_motivo, estado_cruce = 'cruce_ambiguo', 'pendiente'
        elif incp and correo_2 and _normalizar_pn(incp) != _normalizar_pn(correo_2):
            excepcion_motivo, estado_cruce = 'cruce_discrepante', 'pendiente'
        elif not incp and not correo_2:
            excepcion_motivo, estado_cruce = 'sin_cruce', 'pendiente'
        else:
            excepcion_motivo, estado_cruce = None, 'cruzado'

        resultado.append({
            'matching_key':       t.get('matching_key'),
            'identification':     t.get('identification'),
            'payment_date':       t.get('payment_date'),
            'transaction_code_1': t.get('transaction_code_1'),
            'transaction_code_2': t.get('transaction_code_2'),
            'email':              t.get('email'),
            'payment_method':     t.get('payment_method'),
            'program':            t.get('program'),
            'phone':              t.get('phone'),
            'payment_amount':     t.get('payment_amount'),
            'incp':               incp or None,
            'correo_2':           correo_2 or None,
            'excepcion_motivo':   excepcion_motivo,
            'estado_cruce':       estado_cruce,
        })

    if not resultado:
        log.info('Sin transacciones para cruzar.')
        return

    batch_size = 500
    for i in range(0, len(resultado), batch_size):
        upsert_cruce(supabase_url, srk, resultado[i:i + batch_size])

    cruzados    = sum(1 for r in resultado if r['estado_cruce'] == 'cruzado')
    sin_cruce   = sum(1 for r in resultado if r['excepcion_motivo'] == 'sin_cruce')
    ambiguos    = sum(1 for r in resultado if r['excepcion_motivo'] == 'cruce_ambiguo')
    log.info('cruzar.py completado: %d filas | cruzadas=%d | sin_cruce=%d | cruce_ambiguo=%d',
              len(resultado), cruzados, sin_cruce, ambiguos)


if __name__ == '__main__':
    main()
