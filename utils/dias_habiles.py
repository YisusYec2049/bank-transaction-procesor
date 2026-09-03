"""Días hábiles en Colombia y la ventana de un pago antes del sello.

Existe por el requerimiento 3 del área (3 de septiembre de 2026):

    *"Dejar el proceso de 'sellado' para los pagos a dos días hábiles.
    Actualmente se está realizando una validación adicional a algunos pagos con
    el proceso de cartera y el proceso comercial. Esto nos puede generar algunos
    retrasos en la respuesta; por ende, procedemos a dejar pendiente ese caso
    para agregarlo en la cartera del siguiente día. Actualmente con el 'sellado'
    de un día si deseamos ver esos casos pendientes y asignarle la cartera no
    nos es posible."*

Hasta hoy la ventana era **un día calendario**: un pago entraba y la corrida del
día siguiente era la última que podía aplicarlo. Eso tiene un agujero que nadie
había mirado — **el sábado y el domingo gastaban margen igual que un martes**,
así que un pago que entraba el viernes se sellaba el domingo, sin que ninguna
persona hubiera tenido un solo día laborable para tocarlo.

Regla del usuario (3 de septiembre): *"los pagos pueden entrar sábado, domingo,
festivo, lo que dé la gana, de momento no se trabaja esos días, así que no
entran a la automatización hasta el siguiente día hábil o laboral"*.

Lo que rige ahora:

    Un pago sobrevive **dos corridas de día hábil** y se sella al terminar la
    segunda. Las corridas de sábado, domingo y festivo lo dejan pasar sin
    descontarle nada — y, de paso, dejan de sellar.

    | Llega   | Corridas que lo pueden aplicar | Se sella al terminar |
    |---------|--------------------------------|----------------------|
    | martes  | miércoles, jueves              | jueves               |
    | viernes | lunes, martes                  | martes               |
    | sábado  | lunes, martes                  | martes               |

El día en que llega **no** cuenta: casi siempre entra después de la corrida de
las 9:30, así que contarlo le regalaría un día que nadie pudo trabajar.

Medido antes de escribir esto, contra los 710 pagos sellados en producción:
**98 pagos ($104.817.149) se sellaron con menos de dos días hábiles de margen**,
16 de ellos bajo la cartera viva y 3 en la corrida del propio 3 de septiembre.
Decisión del usuario: el cambio aplica **de aquí en adelante**, los ya sellados
se quedan como están.

Los festivos NO son una lista que alguien mantenga: se calculan. Son de tres
clases y las tres son aritmética, sin tabla, sin archivo y sin red.

  1. **Fijos** — caen donde caigan, y por eso hay festivos en viernes (en 2026,
     el 1 de mayo, Boyacá y Navidad).
  2. **Movidos al lunes (Ley Emiliani, 51 de 1983)** — si no caen lunes, se
     corren al lunes siguiente. Son los que "siempre son lunes": el código sabe
     que ese lunes es festivo porque él mismo lo movió ahí.
  3. **Atados a la Pascua** — Jueves y Viernes Santo van pegados a ella; la
     Ascensión, el Corpus Christi y el Sagrado Corazón se cuentan desde la
     Pascua y además se corren al lunes.

Comprobación independiente: para 2026 esto da 18 festivos, con el **7 de agosto
en viernes** y el **17 en lunes** — los dos días hábiles sin movimiento que
quedaron anotados en el vault el 10 de agosto, detectados por otro camino.

⚠️ Lo único que no puede saber es un festivo extraordinario decretado por el
Congreso. No ocurre desde la Ley 51 de 1983, y si ocurriera el daño es acotado:
un pago se sellaría un día antes de lo que debía, no se pierde plata.
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import cache

# Cuántas corridas de día hábil sobrevive un pago antes de quedar sellado.
DIAS_HABILES_VENTANA = 2

# Festivos fijos: (mes, día). No se mueven nunca.
_FIJOS = (
    (1, 1),    # Año Nuevo
    (5, 1),    # Día del Trabajo
    (7, 20),   # Independencia
    (8, 7),    # Batalla de Boyacá
    (12, 8),   # Inmaculada Concepción
    (12, 25),  # Navidad
)

# Ley Emiliani: si no caen lunes, se corren al lunes siguiente.
_EMILIANI = (
    (1, 6),    # Reyes
    (3, 19),   # San José
    (6, 29),   # San Pedro y San Pablo
    (8, 15),   # Asunción
    (10, 12),  # Día de la Raza
    (11, 1),   # Todos los Santos
    (11, 11),  # Independencia de Cartagena
)

# Días desde la Pascua. `True` = además se corre al lunes.
_DESDE_PASCUA = (
    (-3, False),  # Jueves Santo
    (-2, False),  # Viernes Santo
    (43, True),   # Ascensión
    (64, True),   # Corpus Christi
    (71, True),   # Sagrado Corazón
)


def domingo_de_pascua(anio: int) -> date:
    """Algoritmo de Meeus/Butcher (cómputo gregoriano). Solo cuentas."""
    a = anio % 19
    b, c = divmod(anio, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    lo = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * lo) // 451
    mes, dia = divmod(h + lo - 7 * m + 114, 31)
    return date(anio, mes, dia + 1)


def _al_lunes(d: date) -> date:
    """Corre la fecha al lunes siguiente. Si ya es lunes, la deja quieta."""
    return d + timedelta(days=(7 - d.weekday()) % 7)


@cache
def festivos(anio: int) -> frozenset[date]:
    """Los 18 festivos colombianos de un año."""
    pascua = domingo_de_pascua(anio)
    dias = {date(anio, mes, dia) for mes, dia in _FIJOS}
    dias |= {_al_lunes(date(anio, mes, dia)) for mes, dia in _EMILIANI}
    for delta, mueve in _DESDE_PASCUA:
        d = pascua + timedelta(days=delta)
        dias.add(_al_lunes(d) if mueve else d)
    return frozenset(dias)


def es_habil(d: date) -> bool:
    """Lunes a viernes que no sea festivo."""
    return d.weekday() < 5 and d not in festivos(d.year)


def habiles_transcurridos(entrada: date, hoy: date) -> int:
    """Días hábiles que pasaron DESPUÉS de `entrada`, hasta `hoy` inclusive.

    El día de entrada no cuenta: el pago casi siempre llega después de la
    corrida de las 9:30, así que ese día nadie pudo trabajarlo.

    Devuelve 0 si `hoy` es anterior a `entrada` — un reloj no corre hacia atrás,
    y con fechas cruzadas es más seguro dejar el pago vivo que sellarlo.
    """
    if hoy <= entrada:
        return 0
    # Se recorre día a día en vez de calcular por semanas: el rango es de días,
    # no de años, y así los festivos entran sin ningún caso especial.
    n = 0
    d = entrada
    while d < hoy:
        d += timedelta(days=1)
        if es_habil(d):
            n += 1
    return n


def _a_fecha(valor) -> date | None:
    """Acepta `date`, datetime o texto ISO (con hora o sin ella)."""
    if valor is None:
        return None
    if isinstance(valor, date):
        return valor if not hasattr(valor, 'date') else valor.date()
    try:
        return date.fromisoformat(str(valor)[:10])
    except ValueError:
        return None


def ventana_abierta(entrada, hoy) -> bool:
    """¿Este pago todavía puede aplicarse solo en la corrida de `hoy`?

    Es la ÚNICA fuente de la ventana: la usan tanto el filtro que decide qué
    pagos entran al reparto como el cierre diario que los sella. Antes eran dos
    condiciones sueltas escritas por separado (`entrada in (hoy, ayer)` y
    `entrada < hoy`), y separadas se pueden desalinear: bastaría tocar una para
    dejar un pago que ya no se aplica solo pero tampoco se sella —o al revés—,
    y eso es plata muerta sin que nadie se entere.

    Abierta mientras no hayan pasado DIAS_HABILES_VENTANA días hábiles. En la
    corrida que los completa el pago sigue abierto —esa corrida todavía puede
    aplicarlo— y se sella al terminarla; de ahí que acá sea `<=` y en el sello
    `>=`, sobre el mismo número.

    Sin fecha de entrada se considera abierta: no hay dato con que decidir, y
    perder plata en silencio es peor que dejarla viva un día de más.
    """
    e = _a_fecha(entrada)
    if e is None:
        return True
    h = _a_fecha(hoy)
    if h is None:
        return True
    return habiles_transcurridos(e, h) <= DIAS_HABILES_VENTANA


def ventana_vencida(entrada, hoy) -> bool:
    """¿Al cerrar la corrida de `hoy` este pago se queda sin ventana?

    El complemento exacto de `ventana_abierta` sobre el mismo contador, salvo en
    el borde: la corrida que completa los días hábiles aplica Y sella.

    Sin fecha de entrada NO se sella: el sello es irreversible desde la
    pantalla, así que ante la duda se deja vivo.
    """
    e = _a_fecha(entrada)
    h = _a_fecha(hoy)
    if e is None or h is None:
        return False
    return habiles_transcurridos(e, h) >= DIAS_HABILES_VENTANA
