"""De dónde sale un archivo: Google Drive o el depósito de la plataforma.

Por qué existe. Hasta hoy los archivos de bancos y pasarelas entraban por una
sola puerta —una carpeta de Drive por fuente— y los 4 módulos que los leen
llamaban directo a `utils/drive.py`. La carga de archivos desde
`financial-platform` agrega una segunda puerta, y sin una capa en medio cada uno
de esos módulos tendría que saber cuál mirar, en qué orden, y dónde archivar
después.

Qué resuelve. Este módulo expone las mismas operaciones de siempre (listar,
descargar, mover) sobre una **bandeja**, que es el par "carpeta de Drive +
carpeta del depósito" de una misma fuente. Los módulos siguen recorriendo una
lista de archivos y no se enteran de que hay dos sitios.

Lo que NO cambia, y es la mitad del valor: los parsers, la deduplicación, las
llaves, el apartado de cheques y todo el cruce siguen recibiendo exactamente lo
mismo. Un archivo tiene que producir los mismos pagos entre por donde entre.

El orden de lectura es **primero el depósito y después Drive**, en una sola
lista: decisión del usuario del 2026-09-05, Drive pasa a ser el camino
secundario mientras el área cambia su rutina. No es cosmético — para PayU el
orden de la lista decide qué archivo se empareja con cuál.

Si `DEPOSITO_BUCKET` no está configurada, el depósito se apaga entero y esto se
comporta igual que antes de que existiera.
"""

from __future__ import annotations

import io
import logging
import os
from dataclasses import dataclass

from utils import deposito as _deposito
from utils import drive as _drive

log = logging.getLogger(__name__)

# De dónde vino un archivo. Viaja dentro de cada diccionario de archivo para que
# `descargar` y `mover` sepan a quién preguntarle sin que el llamador se entere.
DRIVE = 'drive'
DEPOSITO = 'deposito'


@dataclass(frozen=True)
class Bandeja:
    """Una fuente y sus dos direcciones.

    `fuente` es el nombre corto ('wompi', 'bc2576', 'cartera_prev') y es además
    la carpeta dentro del depósito (`entrada/<fuente>/`), que por eso NO
    necesita una variable de entorno propia. ⚠️ Cambiarle el nombre a una fuente
    cambia dónde busca el pipeline, y tiene que coincidir con lo que escribe la
    pantalla de carga de `financial-platform`.

    Las dos de Drive son los identificadores de carpeta de siempre. Vacías
    significa "esta bandeja no tiene lado de Drive", que es lo que va a pasar
    el día que se apague.
    """

    fuente: str
    drive_entrada: str = ''
    drive_historico: str = ''


# El servicio de Drive es caro de construir (lee el JSON de la cuenta de
# servicio y arma el cliente), así que se hace una vez por corrida y se
# reutiliza. Antes cada módulo lo construía por su cuenta y se lo pasaba a las
# funciones; ahora vive acá y los llamadores no lo ven.
_servicio_drive = None


def _drive_svc():
    global _servicio_drive
    if _servicio_drive is None:
        _servicio_drive = _drive.build_drive_service(os.environ.get('GOOGLE_SA_JSON', ''))
    return _servicio_drive


def reiniciar() -> None:
    """Olvida el servicio cacheado. Existe para las pruebas, que cambian el
    entorno entre casos y no deben heredar el cliente del caso anterior."""
    global _servicio_drive
    _servicio_drive = None


def _como_archivos(items: list[dict], bandeja: Bandeja, origen: str) -> list[dict]:
    """Normaliza lo que devuelve un backend al diccionario que ven los módulos.

    Se conservan las claves `id` y `name` con el mismo significado de siempre
    —hay código que las lee directo— y se agregan `origen` y `fuente`.
    """
    return [{'id': f['id'], 'name': f['name'], 'origen': origen, 'fuente': bandeja.fuente}
            for f in items]


def listar(bandeja: Bandeja) -> list[dict]:
    """Todos los archivos de la bandeja, del más viejo al más reciente.

    El orden lo hereda de `utils/drive.py`, que ordena por `createdTime`
    ascendente y pagina hasta agotar la carpeta: sin paginar, los que se perdían
    eran justamente los últimos, o sea los más nuevos.
    """
    archivos: list[dict] = []

    # El depósito va PRIMERO: es el camino nuevo y Drive el de respaldo.
    if _deposito.activo():
        archivos += _como_archivos(_deposito.listar(bandeja.fuente), bandeja, DEPOSITO)

    if bandeja.drive_entrada:
        archivos += _como_archivos(_drive.list_files(_drive_svc(), bandeja.drive_entrada),
                                   bandeja, DRIVE)

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
    """Baja el contenido del archivo, venga de donde venga."""
    if archivo['origen'] == DRIVE:
        return _drive.download_pdf(_drive_svc(), archivo['id'])
    if archivo['origen'] == DEPOSITO:
        return _deposito.descargar(archivo['id'])
    raise ValueError(f'Origen desconocido: {archivo.get("origen")!r}')


def mover_a_historico(archivo: dict, bandeja: Bandeja) -> bool:
    """Archiva el archivo ya procesado. Devuelve si se movió.

    Un archivo se archiva en el mismo sitio del que salió: el que entró por
    Drive se va al Histórico de Drive, y el que entre por el depósito se irá al
    del depósito. Así los dos caminos pueden convivir sin pisarse.

    Sin carpeta de destino configurada NO se mueve y se avisa: dejarlo en la
    bandeja es molesto (se vuelve a leer la próxima corrida) pero es reversible;
    perderlo de vista no.

    ⚠️ La escritura pasa por `utils/drive.py`, que es donde vive el interruptor
    del modo simulación: en dry-run se registra la intención y el archivo se
    queda donde está. Mover un archivo al Histórico es de las escrituras que más
    duelen —si la corrida no lo procesó bien, moverlo lo esconde— así que ese
    freno no se puede saltar.
    """
    if archivo['origen'] == DRIVE:
        if not bandeja.drive_historico:
            log.warning('[%s] Sin carpeta de Histórico configurada, se deja en su sitio: %s',
                        bandeja.fuente, archivo['name'])
            return False
        _drive.move_file(_drive_svc(), archivo['id'], bandeja.drive_historico)
        return True

    if archivo['origen'] == DEPOSITO:
        # El destino no se configura: se deriva de la fuente, igual que la
        # entrada. Por eso el depósito no puede quedarse "sin histórico".
        _deposito.mover_a_historico(archivo['id'], archivo['fuente'], archivo['name'])
        return True

    raise ValueError(f'Origen desconocido: {archivo.get("origen")!r}')
