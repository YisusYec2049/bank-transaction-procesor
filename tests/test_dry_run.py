"""
El modo simulación tiene que frenar TODA escritura, no casi todas.

La prueba que más vale acá es la última (`test_toda_funcion_de_escritura_tiene_guarda`):
comprueba por introspección que ninguna función de escritura se quedó sin
guarda. Las otras verifican que la guarda funciona; esa verifica que no falte
ninguna, incluida la que alguien agregue dentro de seis meses sin haber leído
nada de esto.

Escribiendo esto ya apareció un defecto real: la guarda de `delete_by_keys`
nombraba un parámetro que no existe (`values` en vez de `keys`), o sea que el
dry-run reventaba con `NameError` justo en la ruta que solo se recorre en
dry-run. Es exactamente el tipo de error que este modo existe para evitar y que
solo se ve corriéndolo.
"""

import inspect

import pytest

from utils import drive, dry_run, sheets, supabase


@pytest.fixture
def simulacion(tmp_path):
    """Enciende el modo simulación y lo apaga al terminar.

    El apagado importa: el estado es global (que es lo que permite cubrir los
    19 puntos de escritura sin tocarlos), así que un test que lo deje encendido
    contaminaría a los siguientes.
    """
    salida = tmp_path / 'simulacion.jsonl'
    dry_run.activar('test', str(salida))
    yield salida
    dry_run.desactivar()


def _explotar(*_a, **_k):
    raise AssertionError('se intentó una llamada de red en modo simulación')


@pytest.fixture
def sin_red(monkeypatch):
    """Cualquier intento de salir a la red revienta el test."""
    for verbo in ('post', 'patch', 'get', 'delete', 'put'):
        monkeypatch.setattr(supabase.http, verbo, _explotar, raising=False)


def test_no_escribe_en_supabase(simulacion, sin_red):
    supabase.upsert_cruce('https://x', 'llave', [{'matching_key': 'a', 'incp': '1'}])
    supabase.upsert_cartera_preventiva('https://x', 'llave', [{'llave': 'L1'}])
    supabase.update_cruce_valores('https://x', 'llave', [{'matching_key': 'a', 'cruce': 'x'}])
    supabase.upsert_pago_asociaciones('https://x', 'llave', [{'matching_key': 'a', 'llave': 'L1'}])

    assert simulacion.exists()
    assert simulacion.read_text().count('\n') == 4


def test_delete_by_keys_no_revienta(simulacion, sin_red):
    """La guarda nombraba mal su parámetro; esto lo habría atrapado."""
    supabase.delete_by_keys('https://x', 'llave', 'cruce_cartera', 'matching_key', ['a', 'b'])
    assert 'cruce_cartera' in simulacion.read_text()


def test_staging_responde_que_si_reemplazo(simulacion, sin_red):
    """Devuelve un valor que el llamador usa para decidir.

    `sync_cartera.py` hace `ok = replace_cartera_preventiva_staging(...)` y con
    eso decide si registra la carga. Un `return` pelado daría None y en la
    simulación parecería que no se cargó nada — reportando lo contrario de lo
    que pasaría de verdad.
    """
    assert supabase.replace_cartera_preventiva_staging('https://x', 'k', [{'llave': 'L1'}]) is True


def test_no_mueve_archivos_en_drive(simulacion):
    """Mover a Histórico es la escritura más traicionera: si la corrida salió
    mal, mover el archivo esconde la evidencia."""
    class _DriveExplota:
        def files(self):
            raise AssertionError('se intentó tocar Drive en modo simulación')

    drive.move_file(_DriveExplota(), 'archivo-1', 'carpeta-destino')
    assert 'archivo-1' in simulacion.read_text()


def test_no_escribe_en_sheets(simulacion):
    class _SheetsExplota:
        def spreadsheets(self):
            raise AssertionError('se intentó tocar Sheets en modo simulación')

    sheets.append_rows(_SheetsExplota(), 'hoja-1', '02-08-2026', [[1, 2, 3]])
    assert 'sheet:02-08-2026' in simulacion.read_text()


def test_resumen_cuenta_las_filas(simulacion, sin_red):
    supabase.upsert_cruce('https://x', 'k', [{'matching_key': str(i)} for i in range(7)])
    supabase.upsert_cruce('https://x', 'k', [{'matching_key': 'z'}])

    resumen = dry_run.resumen()
    assert 'cruce_cartera.upsert' in resumen
    assert '8 fila(s) en 2 llamada(s)' in resumen


def test_apagado_no_intercepta(sin_red):
    """Con el modo apagado, la escritura tiene que intentarse de verdad.

    Si una guarda se quedara activa por accidente, el pipeline dejaría de
    escribir en producción sin decir nada — el peor desenlace posible para una
    herramienta cuyo propósito es evitar fallas silenciosas.
    """
    dry_run.desactivar()
    with pytest.raises(AssertionError, match='llamada de red'):
        supabase.upsert_cruce('https://x', 'k', [{'matching_key': 'a'}])


# Funciones de `utils/supabase.py` que NO escriben, y por eso no llevan guarda.
_SOLO_LECTURA = {'select_all', 'existing_matching_keys'}


def test_toda_funcion_de_escritura_tiene_guarda():
    """Ninguna función que escriba puede quedarse sin guarda.

    Es la prueba estructural: no comprueba un comportamiento, comprueba que la
    garantía siga siendo cierta para el código que todavía no existe. Sin esto,
    la próxima función de escritura entra sin guarda y el dry-run pasa a mentir
    en silencio — que es justo el patrón que este trabajo intenta cerrar.
    """
    sin_guarda = []
    for nombre, fn in inspect.getmembers(supabase, inspect.isfunction):
        if nombre.startswith('_') or nombre in _SOLO_LECTURA:
            continue
        if fn.__module__ != supabase.__name__:
            continue
        if 'dry_run.registrar' not in inspect.getsource(fn):
            sin_guarda.append(nombre)

    assert not sin_guarda, (
        f'estas funciones escriben sin pasar por el modo simulación: {sin_guarda}. '
        f'Agregarles `if dry_run.registrar(<tabla>, <op>, <filas>): return`, '
        f'o incluirlas en _SOLO_LECTURA si de verdad solo leen.'
    )
