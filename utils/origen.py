"""De dónde salen los archivos: el depósito de la plataforma, y nada más.

Por qué existe. Hasta el 2026-09-05 los archivos entraban por una sola puerta
—una carpeta de Google Drive por fuente— y los 4 módulos que los leen llamaban
directo a `utils/drive.py`. La carga de archivos desde `financial-platform`
agregó una segunda puerta, y esta capa nació para que ninguno de esos módulos
tuviera que saber cuál mirar, en qué orden, y dónde archivar después.

**Drive se desconectó el 2026-09-08** (decisión del usuario: *"todo pasa dentro
de la plataforma"*), después de dos días de convivencia que dejaron tres
incidentes en uno solo:

  1. Un archivo que se archiva con el mismo nombre del día anterior chocaba, y
     la corrida entera moría — 152 pagos ($122.112.305) no ingresaron.
  2. Al juntar los archivos de los dos sitios, el ÚLTIMO de la lista dejó de
     ser el más nuevo, y el pipeline tomó como vigente un ReportePagosWompi de
     Drive de 4 días antes, archivando el del día.
  3. En 8 lugares el código preguntaba "¿hay carpeta de Drive?" para decidir si
     había fuente, así que apagar Drive equivalía a apagar el pipeline.

Esta capa se queda igual —los módulos siguen pidiendo archivos POR BANDEJA— y
lo que desapareció es el segundo sitio. Sigue existiendo porque es lo que
mantiene a los 4 módulos sin saber cómo se guarda un archivo, y porque es donde
viviría una tercera puerta el día que haga falta.

Lo que NO cambió, y es la mitad del valor: los parsers, la deduplicación, las
llaves, el apartado de cheques y todo el cruce reciben exactamente lo mismo.

⚠️ `utils/drive.py` sigue en el repo a propósito, pero YA NO LO USA EL PIPELINE:
los Históricos de Drive guardan todos los archivos anteriores al 2026-09-08 y
hubo tres ocasiones en que hubo que releerlos. Es una herramienta suelta para
un script puntual, no una fuente.

Si `DEPOSITO_BUCKET` no está configurada, el depósito se apaga entero y este
módulo no devuelve ningún archivo — el pipeline corre sin procesar nada y lo
grita en el log.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from utils import deposito as _deposito

log = logging.getLogger(__name__)

# De dónde vino un archivo. Viaja dentro de cada diccionario de archivo para que
# `descargar` y `mover` sepan a quién preguntarle sin que el llamador se entere.
# Hoy hay un solo valor posible; se conserva la clave porque es lo que hace que
# agregar una puerta nueva no obligue a tocar los 4 módulos que leen archivos.
DEPOSITO = 'deposito'


@dataclass(frozen=True)
class Bandeja:
    """Una fuente de archivos.

    `fuente` es el nombre corto ('wompi', 'bc2576', 'cartera_prev') y es además
    la carpeta dentro del depósito (`entrada/<fuente>/`), que por eso NO
    necesita una variable de entorno propia. ⚠️ Cambiarle el nombre a una fuente
    cambia dónde busca el pipeline, y tiene que coincidir con lo que escribe la
    pantalla de carga de `financial-platform`.
    """

    fuente: str


def _como_archivos(items: list[dict], bandeja: Bandeja) -> list[dict]:
    """Normaliza lo que devuelve el depósito al diccionario que ven los módulos.

    Se conservan las claves `id` y `name` con el mismo significado de siempre
    —hay código que las lee directo— y se agregan `origen`, `fuente` y `fecha`.
    """
    return [{'id': f['id'], 'name': f['name'], 'origen': DEPOSITO,
             'fuente': bandeja.fuente, 'fecha': f.get('created_at')}
            for f in items]


def hay_de_donde_leer(bandeja: Bandeja) -> bool:
    """¿Hay de dónde leer los archivos de esta bandeja?

    Es la pregunta que decide si un módulo procesa una fuente o la saltea. Antes
    cada uno preguntaba por su carpeta de Drive, y esa pregunta se volvió la
    equivocada el día que Drive dejó de ser la fuente: los 3 sitios de
    `procesar_todos.py` se habrían salteado TODOS los bancos y pasarelas, y
    `sync_cartera.py` habría cortado la corrida entera.

    ⚠️ La respuesta NO depende de la bandeja sino del depósito, y eso es a
    propósito: una fuente sin archivos hoy sigue teniendo de dónde leer. "No hay
    archivos" y "no hay de dónde leerlos" son dos cosas distintas, y confundirlas
    es lo que hacía el código viejo.
    """
    return _deposito.activo()


def _cuando(archivo: dict) -> datetime:
    """La fecha del archivo, para ordenarlos del más viejo al más reciente.

    Sin fecha se lo trata como lo MÁS VIEJO: así un archivo del que no se sabe
    nada nunca se hace pasar por el más reciente, que es la decisión que puede
    hacer daño (cargar un Excel viejo encima de la tabla, o dejar un
    ReportePagosWompi vencido como el vigente).
    """
    crudo = archivo.get('fecha')
    if not crudo:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(crudo).replace('Z', '+00:00'))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def listar(bandeja: Bandeja) -> list[dict]:
    """Los archivos de la bandeja sin procesar, del más viejo al más reciente.

    ⚠️ El orden se ordena acá por FECHA y no se hereda del almacenamiento. Hay
    dos sitios que leen el último de la lista como si fuera el más nuevo
    (`mas_reciente`, y el archivado del ReportePagosWompi en `cruzar.py`), y el
    2026-09-08 un orden que no era por fecha dejó vigente un reporte de 4 días
    antes. Ordenar acá es lo que hace que esa lectura sea siempre cierta.
    """
    if not _deposito.activo():
        return []
    archivos = _como_archivos(_deposito.listar(bandeja.fuente), bandeja)
    archivos.sort(key=_cuando)
    return archivos


def mas_reciente(bandeja: Bandeja) -> dict | None:
    """El archivo más nuevo de la bandeja, sin importar cómo se llame.

    Es lo que usan los 3 archivos de referencia: cada carpeta está dedicada a un
    solo tipo, así que cualquier archivo que caiga ahí ES ese tipo (decisión del
    usuario del 2026-07-21).
    """
    archivos = listar(bandeja)
    return archivos[-1] if archivos else None


def todos_los_que_contienen(bandeja: Bandeja, texto: str) -> list[dict]:
    """Los archivos cuyo nombre contiene `texto`, del más viejo al más reciente.

    Existe para el ReportePagosWompi: leer solo el más reciente hace que, cuando
    se suben varias entregas juntas (el lunes con el fin de semana), las demás
    no se lean NUNCA y sus pagos por link queden como manuales para siempre.
    """
    buscado = texto.strip().lower()
    return [a for a in listar(bandeja) if buscado in a['name'].strip().lower()]


def descargar(archivo: dict) -> io.BytesIO:
    """Baja el contenido del archivo."""
    if archivo['origen'] == DEPOSITO:
        return _deposito.descargar(archivo['id'])
    raise ValueError(f'Origen desconocido: {archivo.get("origen")!r}')


def mover_a_historico(archivo: dict, bandeja: Bandeja) -> bool:
    """Archiva el archivo ya procesado. Devuelve si se movió.

    ⚠️ El destino NO se configura: se deriva de la fuente, igual que la entrada.
    Por eso una bandeja del depósito no puede quedarse "sin histórico" —que era
    la forma en que un archivo de Drive terminaba releyéndose para siempre— y
    por eso el vigilante puede vigilar cualquier bandeja sin entrar en bucle.

    El modo simulación frena esta escritura dentro de `utils/deposito.py`: mover
    un archivo al Histórico es de las que más duelen, porque si la corrida no lo
    procesó bien, moverlo lo esconde.
    """
    if archivo['origen'] == DEPOSITO:
        _deposito.mover_a_historico(archivo['id'], archivo['fuente'], archivo['name'])
        return True

    raise ValueError(f'Origen desconocido: {archivo.get("origen")!r}')
