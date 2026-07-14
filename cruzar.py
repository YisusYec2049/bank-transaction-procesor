#!/opt/matching-test/venv/bin/python3
"""
cruzar.py — calcula el cruce de cartera sobre consolidated_transactions.

Implementado hasta ahora (columnas 10-11 del diseño de 20 columnas):
  - INCP:      identification vs cartera_inscrip.numero_id → id_inscripcion.
               numero_id se normaliza quitando el dígito de verificación de
               NIT (ej. "860004922-4" -> "860004922") antes de indexar, ya
               que identification nunca lo trae — sin esto ningún pago hecho
               por una empresa (Persona Jurídica) cruzaba (ver _normalizar_nit).
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
                      difieren por un sufijo ignorable ("PN" o "PJ", mismo
                      número, ej. "3300"/"3300PN") no cuenta como ambigüedad
                      — se normaliza al valor con el sufijo. Para INCP
                      además: si los números base son genuinamente distintos
                      pero exactamente uno de los valores lleva sufijo
                      financiero (PN/PJ) y el resto no lleva ninguno (ej.
                      pregrado antiguo en Access + diplomado nuevo ya en el
                      sistema financiero), se toma el que tiene sufijo (ver
                      `preferir_sufijo_financiero` en _build_lookup). Si
                      sigue ambiguo (2+ con sufijo), se puede resolver
                      permanentemente desde financial-platform descartando
                      un id_inscripcion para ese documento — ver tabla
                      `cruce_incp_exclusiones` (sql/004), que este script lee
                      y filtra antes de construir el lookup de INCP.
  - cruce_discrepante: INCP y CORREO(2) encontraron resultado cada uno (sin
                      ambigüedad en ninguno), pero apuntan a inscripciones
                      distintas (ej. un correo familiar compartido donde el
                      documento del pagador cruza a su propia inscripción,
                      pero ese mismo correo en la hoja de Ingresos quedó
                      asociado a la inscripción de otro familiar). Antes esto
                      se guardaba en silencio como 'cruzado', usando ambos
                      valores tal cual sin ninguna señal de la discrepancia.

Regla "cesantías" (BANCOLOMBIA): NITS_CESANTIAS son referencia_1 de terceros
que reciben pagos por cuenta de muchos estudiantes distintos (ej. NIT de
"PROTECCIÓN SA", que en la hoja BANCOLOMBIA 2576 aparece repetido con 190+
incp distintos — no identifica a una persona, así que buscar por esa llave
siempre "ambiguaría" en falso). Si el email/identification de la transacción
es uno de estos NIT, CORREO(2) se fija directo en "Cesantías" (no se hace
lookup), no cuenta como cruce_ambiguo, y se excluye también de la
comparación de cruce_discrepante contra INCP — la fila puede quedar
'cruzado' con INCP resuelto normalmente por su lado.

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

NOMBRE / MÉTODO DE PAGO / CI — WOMPI automático (13-14 de julio):
Solo para transacciones payment_method WOMPI* con `program` vacío. Se lee
ReportePagosWompi_*.xlsx directo de Drive en cada corrida (subcarpeta "wompi"
dentro de Archivos Cruce, WOMPI_REPORTE_DRIVE_FOLDER_ID) — NO se sincroniza a
ninguna tabla mirror, se arma el lookup en memoria y se descarta al terminar
la corrida (decisión explícita del usuario: es un reporte de pagos del día
para cruzar en el momento, no un dato de referencia que haga falta guardar).

Llave: `Documento` del reporte (prefijo "CC-"/"CEDULA_DE_EXTRANJERIA-" quitado)
vs `identification`. Si hay match: NOMBRE ← Pagador, MÉTODO DE PAGO ← Método
Pago (literal), CI ← Comprobante. Si no hay match (pago manual, sin reportar
en el automático): MÉTODO DE PAGO = "PAGOS MANUALES" (literal), NOMBRE/CI
quedan vacíos y la fila NO puede cerrar como 'cruzado' aunque INCP/CORREO(2)
hayan resuelto limpio — queda 'pendiente' con excepcion_motivo
'sin_identificar_pagador' (nombre provisional, sin confirmar con el usuario).
Si el reporte no se pudo cargar en absoluto esta corrida (archivo no
encontrado en Drive, o WOMPI_REPORTE_DRIVE_FOLDER_ID sin configurar), se
omite esta regla por completo en vez de marcar todas las transacciones WOMPI
como sin identificar — evita falsos positivos masivos si el archivo del día
todavía no se ha subido. WOMPI con `program` lleno y el resto de bancos/
pasarelas quedan fuera de alcance (diseño sin cerrar todavía).
"""

import logging
import os
import sys
from datetime import date, datetime as dt

from dotenv import load_dotenv

from utils.drive import build_drive_service, find_latest_file, download_pdf as download_file
from utils.excel_cartera import read_pagos_wompi_reporte
from utils.parser import normalizar_nit as _normalizar_nit
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


SUFIJOS_IGNORABLES = ('PN', 'PJ', 'P')


def _normalizar_sufijo(valor: str) -> str:
    """Quita el sufijo (PN, PJ, o un "P" truncado, con o sin espacio antes,
    ej. "411 PJ" o "4844P") para comparar el número base.

    Un "P" suelto es ambiguo por sí mismo (puede ser "PN" o "PJ" truncado) —
    esta función solo calcula el número base, no decide a cuál corresponde.
    Esa resolución depende del resto de valores de la misma llave y se hace
    en _build_lookup, no aquí."""
    v = valor.strip()
    upper = v.upper()
    for suf in SUFIJOS_IGNORABLES:
        if upper.endswith(suf):
            return v[:-len(suf)].strip()
    return v


# NIT de terceros/entidades que reciben pagos de cesantías por cuenta de
# muchos estudiantes distintos (ej. "PROTECCIÓN SA"). En la hoja BANCOLOMBIA
# 2576 aparecen como referencia_1 repetida con decenas de incp distintos —
# no identifican a una persona, así que nunca deben tratarse como ambigüedad
# real. Confirmado por el usuario el 8 de julio para "800138188".
NITS_CESANTIAS = {'800138188'}
CESANTIAS_LABEL = 'Cesantías'

def _es_valor_relleno(valor: str) -> bool:
    """True si el valor es basura de captura del Excel de referencia, no un
    cruce real: un punto solo (".") o un sufijo (PN/PJ) sin ningún dígito
    debajo (ej. "PN" a secas). Confirmado por el usuario el 8 de julio."""
    v = valor.strip()
    if not v:
        return False
    if v == '.':
        return True
    return _normalizar_sufijo(v) == ''


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


def _tiene_sufijo_financiero(valor: str) -> bool:
    """True si el valor termina en un sufijo real del sistema financiero (PN
    o PJ, con o sin espacio antes). A diferencia de _normalizar_sufijo, NO
    cuenta un "P" truncado como sufijo real aquí — para esta distinción
    (financiero vs. Access) solo interesa un sufijo confirmado."""
    return valor.strip().upper().endswith(('PN', 'PJ'))


WOMPI_REPORTE_PATTERN = 'ReportePagosWompi'
PAGOS_MANUALES_LABEL  = 'PAGOS MANUALES'
_PREFIJOS_DOCUMENTO_WOMPI = ('CC-', 'CEDULA_DE_EXTRANJERIA-')


def _normalizar_documento_wompi(valor: str) -> str:
    """Quita el prefijo de tipo de documento que trae la columna `Documento`
    de ReportePagosWompi (ej. "CC-1143335891" -> "1143335891"), para poder
    comparar contra `identification` (que nunca trae este prefijo). También
    limpia espacios sueltos alrededor del número (vistos en datos reales,
    ej. "CC- 35412765 ")."""
    v = valor.strip().upper()
    for pref in _PREFIJOS_DOCUMENTO_WOMPI:
        if v.startswith(pref):
            return v[len(pref):].strip()
    return v


def _cargar_lookup_wompi_reporte(sa_json: str, folder_id: str) -> tuple[dict[str, dict], bool]:
    """Lee el ReportePagosWompi_*.xlsx más reciente directo de Drive y arma
    {documento_normalizado: {pagador, metodo_pago, comprobante}} en memoria —
    no se guarda en ninguna tabla, se descarta al terminar la corrida.
    Primera coincidencia gana (mismo criterio BUSCARV que el resto del script).

    Devuelve (lookup, disponible). `disponible=False` significa que esta
    corrida no pudo cargar el reporte en absoluto (config faltante o archivo
    no encontrado) — el llamador debe omitir la regla por completo en ese
    caso, no tratarlo como "ningún documento identificado" (que marcaría de
    golpe todas las transacciones WOMPI del día como sin identificar)."""
    if not sa_json or not folder_id:
        log.warning('GOOGLE_SA_JSON / WOMPI_REPORTE_DRIVE_FOLDER_ID no configurados, '
                    'se omite el cruce NOMBRE/CI/MÉTODO DE PAGO de WOMPI.')
        return {}, False

    drive = build_drive_service(sa_json)
    file_id = find_latest_file(drive, folder_id, WOMPI_REPORTE_PATTERN)
    if not file_id:
        log.warning('No se encontró ningún archivo "%s*" en la carpeta de Drive (%s), '
                    'se omite el cruce NOMBRE/CI/MÉTODO DE PAGO de WOMPI esta corrida.',
                    WOMPI_REPORTE_PATTERN, folder_id)
        return {}, False

    filas = read_pagos_wompi_reporte(download_file(drive, file_id))
    lookup = {}
    for fila in filas:
        doc = _normalizar_documento_wompi(fila.get('documento') or '')
        if doc and doc not in lookup:
            lookup[doc] = fila
    log.info('ReportePagosWompi: %d documentos indexados (de %d filas).', len(lookup), len(filas))
    return lookup, True


def _build_lookup(rows: list[dict], key_field: str, value_field: str,
                   lower: bool = False, fecha_field: str | None = None,
                   preferir_sufijo_financiero: bool = False,
                   ) -> tuple[dict, set, dict]:
    """Primera coincidencia gana (replica BUSCARV de Excel).

    Devuelve además el conjunto de llaves ambiguas: aquellas donde la hoja de
    referencia trae 2+ valores distintos no vacíos para la misma llave (ej. un
    correo con dos números de inscripción diferentes). Esas NO se resuelven
    aquí, solo se señalan para revisión humana.

    Excepción confirmada por el usuario: si los valores de una llave solo
    difieren por un sufijo ignorable ("PN" o "PJ", mismo número, ej. "3300"
    y "3300PN"), no es una ambigüedad real — se normaliza al valor con el
    sufijo y no se marca excepción. Un "P" suelto (sin la segunda letra) se
    trata como "PN"/"PJ" truncado, pero solo se resuelve cuando otro valor de
    la misma llave confirma cuál de los dos es (ej. "179PJ" + "179P" ->
    "179PJ"); si no hay ningún sufijo real que lo confirme, o si la misma
    llave trae más de un sufijo distinto para el mismo número base (ej.
    "3300PN" y "3300PJ"), se deja como ambigüedad real — no hay confirmación
    de que los sufijos sean intercambiables entre sí.

    Si `preferir_sufijo_financiero=True` (solo usado hoy para INCP/
    cartera_inscrip): cuando los valores de una llave tienen números base
    genuinamente distintos (ej. "40908" vs "4260PN" — no es el mismo número
    con/sin sufijo, son dos inscripciones reales distintas, típicamente un
    pregrado antiguo registrado en Access y un diplomado nuevo ya en el
    sistema financiero), si EXACTAMENTE uno de los valores lleva sufijo
    financiero (PN/PJ) y el/los otro(s) no llevan ninguno, se toma el que
    tiene sufijo — este pipeline solo cruza pagos del sistema financiero, así
    que es la mejor señal disponible. No es una certeza absoluta: alguien
    pudo haber borrado el sufijo de un registro por error, así que un valor
    sin sufijo no se descarta de cartera_inscrip, solo se ignora para este
    cruce puntual. Si hay 2+ valores con sufijo financiero, sigue siendo
    ambigüedad real (ver `cruce_incp_exclusiones` para resolverla a mano).

    Filas de relleno (ver _es_valor_relleno: un punto solo o un sufijo sin
    dígitos) se ignoran por completo, como si esa fila no existiera — así una
    fila real con un valor real para la misma llave puede seguir ganando.

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
        if _es_valor_relleno(value):
            continue
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
        bases = {_normalizar_sufijo(v) for v in valores}
        if len(bases) != 1:
            if preferir_sufijo_financiero:
                con_sufijo = {v for v in valores if _tiene_sufijo_financiero(v)}
                sin_sufijo = valores - con_sufijo
                if len(con_sufijo) == 1 and sin_sufijo:
                    lookup[key] = next(iter(con_sufijo))
                    continue
            ambiguos.add(key)
            continue
        base = next(iter(bases))
        sufijos = {v[len(base):] for v in valores if v != base}
        sufijos_reales = {s for s in sufijos if s.upper() in ('PN', 'PJ')}
        if len(sufijos_reales) == 1:
            # un "P" suelto en el grupo se resuelve al sufijo real presente
            # (ej. "179PJ" + "179P" -> "179PJ"): "P" = PN o PJ únicamente
            # cuando hay otro valor exactamente igual salvo la letra que le
            # falta — nunca se asume uno de los dos sin esa confirmación.
            lookup[key] = base + next(iter(sufijos_reales))
        else:
            # sufijos reales distintos a la vez (PN y PJ), o un "P" suelto
            # sin ningún sufijo real que lo desambigüe — no sabemos a cuál
            # corresponde, se deja como ambigüedad real para revisión manual.
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
    for row in inscrip_rows:
        row['numero_id'] = _normalizar_nit(str(row.get('numero_id') or ''))

    exclusiones_rows = select_all(supabase_url, srk, 'cruce_incp_exclusiones',
                                   select='identification,id_inscripcion_excluido')
    exclusiones = {(r['identification'], r['id_inscripcion_excluido']) for r in exclusiones_rows}
    if exclusiones:
        antes = len(inscrip_rows)
        inscrip_rows = [
            r for r in inscrip_rows
            if (r['numero_id'], str(r.get('id_inscripcion') or '').strip()) not in exclusiones
        ]
        log.info('%d filas de cartera_inscrip descartadas por exclusión manual.', antes - len(inscrip_rows))

    bc2576_rows  = select_all(supabase_url, srk, 'cartera_ingresos_bancolombia_2576',
                               select='referencia_1,incp,fecha')
    wompi_rows   = select_all(supabase_url, srk, 'cartera_ingresos_wompi',
                               select='email,inscrip,fecha')
    stripe_rows  = select_all(supabase_url, srk, 'cartera_ingresos_stripe_usa',
                               select='email_cliente,incp,fecha')

    lookup_inscrip, ambiguos_inscrip, _                   = _build_lookup(
        inscrip_rows, 'numero_id', 'id_inscripcion', preferir_sufijo_financiero=True)
    lookup_bc2576, ambiguos_bc2576, historial_bc2576      = _build_lookup(
        bc2576_rows, 'referencia_1', 'incp', fecha_field='fecha')
    lookup_wompi, ambiguos_wompi, historial_wompi         = _build_lookup(
        wompi_rows, 'email', 'inscrip', lower=True, fecha_field='fecha')
    lookup_stripe, ambiguos_stripe, historial_stripe      = _build_lookup(
        stripe_rows, 'email_cliente', 'incp', lower=True, fecha_field='fecha')

    log.info('Referencias cargadas: inscrip=%d, bc2576=%d, wompi=%d, stripe=%d',
              len(lookup_inscrip), len(lookup_bc2576), len(lookup_wompi), len(lookup_stripe))

    sa_json = os.environ.get('GOOGLE_SA_JSON', '')
    wompi_reporte_folder_id = os.environ.get('WOMPI_REPORTE_DRIVE_FOLDER_ID', '')
    lookup_wompi_reporte, wompi_reporte_disponible = _cargar_lookup_wompi_reporte(
        sa_json, wompi_reporte_folder_id)

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
               'email,payment_method,program,phone,payment_amount,matching_key,'
               'registration_date',
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
            if email in NITS_CESANTIAS:
                correo_2           = CESANTIAS_LABEL
                correo_2_ambiguo   = False
                correo_2_historial = {}
            else:
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

        # NOMBRE / MÉTODO DE PAGO / CI — todas las transacciones WOMPI.
        # (No se filtra por `program`: ese campo siempre trae el nombre del
        # pagador para Wompi desde el 26 de junio, nunca está vacío, así que
        # no sirve para distinguir "automático" de "manual" como asumía el
        # diseño original del 13 de julio. El propio cruce contra el reporte
        # ya hace esa distinción: si el documento aparece reportado, era
        # automático; si no, puede ser manual o simplemente no reportado
        # todavía — en ambos casos queda pendiente para revisión.)
        nombre, metodo_de_pago, ci = None, None, None
        wompi_sin_identificar = False
        if wompi_reporte_disponible and payment_method.startswith('WOMPI'):
            doc = _normalizar_documento_wompi(identification) if identification else ''
            match = lookup_wompi_reporte.get(doc) if doc else None
            if match:
                nombre         = match.get('pagador') or None
                metodo_de_pago = match.get('metodo_pago') or None
                ci             = match.get('comprobante') or None
            else:
                metodo_de_pago = PAGOS_MANUALES_LABEL
                wompi_sin_identificar = True

        if incp_ambiguo or correo_2_ambiguo:
            excepcion_motivo, estado_cruce = 'cruce_ambiguo', 'pendiente'
        elif (incp and correo_2 and correo_2 != CESANTIAS_LABEL
              and _normalizar_sufijo(incp) != _normalizar_sufijo(correo_2)):
            excepcion_motivo, estado_cruce = 'cruce_discrepante', 'pendiente'
        elif not incp and not correo_2:
            excepcion_motivo, estado_cruce = 'sin_cruce', 'pendiente'
        elif wompi_sin_identificar:
            # Tiene prioridad sobre INCP/CORREO(2): aunque esos dos hayan
            # resuelto limpio, la fila no puede cerrar como 'cruzado' si no
            # se pudo identificar NOMBRE/CI del pagador (diseño 13 julio).
            excepcion_motivo, estado_cruce = 'sin_identificar_pagador', 'pendiente'
        else:
            excepcion_motivo, estado_cruce = None, 'cruzado'

        resultado.append({
            'matching_key':       t.get('matching_key'),
            'identification':     t.get('identification'),
            'registration_date':  t.get('registration_date'),
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
            'nombre':             nombre,
            'metodo_de_pago':     metodo_de_pago,
            'ci':                 ci,
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
