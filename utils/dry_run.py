"""
Modo simulación: correr el pipeline entero contra producción **en lectura**,
registrando lo que escribiría en vez de escribirlo.

Por qué existe. La lista de bugs de este repo tiene una forma común: `cheque_logic`
roto un mes, la Fase 4 que nunca aplicó un pago, el motor caído un día entero por
un upsert parcial, el dedup que se comía pagos reales. Ninguno lo atrapó una
prueba — los encontró el equipo mirando pantallas, días o semanas después. La
causa de fondo no era la falta de cuidado: **no había forma de correr esto y ver
qué haría sin que lo hiciera.**

Dónde está enchufado, y por qué ahí. El interruptor vive en las dos puertas al
exterior (`utils/supabase.py` y `utils/drive.py`), no en cada llamada de los
scripts. Entre `cruzar.py` y `cruzar_cartera_preventiva.py` hay 19 puntos de
escritura; envolver cada uno sería invasivo y, sobre todo, se olvidaría el
próximo que se agregue. En la puerta no se escapa ninguna, ni las de mañana.

Qué NO hace, a propósito:

* **No intercepta lecturas.** La gracia es correr contra los datos de verdad; un
  dry-run sobre datos inventados no dice nada sobre lo que pasaría hoy.
* **No garantiza que el resultado sea correcto**, solo lo hace visible. Sigue
  haciendo falta leerlo.

Uso:

    python cruzar.py --dry-run
    python cruzar.py --dry-run --dry-run-salida /tmp/hoy.jsonl

Sin `--dry-run-salida` va a `logs/dry-run-<script>-<fecha>.jsonl`. El formato es
una operación por línea, para poder comparar dos corridas con `diff` — que es
justo lo que se quiere antes y después de un cambio de rendimiento.
"""

import json
import logging
import os
from collections import Counter
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

_activo = False
_salida: Path | None = None
_conteo: Counter = Counter()
_filas: Counter = Counter()


def activar(nombre_script: str, ruta: str | None = None) -> Path:
    """Enciende el modo simulación para toda la corrida."""
    global _activo, _salida
    _activo = True
    _conteo.clear()
    _filas.clear()

    if ruta:
        _salida = Path(ruta)
    else:
        marca = datetime.now().strftime('%Y%m%d-%H%M%S')
        _salida = Path('logs') / f'dry-run-{nombre_script}-{marca}.jsonl'

    _salida.parent.mkdir(parents=True, exist_ok=True)
    _salida.write_text('', encoding='utf-8')

    log.warning('*** DRY-RUN: no se escribirá nada. Detalle en %s ***', _salida)
    return _salida


def activo() -> bool:
    return _activo


def registrar(destino: str, operacion: str, filas) -> bool:
    """Anota una escritura que NO se va a hacer. Devuelve True si el llamador
    debe cortar ahí.

    Devuelve un booleano en vez de lanzar una excepción para que la línea en
    cada función de escritura sea una sola y se lea de corrido:

        if dry_run.registrar('cruce_cartera', 'upsert', rows):
            return
    """
    if not _activo:
        return False

    if isinstance(filas, (list, tuple)):
        cantidad, muestra = len(filas), list(filas[:3])
    elif filas is None:
        cantidad, muestra = 0, []
    else:
        cantidad, muestra = 1, [filas]

    _conteo[f'{destino}.{operacion}'] += 1
    _filas[f'{destino}.{operacion}'] += cantidad

    if _salida is not None:
        with _salida.open('a', encoding='utf-8') as fh:
            fh.write(json.dumps({
                'destino': destino,
                'operacion': operacion,
                'filas': cantidad,
                'muestra': muestra,
            }, default=str, ensure_ascii=False) + '\n')

    log.info('[dry-run] %s %s: %d fila(s) — no escritas', operacion, destino, cantidad)
    return True


def resumen() -> str:
    """Una tabla corta de todo lo que se habría escrito. Va al final del log."""
    if not _activo:
        return ''
    if not _conteo:
        return 'DRY-RUN: la corrida no habría escrito nada.'

    lineas = ['DRY-RUN — lo que esta corrida habría escrito:']
    for clave in sorted(_conteo):
        lineas.append(f'  {clave:52} {_filas[clave]:>7} fila(s) en {_conteo[clave]} llamada(s)')
    lineas.append(f'  {"TOTAL":52} {sum(_filas.values()):>7} fila(s)')
    if _salida:
        lineas.append(f'  detalle: {_salida}')
    return '\n'.join(lineas)


def agregar_flags(parser) -> None:
    """Los dos flags, iguales en los cuatro scripts."""
    parser.add_argument('--dry-run', action='store_true',
                        help='No escribe nada: registra lo que haría y lo resume al final.')
    parser.add_argument('--dry-run-salida', metavar='RUTA',
                        help='Dónde dejar el detalle (por defecto logs/dry-run-*.jsonl).')


def desde_args(args, nombre_script: str) -> None:
    """Enciende el modo si los flags lo piden. Se llama al empezar `main()`."""
    if getattr(args, 'dry_run', False):
        activar(nombre_script, getattr(args, 'dry_run_salida', None))


def desactivar() -> None:
    """Solo para los tests: deja el módulo como estaba.

    Hace falta porque el estado es global (que es justo lo que permite no tocar
    los 19 puntos de escritura), y un test que lo encienda se lo dejaría
    encendido al siguiente.
    """
    global _activo, _salida
    _activo = False
    _salida = None
    _conteo.clear()
    _filas.clear()


# Permite encenderlo sin tocar la línea de comandos, útil desde el cron o un
# contenedor: MATCHING_DRY_RUN=1
if os.environ.get('MATCHING_DRY_RUN') == '1':
    activar(os.environ.get('MATCHING_DRY_RUN_NOMBRE', 'pipeline'),
            os.environ.get('MATCHING_DRY_RUN_SALIDA'))
