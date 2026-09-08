"""Los 3 archivos de referencia, y por qué archivarlos no puede frenar la corrida.

`sync_cartera.py` es el PRIMER script de la cadena (`sync_cartera && procesar_todos
&& cruzar && preventiva`), así que cualquier excepción que se le escape se lleva
por delante la ingesta de pagos del día.

El 2026-09-08 pasó exactamente eso: `Payu UC.xlsx` se cargó bien —10.686
inscripciones entraron— y al ir a archivarlo el depósito respondió 400, porque
la ruta ya la ocupaba el archivo del día anterior. La corrida murió ahí y
**152 pagos ($122.112.305) no ingresaron** por no poder mover un archivo que ya
estaba leído.
"""

import logging

import pytest

import sync_cartera
from utils.origen import Bandeja

BANDEJA = Bandeja(fuente='payu_uc')
ARCHIVO = {'id': 'entrada/payu_uc/Payu UC.xlsx', 'name': 'Payu UC.xlsx',
           'origen': 'deposito', 'fuente': 'payu_uc'}


@pytest.fixture
def bandeja_con_archivo(monkeypatch):
    monkeypatch.setattr(sync_cartera, 'mas_reciente', lambda _b: ARCHIVO)


def test_el_archivo_se_carga_y_se_archiva(bandeja_con_archivo, monkeypatch):
    archivados = []
    monkeypatch.setattr(sync_cartera, 'mover_a_historico',
                        lambda a, _b: archivados.append(a['name']) or True)

    sync_cartera._procesar_opcional('Payu UC.xlsx', BANDEJA, lambda _a: True)

    assert archivados == ['Payu UC.xlsx']


def test_no_poder_archivar_NO_frena_la_corrida(bandeja_con_archivo, monkeypatch, caplog):
    """El caso del 2026-09-08. La tabla de referencia ya quedó cargada antes de
    este paso, así que el precio de seguir es que el archivo se relea; el precio
    de cortar es que no entre ningún pago del día."""
    def _explota(_a, _b):
        raise RuntimeError('400 Client Error: Bad Request for url: .../object/move')

    monkeypatch.setattr(sync_cartera, 'mover_a_historico', _explota)

    with caplog.at_level(logging.ERROR):
        sync_cartera._procesar_opcional('Payu UC.xlsx', BANDEJA, lambda _a: True)

    # No se traga el problema: queda a gritos en el log, con el nombre adentro.
    assert any(r.levelno >= logging.ERROR and 'Payu UC.xlsx' in r.getMessage()
               for r in caplog.records)


def test_una_lectura_vacia_no_archiva_el_archivo(bandeja_con_archivo, monkeypatch):
    """Guardo viejo que este cambio no debe aflojar: si `cargar` dice que no
    tocó la tabla, el archivo se queda en su bandeja — archivarlo lo escondería
    sin haber servido para nada."""
    def _explota(_a, _b):
        raise AssertionError('no se debe archivar un archivo que no se pudo leer')

    monkeypatch.setattr(sync_cartera, 'mover_a_historico', _explota)

    sync_cartera._procesar_opcional('Payu UC.xlsx', BANDEJA, lambda _a: False)


def test_sin_archivo_en_la_bandeja_no_pasa_nada(monkeypatch):
    """Los 3 son opcionales: que no esté no es un error."""
    monkeypatch.setattr(sync_cartera, 'mas_reciente', lambda _b: None)
    monkeypatch.setattr(sync_cartera, 'mover_a_historico',
                        lambda *_a: (_ for _ in ()).throw(AssertionError('nada que archivar')))

    sync_cartera._procesar_opcional('Payu UC.xlsx', BANDEJA, lambda _a: True)
