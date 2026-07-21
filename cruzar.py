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
  - cruce_unico:      CORREO(2) encontró resultado (sin ambigüedad) pero INCP
                      no encontró nada (regla del 14 de julio). INCP (número
                      de documento vs cartera_inscrip) es una señal fuerte y
                      solo con eso ya cierra 'cruzado' sin problema; CORREO(2)
                      (VLOOKUP por email) es más débil — un correo por sí solo,
                      sin que el documento lo confirme, no cierra la fila,
                      queda pendiente para revisión manual.

Pagos apartados (Fase 2 del rediseño, 16 de julio): cesantías y "pago por
llave" ya NO se resuelven dentro del cruce (la regla vieja NITS_CESANTIAS,
que fijaba CORREO(2)="Cesantías" por NIT en Bancolombia 2576, se elimina —
nunca disparaba para el resto de bancos y no describía bien los datos
reales). Ahora, al inicio de main(), cada transacción se revisa contra:
  - Cesantías: transaction_code_1 contiene "PROTECCION" o "FONDO NACIONAL"
    (semilla fija, no se agregan fondos especulativos), o coincide
    exactamente (normalizado) con algo en la tabla cesantias_patrones (lista
    aprendida, crece por uso desde financial-platform).
  - Pago por llave: identification o email es uno de los identificadores de
    canal fijos (ID_CANAL_PAGO_LLAVE) — NO son cédulas de personas, son
    llaves de la universidad que aparecen idénticas en pagos de decenas de
    estudiantes distintos.
Las que matchean y todavía no están en pagos_apartados se insertan ahí
(tipo='cesantias'/'pago_llave', origen='automatico') y se excluyen por
completo del cruce (ni INCP ni CORREO(2), tampoco _sugerir_por_cadencia) —
si alguna ya estaba en cruce_cartera de una corrida anterior, se borra
(retroactivo). Las que YA están en pagos_apartados con incp_resuelto
llenado a mano desde financial-platform vuelven al flujo con ese INCP
forzado (sin recalcular por lookup) y cierran 'cruzado' directamente.

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

NOMBRE / MÉTODO DE PAGO / CI / VAL / PROGRAM — WOMPI LINK vs MANUAL (13-14 de
julio, reescrito el 16 como Fase 9 del rediseño): se lee
ReportePagosWompi_*.xlsx directo de Drive en cada corrida (subcarpeta "wompi"
dentro de Archivos Cruce, WOMPI_REPORTE_DRIVE_FOLDER_ID) — NO se sincroniza a
ninguna tabla mirror, se arma el lookup en memoria y se descarta al terminar
la corrida (decisión explícita del usuario: es un reporte de pagos del día
para cruzar en el momento, no un dato de referencia que haga falta guardar).

Llave: `Documento` del reporte (prefijo "CC-"/"CEDULA_DE_EXTRANJERIA-"
quitado) vs `identification`. Si hay match (WOMPI LINK, 9.2): NOMBRE ←
Pagador, MÉTODO DE PAGO = "WOMPI (Genera Link)" (literal, ya NO se lee del
reporte), CI ← Comprobante, VAL ← Transaction Id (Wompi) (mismo formato que
`id de la transaccion` del CSV de WOMPI), PROGRAM ← Proyecto — y el INCP del
reporte (columna "Inscripción", casi siempre sin sufijo, ej. "3077") gana
sobre el lookup por documento y por correo: se le busca la forma con sufijo
en cartera_inscrip (ej. "3077PN", ver _resolver_incp_wompi_link) y, si
resuelve sin ambigüedad, la fila cierra 'cruzado' directo — NO puede quedar
'cruce_ambiguo'/'cruce_discrepante' por INCP. Si esa Inscripción no existe en
Payu UC (o es ambigua entre PN/PJ), no se inventa nada: la fila queda
'pendiente' con excepcion_motivo='pendiente_asignar_incp' (línea 25),
_sin importar_ lo que el lookup normal (INCP/CORREO(2)/Fase 3.6) hubiera
resuelto por su cuenta.

Si NO hay match (WOMPI MANUAL, 9.3): se identifica por CORREO(2) contra la
hoja WOMPI de "Ingresos PSE y PAYU" — comportamiento ya existente, sin
cambios. MÉTODO DE PAGO = "PAGOS MANUALES" (literal), NOMBRE/CI/VAL quedan
vacíos, PROGRAM queda vacío (el parser de WOMPI ya no lo llena, ver
fuentes/wompi.py) — puramente informativo, no bloquea el cierre (sigue
aplicando la asimetría del 14 de julio: CORREO(2) solo → cruce_unico).

Re-evaluación de filas viejas (9.4, "línea 22"): las filas WOMPI que
cruzaron ANTES del fix del 14 de julio (bug de "program vacío" que nunca
dejaba correr la regla del reporte) quedaron cerradas 'cruzado' con
"PAGOS MANUALES" siendo en realidad LINK. Al final de main() se recalculan
los campos del reporte (val/nombre/metodo_de_pago/ci/program) + el INCP de
9.2 sobre las filas WOMPI ya 'cruzado' cuyo documento SÍ aparece en el
reporte de esta corrida — excepción acotada a "las filas terminales no se
tocan": solo estos campos, nunca estado_cruce/excepcion_motivo, y solo si
`corregido_manual=false` (si un humano la tocó desde financial-platform, no
se pisa nunca). Limitación conocida y aceptada: el lookup solo cubre el
período del reporte más reciente (no se rehace un histórico acumulado), así
que esta re-evaluación solo alcanza filas viejas cuyo pago siga apareciendo
en el archivo más reciente que se suba a Drive.

Si el reporte no se pudo cargar en absoluto esta corrida (archivo no
encontrado en Drive, o WOMPI_REPORTE_DRIVE_FOLDER_ID sin configurar), se
omite toda esta sección (LINK/MANUAL y la re-evaluación 9.4 por igual) —
NOMBRE/MÉTODO DE PAGO/CI/VAL quedan NULL y PROGRAM vacío para todas las
transacciones WOMPI de esa corrida, sin afectar estado_cruce de ninguna
fila nueva (el resto de bancos/pasarelas quedan fuera de alcance, diseño sin
cerrar todavía). El excepcion_motivo 'sin_identificar_pagador' de la
migración del CHECK constraint sigue sin uso (ver cierre del 14 de julio).

Stripe: cierre por doble señal correo+nombre, respaldo por nombre, excepción
pendiente_asignar_incp (16 de julio, Fase 3.1-3.3 del rediseño):

Stripe nunca trae `identification` (Stripe no manda documento), así que
`incp` siempre da vacío — con la asimetría INCP/CORREO(2) del 14 de julio
(CORREO(2) solo → cruce_unico), Stripe quedaba pendiente para siempre.

  1. Si el correo cruza contra STRIPE_USA (sin ambigüedad) y el nombre del
     pagador (transaction_code_1 = Card Name) coincide con NOMBRE CLIENTE de
     esa fila → cierra 'cruzado' directo, sin esperar confirmación manual.
     Si el correo cruza pero el nombre NO coincide, sigue 'cruce_unico' (caso
     legítimo: un tercero pagando por otro).
  2. Si el correo NO cruza, se busca por nombre entre TODAS las filas de
     STRIPE_USA (mismo criterio de comparación). Es señal débil (homónimos):
     si resuelve a un único INCP, cierra como CORREO(2) solo → cruce_unico,
     nunca 'cruzado' directo. Si el nombre es ambiguo (2+ INCP distintos
     entre las filas que matchean) → cruce_ambiguo.
  3. Si el correo (o el nombre) encuentra fila en la hoja pero esa fila tiene
     el INCP vacío → no es 'sin_cruce' (el equipo aún no le asignó
     inscripción todavía): excepción propia 'pendiente_asignar_incp', se
     resuelve sola cuando alguien llene el INCP en el Excel.

Comparación de nombres (_normalizar_nombre / _nombres_coinciden): NFKD →
quitar acentos → ASCII → MAYÚSCULAS → quitar puntuación → colapsar espacios;
tokenizar y quedarse con tokens de más de 2 caracteres; coincide si comparten
2+ tokens (o si son idénticos y el nombre tiene 1 solo token). No exige
igualdad exacta porque Card Name suele venir recortado (ej. "GABRIELA
VALDIVIA" vs "GABRIELA VALDIVIA PARODI"). Requiere reparar mojibake primero
(los nombres de STRIPE_USA vienen con UTF-8 mal leído como cp1252 en el
Excel de origen) — se repara en utils/excel_cartera.py (reparar_mojibake,
aplicada en _cell_str para todas las hojas, no solo Stripe) antes de que
estos nombres lleguen aquí.
"""

import logging
import os
import re
import sys
import unicodedata
from datetime import date, datetime as dt

from dotenv import load_dotenv

from utils.drive import build_drive_service, find_latest_file, download_pdf as download_file
from utils.excel_cartera import read_pagos_wompi_reporte
from utils.parser import normalizar_nit as _normalizar_nit
from utils.parser import normalizar_sufijo as _normalizar_sufijo
from utils.supabase import (select_all, upsert_cruce, upsert_pagos_apartados, delete_by_keys,
                             update_cruce_valores)

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


# Identificadores de canal (no cédulas de personas): "90473364" es la llave
# fija del canal de pago de la universidad, aparece idéntica en pagos de
# decenas de estudiantes distintos (56 incp distintos en
# cartera_ingresos_bancolombia_2576, confirmado el 16 de julio). "800138188"
# es el NIT de un intermediario de cesantías (ej. "PROTECCIÓN SA") que
# también recibe pagos por cuenta de muchos estudiantes — mismo patrón.
# Ninguno de los dos identifica a una persona, así que nunca deben cruzarse
# ni sugerirse por cadencia — se apartan del proceso por completo.
ID_CANAL_PAGO_LLAVE = {'90473364', '800138188'}

# Semilla fija para detectar cesantías por descripción (Bancolombia). NO
# agregar fondos especulativos (PORVENIR, COLFONDOS...) que no aparecen en
# los datos reales — decisión explícita del 16 de julio. "PAGO DE PROV" NO
# es señal (es el tipo genérico de pago a proveedores, lo usan empresas
# normales) — solo el nombre del fondo cuenta.
CESANTIAS_SEMILLA = ('PROTECCION', 'FONDO NACIONAL')


def _normalizar_descripcion(desc: str) -> str:
    return ' '.join(str(desc or '').upper().split())


def _es_cesantias(transaction_code_1: str, patrones_aprendidos: set[str]) -> bool:
    desc = _normalizar_descripcion(transaction_code_1)
    if not desc:
        return False
    if any(semilla in desc for semilla in CESANTIAS_SEMILLA):
        return True
    return desc in patrones_aprendidos


def _es_pago_llave(identification: str, email: str) -> bool:
    return identification in ID_CANAL_PAGO_LLAVE or email in ID_CANAL_PAGO_LLAVE


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


def _normalizar_candidatos(valores: set[str]) -> set[str]:
    """Colapsa variantes de formato (mismo número base, con/sin sufijo PN/PJ)
    en un solo candidato — mismo criterio de _build_lookup, pero aquí
    interesa el CONJUNTO completo de candidatos reales (Fase 3.6), no solo
    detectar ambigüedad. Si dos variantes comparten base y una trae sufijo
    financiero y la otra no, se prefiere la que trae sufijo (mismo criterio
    "sistema financiero sobre Access" que preferir_sufijo_financiero)."""
    por_base: dict[str, set[str]] = {}
    for v in valores:
        base = _normalizar_sufijo(v) or v
        por_base.setdefault(base, set()).add(v)
    resultado = set()
    for variantes in por_base.values():
        con_sufijo = {v for v in variantes if _tiene_sufijo_financiero(v)}
        resultado |= con_sufijo if con_sufijo else variantes
    return resultado


def _tiene_deuda_pendiente(candidato: str, bases_con_deuda: set[str]) -> bool:
    return (_normalizar_sufijo(candidato) or candidato) in bases_con_deuda


_RE_NO_ALFANUM = re.compile(r'[^A-Z0-9\s]')


def _normalizar_nombre(nombre: str) -> str:
    """NFKD -> quitar acentos -> ASCII -> MAYÚSCULAS -> quitar puntuación ->
    colapsar espacios (Fase 3.1, comparación de nombres para Stripe)."""
    nfkd = unicodedata.normalize('NFKD', str(nombre or ''))
    solo_ascii = nfkd.encode('ascii', 'ignore').decode('ascii')
    solo_alfanum = _RE_NO_ALFANUM.sub(' ', solo_ascii.upper())
    return ' '.join(solo_alfanum.split())


def _tokens_nombre(nombre: str) -> set[str]:
    """Tokens de más de 2 caracteres del nombre normalizado — descarta
    partículas como "DE"/"A" e iniciales sueltas."""
    return {t for t in _normalizar_nombre(nombre).split() if len(t) > 2}


def _nombres_coinciden(a: str, b: str) -> bool:
    """Coincide si comparten 2+ tokens, o si ambos se reducen a un único
    token idéntico (nombre de una sola palabra, ej. "MADONNA"). No exige
    igualdad exacta — el Card Name de Stripe suele venir recortado."""
    tokens_a, tokens_b = _tokens_nombre(a), _tokens_nombre(b)
    if not tokens_a or not tokens_b:
        return False
    if len(tokens_a & tokens_b) >= 2:
        return True
    return len(tokens_a) == 1 and len(tokens_b) == 1 and tokens_a == tokens_b


def _cruzar_stripe(card_name: str, email_lower: str, lookup_stripe: dict,
                    ambiguos_stripe: set, nombre_por_email: dict, emails_hoja: set,
                    candidatos_nombre: list[tuple[str, str]]) -> tuple[str, bool, bool, bool]:
    """Cruce de Stripe con doble señal correo+nombre (Fase 3.1-3.3, ver
    docstring del módulo). Devuelve (correo_2, ambiguo, pendiente_incp,
    confirmado_por_nombre):
      - Si el correo cruza sin ambigüedad: correo_2 = valor encontrado,
        confirmado_por_nombre = True si el Card Name coincide con NOMBRE
        CLIENTE de esa fila (ver _nombres_coinciden).
      - Si el correo cruza pero esa fila no tiene INCP: pendiente_incp=True.
      - Si el correo no cruza en absoluto: respaldo por nombre, comparando
        contra TODAS las filas de la hoja (candidatos_nombre). Nunca marca
        confirmado_por_nombre (es señal débil por sí sola, ver 3.2)."""
    if email_lower and email_lower in ambiguos_stripe:
        return '', True, False, False

    if email_lower and lookup_stripe.get(email_lower):
        # .get(...) en vez de "in": _build_lookup guarda la llave igual
        # aunque el INCP de esa fila venga vacío (_es_valor_relleno('') es
        # False — una cadena vacía no cuenta como "relleno"), así que
        # lookup_stripe puede tener email_lower -> '' — eso es justo el
        # caso "fila existe, INCP vacío" que cae más abajo, no un cruce real.
        valor = lookup_stripe[email_lower]
        nombre_fila = nombre_por_email.get(email_lower, '')
        confirmado = _nombres_coinciden(card_name, nombre_fila)
        return valor, False, False, confirmado

    if email_lower and email_lower in emails_hoja:
        # la(s) fila(s) de este correo existen pero su INCP está vacío
        return '', False, True, False

    if not card_name:
        return '', False, False, False

    incps_match, fila_con_incp_vacio = set(), False
    for nombre_fila, incp in candidatos_nombre:
        if not _nombres_coinciden(card_name, nombre_fila):
            continue
        if _es_valor_relleno(incp):
            fila_con_incp_vacio = True
        else:
            incps_match.add(incp)

    if len(incps_match) == 1:
        return next(iter(incps_match)), False, False, False
    if len(incps_match) > 1:
        return '', True, False, False
    if fila_con_incp_vacio:
        return '', False, True, False
    return '', False, False, False


WOMPI_REPORTE_PATTERN = 'ReportePagosWompi'
PAGOS_MANUALES_LABEL  = 'PAGOS MANUALES'
WOMPI_GENERA_LINK_LABEL = 'WOMPI (Genera Link)'


def _cargar_lookup_wompi_reporte(sa_json: str, folder_id: str) -> tuple[dict[str, dict], bool]:
    """Lee el ReportePagosWompi_*.xlsx más reciente directo de Drive y arma
    {id_transaccion: {pagador, comprobante, inscripcion, id_transaccion,
    proyecto, fecha_pago}} en memoria — no se guarda en ninguna tabla, se
    descarta al terminar la corrida.

    Indexado por `id_transaccion` (columna "Transaction Id (Wompi)"), NO por
    documento (21 de julio, regla #2 de "Automatización de Cartera"): el
    match anterior por `Documento` vs `identification` fallaba cuando la
    cédula no calzaba limpio, clasificando pagos que SÍ fueron por link como
    "PAGOS MANUALES". El id de transacción es único — WOMPI lo guarda como
    `matching_key` en `consolidated_transactions` (ver `fuentes/wompi.py`,
    `VAL` = id de la transacción), así que cruzar código contra código es
    exacto. Filas del reporte sin `id_transaccion` se omiten (no hay con qué
    cruzarlas). Primera coincidencia gana (mismo criterio BUSCARV que el
    resto del script).

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
        tx_id = str(fila.get('id_transaccion') or '').strip()
        if tx_id and tx_id not in lookup:
            lookup[tx_id] = fila
    log.info('ReportePagosWompi: %d id(s) de transacción indexados (de %d filas).', len(lookup), len(filas))
    return lookup, True


def _build_id_inscripcion_por_base(inscrip_rows: list[dict]) -> dict[str, set[str]]:
    """Índice inverso de cartera_inscrip.id_inscripcion: número base (sin
    sufijo PN/PJ) -> conjunto de valores completos vistos con esa base.
    Usado por WOMPI LINK (Fase 9.2) para encontrar la forma con sufijo del
    número de "Inscripción" que trae ReportePagosWompi (ej. "3077" ->
    "3077PN")."""
    resultado: dict[str, set[str]] = {}
    for row in inscrip_rows:
        val = str(row.get('id_inscripcion') or '').strip()
        if not val:
            continue
        base = _normalizar_sufijo(val) or val
        resultado.setdefault(base, set()).add(val)
    return resultado


def _resolver_incp_wompi_link(inscripcion_reporte: str,
                               id_inscripcion_por_base: dict[str, set[str]]) -> str | None:
    """Fase 9.2: dado el número de "Inscripción" que trae ReportePagosWompi
    (ej. "3077", normalmente sin sufijo), busca su forma con sufijo en
    cartera_inscrip (ej. "3077PN"). Si esa base no aparece en absoluto, o
    aparece con 2+ valores reales distintos (ej. "3077PN" y "3077PJ", ambos
    inscritos — ambigüedad real que el reporte no confirma), no se inventa
    nada: devuelve None (línea 25 — queda pendiente_asignar_incp, no se
    cierra)."""
    base = _normalizar_sufijo(str(inscripcion_reporte or '').strip())
    if not base:
        return None
    candidatos = id_inscripcion_por_base.get(base)
    if candidatos and len(candidatos) == 1:
        return next(iter(candidatos))
    return None


def _build_lookup(rows: list[dict], key_field: str, value_field: str,
                   lower: bool = False, fecha_field: str | None = None,
                   preferir_sufijo_financiero: bool = False,
                   ) -> tuple[dict, set, dict, dict]:
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

    Devuelve también `valores_no_vacios` ({key: set(valores crudos)}) — el
    conjunto COMPLETO de candidatos vistos para cada llave, sin importar si
    quedó ambigua o no. Lo usa el árbitro de cartera preventiva (Fase 3.6)
    para armar la lista de candidatos reales de una llave ambigua (no solo
    el valor que "ganó" el BUSCARV).
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

    return lookup, ambiguos, historial, valores_no_vacios


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
                               select='email_cliente,incp,nombre_cliente,fecha')

    lookup_inscrip, ambiguos_inscrip, _, valores_inscrip       = _build_lookup(
        inscrip_rows, 'numero_id', 'id_inscripcion', preferir_sufijo_financiero=True)
    id_inscripcion_por_base = _build_id_inscripcion_por_base(inscrip_rows)
    lookup_bc2576, ambiguos_bc2576, historial_bc2576, valores_bc2576 = _build_lookup(
        bc2576_rows, 'referencia_1', 'incp', fecha_field='fecha')
    lookup_wompi, ambiguos_wompi, historial_wompi, valores_wompi     = _build_lookup(
        wompi_rows, 'email', 'inscrip', lower=True, fecha_field='fecha')
    lookup_stripe, ambiguos_stripe, historial_stripe, valores_stripe = _build_lookup(
        stripe_rows, 'email_cliente', 'incp', lower=True, fecha_field='fecha')

    log.info('Referencias cargadas: inscrip=%d, bc2576=%d, wompi=%d, stripe=%d',
              len(lookup_inscrip), len(lookup_bc2576), len(lookup_wompi), len(lookup_stripe))

    # Fase 3.1-3.3: estructuras auxiliares para el cruce de Stripe por doble
    # señal correo+nombre (ver _cruzar_stripe). nombre_cliente ya viene
    # reparado de mojibake desde utils/excel_cartera.py (_cell_str).
    nombre_cliente_por_email_stripe: dict[str, str] = {}
    emails_stripe: set[str] = set()
    candidatos_nombre_stripe: list[tuple[str, str]] = []
    for row in stripe_rows:
        email = str(row.get('email_cliente') or '').strip().lower()
        nombre_cliente = row.get('nombre_cliente') or ''
        if email:
            emails_stripe.add(email)
            if email not in nombre_cliente_por_email_stripe:
                nombre_cliente_por_email_stripe[email] = nombre_cliente
        if nombre_cliente:
            candidatos_nombre_stripe.append((nombre_cliente, str(row.get('incp') or '').strip()))

    # Fase 3.6: inscripciones con al menos una cuota SIN pago identificado en
    # cartera_preventiva — el árbitro solo puede elegir entre estas (ver
    # _tiene_deuda_pendiente). "resuelta" = tiene fecha_pago; no toca a
    # cartera_preventiva ni requiere que cruzar_cartera_preventiva.py haya
    # corrido en este mismo ciclo (usa el estado tal cual está ahora mismo).
    preventiva_rows_arbitro = select_all(supabase_url, srk, 'cartera_preventiva',
                                          select='inscrip,fecha_pago')
    bases_con_deuda: set[str] = set()
    for r in preventiva_rows_arbitro:
        if r.get('fecha_pago') is not None:
            continue
        insc = str(r.get('inscrip') or '').strip()
        if insc:
            bases_con_deuda.add(_normalizar_sufijo(insc) or insc)
    log.info('%d inscripciones (base) con al menos una cuota sin pago identificado.', len(bases_con_deuda))

    sa_json = os.environ.get('GOOGLE_SA_JSON', '')
    wompi_reporte_folder_id = os.environ.get('WOMPI_REPORTE_DRIVE_FOLDER_ID', '')
    lookup_wompi_reporte, wompi_reporte_disponible = _cargar_lookup_wompi_reporte(
        sa_json, wompi_reporte_folder_id)

    log.info('Cargando estado_cruce existente...')
    existentes = select_all(
        supabase_url, srk, 'cruce_cartera',
        select='matching_key,identification,payment_method,estado_cruce,corregido_manual',
    )
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

    log.info('Cargando pagos_apartados y patrones de cesantías...')
    apartados_rows = select_all(supabase_url, srk, 'pagos_apartados',
                                 select='matching_key,tipo,incp_resuelto')
    apartados_map = {r['matching_key']: r for r in apartados_rows}
    patrones_cesantias = {
        r['descripcion'] for r in select_all(supabase_url, srk, 'cesantias_patrones', select='descripcion')
        if r.get('descripcion')
    }

    nuevas_apartadas = []
    for t in transacciones:
        mk = t.get('matching_key')
        if mk in apartados_map:
            continue
        identification = str(t.get('identification') or '').strip()
        email          = str(t.get('email') or '').strip()
        if _es_pago_llave(identification, email):
            tipo = 'pago_llave'
        elif _es_cesantias(t.get('transaction_code_1'), patrones_cesantias):
            tipo = 'cesantias'
        else:
            continue
        nuevas_apartadas.append({
            'matching_key':       mk,
            'tipo':               tipo,
            'origen':             'automatico',
            'es_pago_unico':      False,
            'incp_resuelto':      None,
            'aparicion':          None,
            'fecha_ingreso':      t.get('registration_date'),
            'val':                None,
            'identification':     t.get('identification'),
            'payment_date':       t.get('payment_date'),
            'transaction_code_1': t.get('transaction_code_1'),
            'transaction_code_2': t.get('transaction_code_2'),
            'email':              t.get('email'),
            'payment_method':     t.get('payment_method'),
            'program':            t.get('program'),
            'phone':              t.get('phone'),
            'payment_amount':     t.get('payment_amount'),
        })
        apartados_map[mk] = {'matching_key': mk, 'tipo': tipo, 'incp_resuelto': None}

    if nuevas_apartadas:
        upsert_pagos_apartados(supabase_url, srk, nuevas_apartadas)
        n_cesantias = sum(1 for a in nuevas_apartadas if a['tipo'] == 'cesantias')
        n_llave     = sum(1 for a in nuevas_apartadas if a['tipo'] == 'pago_llave')
        log.info('%d pago(s) apartados automáticamente (cesantias=%d, pago_llave=%d).',
                  len(nuevas_apartadas), n_cesantias, n_llave)

    excluir_sin_incp    = {mk for mk, info in apartados_map.items() if not info.get('incp_resuelto')}
    reintegrar_con_incp = {mk: info['incp_resuelto'] for mk, info in apartados_map.items()
                            if info.get('incp_resuelto')}

    llaves_en_cruce = {r['matching_key'] for r in existentes}
    a_borrar = [mk for mk in excluir_sin_incp if mk in llaves_en_cruce]
    if a_borrar:
        delete_by_keys(supabase_url, srk, 'cruce_cartera', 'matching_key', a_borrar)
        log.info('%d fila(s) borradas de cruce_cartera por quedar apartadas (retroactivo).', len(a_borrar))

    transacciones_activas, filas_reintegradas = [], []
    for t in transacciones:
        mk = t.get('matching_key')
        if mk in excluir_sin_incp:
            continue
        if mk in reintegrar_con_incp:
            filas_reintegradas.append(t)
        else:
            transacciones_activas.append(t)

    log.info('%d transacciones activas, %d apartadas sin INCP, %d reintegradas con INCP forzado.',
              len(transacciones_activas), len(excluir_sin_incp), len(filas_reintegradas))

    resultado = []
    for t in filas_reintegradas:
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
            'val':                None,
            'incp':               reintegrar_con_incp[t.get('matching_key')],
            'correo_2':           None,
            'nombre':             None,
            'metodo_de_pago':     None,
            'ci':                 None,
            'excepcion_motivo':   None,
            'estado_cruce':       'cruzado',
        })

    for t in transacciones_activas:
        identification = str(t.get('identification') or '').strip()
        email          = str(t.get('email') or '').strip()
        email_lower    = email.lower()
        payment_method = str(t.get('payment_method') or '').upper()

        incp         = lookup_inscrip.get(identification, '')
        incp_ambiguo = identification in ambiguos_inscrip

        correo_2            = ''
        correo_2_ambiguo    = False
        correo_2_historial  = {}
        stripe_pendiente_incp    = False
        stripe_confirmado_nombre = False
        if payment_method == 'BANCOLOMBIA':
            correo_2           = lookup_bc2576.get(email, '')
            correo_2_ambiguo   = email in ambiguos_bc2576
            correo_2_historial = historial_bc2576.get(email, {})
        elif payment_method.startswith('WOMPI'):
            correo_2           = lookup_wompi.get(email_lower, '')
            correo_2_ambiguo   = email_lower in ambiguos_wompi
            correo_2_historial = historial_wompi.get(email_lower, {})
        elif payment_method == 'STRIPE_USA':
            card_name = str(t.get('transaction_code_1') or '')
            correo_2, correo_2_ambiguo, stripe_pendiente_incp, stripe_confirmado_nombre = _cruzar_stripe(
                card_name, email_lower, lookup_stripe, ambiguos_stripe,
                nombre_cliente_por_email_stripe, emails_stripe, candidatos_nombre_stripe)
            correo_2_historial = historial_stripe.get(email_lower, {})

        if correo_2_ambiguo:
            sugerido = _sugerir_por_cadencia(correo_2_historial, _parse_fecha(t.get('payment_date')))
            if sugerido:
                correo_2 = sugerido

        # Unificar presentación de sufijo cuando INCP y CORREO(2) son el mismo
        # número base pero cada uno vino de una hoja distinta con su propio
        # formato (ej. INCP="2570PN" desde Payu UC.xlsx, CORREO(2)="2570"
        # desde Ingresos PSE y PAYU.xlsx — misma inscripción, dos hojas). Ya
        # NO se marcan como cruce_discrepante entre sí (misma base), pero sin
        # esto se guardaban/mostraban distinto pese a ser el mismo número.
        if (incp and correo_2
                and incp != correo_2 and _normalizar_sufijo(incp) == _normalizar_sufijo(correo_2)):
            if _tiene_sufijo_financiero(incp) and not _tiene_sufijo_financiero(correo_2):
                correo_2 = incp
            elif _tiene_sufijo_financiero(correo_2) and not _tiene_sufijo_financiero(incp):
                incp = correo_2

        # WOMPI LINK vs MANUAL (Fase 9.1-9.3, 16 de julio). El documento del
        # pago está en ReportePagosWompi -> LINK: "el reporte manda" (9.2),
        # su INCP gana sobre el lookup por documento y por correo, y la fila
        # no puede quedar ambigua/discrepante por INCP (se fuerza más abajo
        # antes de la Fase 3.6, para que ni siquiera se dispare el árbitro).
        # Si no está -> MANUAL, se identifica por correo — comportamiento ya
        # existente, sin cambios (9.3). NOMBRE/CI/`val`/`program` son
        # puramente informativos en ambos casos; el único campo que puede
        # bloquear el cierre es el INCP de una fila LINK sin resolver
        # (9.2, línea 25).
        val, nombre, metodo_de_pago, ci = None, None, None, None
        program = t.get('program')
        wompi_link_resuelto, wompi_link_pendiente = False, False
        if wompi_reporte_disponible and payment_method.startswith('WOMPI'):
            tx_id = str(t.get('matching_key') or '').strip()
            match = lookup_wompi_reporte.get(tx_id) if tx_id else None
            if match:
                val            = match.get('id_transaccion') or None
                nombre         = match.get('pagador') or None
                metodo_de_pago = WOMPI_GENERA_LINK_LABEL
                ci             = match.get('comprobante') or None
                program        = match.get('proyecto') or None
                incp_link = _resolver_incp_wompi_link(match.get('inscripcion'), id_inscripcion_por_base)
                if incp_link:
                    incp, incp_ambiguo, correo_2_ambiguo = incp_link, False, False
                    wompi_link_resuelto = True
                else:
                    wompi_link_pendiente = True
            else:
                metodo_de_pago = PAGOS_MANUALES_LABEL

        # Fase 3.6: cartera preventiva como árbitro del INCP. SOLO desempata
        # filas que YA iban a quedar cruce_ambiguo (2+ candidatos) — un cruce
        # limpio de 1 solo candidato nunca pasa por acá, y una fila ya
        # 'cruzado' tampoco (queda fuera de transacciones_activas). Candidatos
        # = unión de lo que trajo el lookup por documento (INCP) y por correo
        # (CORREO(2)), filtrados a los que tienen cuota pendiente; si queda
        # exactamente uno, gana — aunque contradiga el valor que había en
        # incp/correo_2 (ver "caso conflicto" en la spec).
        if incp_ambiguo or correo_2_ambiguo:
            candidatos_doc = valores_inscrip.get(identification, set())
            if payment_method == 'BANCOLOMBIA':
                candidatos_correo = valores_bc2576.get(email, set())
            elif payment_method.startswith('WOMPI'):
                candidatos_correo = valores_wompi.get(email_lower, set())
            elif payment_method == 'STRIPE_USA':
                candidatos_correo = valores_stripe.get(email_lower, set())
            else:
                candidatos_correo = set()

            candidatos = _normalizar_candidatos(candidatos_doc | candidatos_correo)
            candidatos_con_deuda = {c for c in candidatos if _tiene_deuda_pendiente(c, bases_con_deuda)}

            if len(candidatos_con_deuda) == 1:
                ganador = next(iter(candidatos_con_deuda))
                incp, correo_2 = ganador, ganador
                incp_ambiguo, correo_2_ambiguo = False, False
            # 2+ o 0 candidatos con deuda: sin cambio, sigue cruce_ambiguo
            # (ya lo era) — ver puntos 4 y 5 de la regla.

        if wompi_link_pendiente:
            # Fase 9.2, línea 25: es LINK pero el "Inscripción" del reporte
            # no se pudo resolver contra Payu UC (no existe, o ambiguo entre
            # PN/PJ) — no se inventa el sufijo ni se cierra, sin importar lo
            # que haya resuelto el lookup normal (incluida la Fase 3.6).
            excepcion_motivo, estado_cruce = 'pendiente_asignar_incp', 'pendiente'
        elif wompi_link_resuelto:
            # El reporte manda (9.2): gana sobre INCP/CORREO(2) del lookup
            # normal, cierra directo sin pasar por ambigüedad/discrepancia.
            excepcion_motivo, estado_cruce = None, 'cruzado'
        elif incp_ambiguo or correo_2_ambiguo:
            excepcion_motivo, estado_cruce = 'cruce_ambiguo', 'pendiente'
        elif (incp and correo_2
              and _normalizar_sufijo(incp) != _normalizar_sufijo(correo_2)):
            excepcion_motivo, estado_cruce = 'cruce_discrepante', 'pendiente'
        elif not incp and not correo_2:
            # Stripe (3.3): el correo/nombre encontró fila en STRIPE_USA pero
            # esa fila todavía no tiene INCP asignado — no es lo mismo que no
            # encontrar nada, se resuelve solo cuando el equipo llene el
            # Excel. Motivo propio, no 'sin_cruce'.
            if stripe_pendiente_incp:
                excepcion_motivo, estado_cruce = 'pendiente_asignar_incp', 'pendiente'
            else:
                excepcion_motivo, estado_cruce = 'sin_cruce', 'pendiente'
        elif not incp and correo_2:
            # Solo CORREO(2) identificó, sin confirmación de INCP — señal más
            # débil (regla del 14 de julio: INCP solo SÍ cierra 'cruzado' sin
            # problema, CORREO(2) solo NO, queda para revisión manual).
            # Excepción (3.1): Stripe con doble señal correo+nombre confirmada
            # SÍ cierra 'cruzado' directo — el nombre del pagador coincide con
            # NOMBRE CLIENTE de la fila que encontró el correo.
            if stripe_confirmado_nombre:
                excepcion_motivo, estado_cruce = None, 'cruzado'
            else:
                excepcion_motivo, estado_cruce = 'cruce_unico', 'pendiente'
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
            'program':            program,
            'phone':              t.get('phone'),
            'payment_amount':     t.get('payment_amount'),
            'val':                val,
            'incp':               incp or None,
            'correo_2':           correo_2 or None,
            'nombre':             nombre,
            'metodo_de_pago':     metodo_de_pago,
            'ci':                 ci,
            'excepcion_motivo':   excepcion_motivo,
            'estado_cruce':       estado_cruce,
        })

    # Fase 9.4 (línea 22, "Opción B"): filas WOMPI que cerraron 'cruzado'
    # ANTES del fix del 14 de julio (bug de "program vacío" que nunca dejaba
    # correr la regla del reporte) quedaron marcadas "PAGOS MANUALES" siendo
    # en realidad LINK. Se recalculan los campos del reporte (val/nombre/
    # metodo_de_pago/ci/program) + el incp de 9.2 — pero SOLO si el
    # documento del pago aparece en el reporte de ESTA corrida (el lookup no
    # se rehace para cubrir períodos históricos, solo el más reciente, ver
    # _cargar_lookup_wompi_reporte) y SOLO si nadie lo corrigió a mano
    # (corregido_manual=false) — si un humano lo tocó, no se pisa nunca,
    # aunque el reporte diga otra cosa. Es una excepción acotada a "las
    # filas terminales no se vuelven a tocar": esta sí re-evalúa filas ya
    # 'cruzado', pero solo estos campos — nunca estado_cruce/
    # excepcion_motivo, y nunca filas 'no_identificable'. Se corre siempre
    # que el reporte esté disponible, sin importar si hubo transacciones
    # nuevas para cruzar esta corrida.
    if wompi_reporte_disponible:
        log.info('Fase 9.4: re-evaluando filas WOMPI ya cruzadas (corregido_manual=false)...')
        actualizaciones_9_4 = []
        for r in existentes:
            if r.get('estado_cruce') != 'cruzado' or r.get('corregido_manual'):
                continue
            payment_method = str(r.get('payment_method') or '').upper()
            if not payment_method.startswith('WOMPI'):
                continue
            tx_id = str(r.get('matching_key') or '').strip()
            match = lookup_wompi_reporte.get(tx_id) if tx_id else None
            if not match:
                continue
            update = {
                'matching_key':   r['matching_key'],
                'val':            match.get('id_transaccion') or None,
                'nombre':         match.get('pagador') or None,
                'metodo_de_pago': WOMPI_GENERA_LINK_LABEL,
                'ci':             match.get('comprobante') or None,
                'program':        match.get('proyecto') or None,
            }
            incp_link = _resolver_incp_wompi_link(match.get('inscripcion'), id_inscripcion_por_base)
            if incp_link:
                update['incp'] = incp_link
            actualizaciones_9_4.append(update)

        if actualizaciones_9_4:
            update_cruce_valores(supabase_url, srk, actualizaciones_9_4)
        log.info('Fase 9.4: %d fila(s) WOMPI re-evaluadas contra el reporte.', len(actualizaciones_9_4))

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
