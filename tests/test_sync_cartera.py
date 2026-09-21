"""Los 3 archivos de referencia: cuándo se frena la corrida y cuándo no.

`sync_cartera.py` es el PRIMER script de la cadena (`sync_cartera && procesar_todos
&& cruzar && preventiva`), así que lo que haga con su código de salida decide si
entran los pagos del día. Hay dos reglas que tiran para lados opuestos y las dos
se ganaron su lugar con un incidente real:

1. **Archivar NO puede frenar la corrida.** El 2026-09-08 `Payu UC.xlsx` se cargó
   bien —10.686 inscripciones entraron— y al ir a archivarlo el depósito
   respondió 400, porque la ruta ya la ocupaba el archivo del día anterior. La
   corrida murió ahí y **152 pagos ($122.112.305) no ingresaron** por no poder
   mover un archivo que ya estaba leído.

2. **Un archivo que se subió y NO se puede leer SÍ frena la corrida.** El
   2026-09-21 subieron a la bandeja de Payu UC un Excel que no era, y esa vez
   frenar fue lo correcto: repartir los pagos del día contra la lista de
   inscripciones de ayer es aplicar plata sobre información vieja, y un pago se
   reparte una sola vez en su vida. Lo que estaba mal era CÓMO frenaba —una
   excepción que se escapaba, sin mensaje y sin rastro en ninguna pantalla.

La diferencia entre las dos no es el archivo sino el momento: antes de cargar la
tabla de referencia, lo que hay en la base todavía no sirve; después, ya sirve.
"""

import io
import logging

import openpyxl
import pytest

import sync_cartera
from utils.excel_cartera import read_bancolombia_2833
from utils.origen import Bandeja

BANDEJA = Bandeja(fuente='payu_uc')
ARCHIVO = {'id': 'entrada/payu_uc/Payu UC.xlsx', 'name': 'Payu UC.xlsx',
           'origen': 'deposito', 'fuente': 'payu_uc'}


@pytest.fixture
def bandeja_con_archivo(monkeypatch):
    monkeypatch.setattr(sync_cartera, 'mas_reciente', lambda _b: ARCHIVO)


@pytest.fixture
def registro_anotado(monkeypatch):
    """Lo que el script deja escrito en `archivos_procesados`, que es de donde
    la pantalla saca el rojo y el motivo."""
    anotados = []
    monkeypatch.setattr(sync_cartera.registro, 'anotar',
                        lambda archivo, **kw: anotados.append({'archivo': archivo, **kw}))
    return anotados


def test_el_archivo_se_carga_y_se_archiva(bandeja_con_archivo, registro_anotado, monkeypatch):
    archivados = []
    monkeypatch.setattr(sync_cartera, 'mover_a_historico',
                        lambda a, _b: archivados.append(a['name']) or True)

    fallo = sync_cartera._procesar_opcional('Payu UC.xlsx', BANDEJA, lambda _a: 11089)

    assert fallo is None
    assert archivados == ['Payu UC.xlsx']
    # Queda anotado con cuántas filas trajo: es lo que delata un archivo
    # cortado a la mitad sin que nadie abra el Excel.
    assert registro_anotado == [{'archivo': ARCHIVO, 'filas_leidas': 11089, 'resultado': 'ok'}]


def test_no_poder_archivar_NO_frena_la_corrida(bandeja_con_archivo, registro_anotado,
                                               monkeypatch, caplog):
    """El caso del 2026-09-08. La tabla de referencia ya quedó cargada antes de
    este paso, así que el precio de seguir es que el archivo se relea; el precio
    de cortar es que no entre ningún pago del día."""
    def _explota(_a, _b):
        raise RuntimeError('400 Client Error: Bad Request for url: .../object/move')

    monkeypatch.setattr(sync_cartera, 'mover_a_historico', _explota)

    with caplog.at_level(logging.ERROR):
        fallo = sync_cartera._procesar_opcional('Payu UC.xlsx', BANDEJA, lambda _a: 11089)

    assert fallo is None  # la cadena sigue
    # No se traga el problema: queda a gritos en el log, con el nombre adentro.
    assert any(r.levelno >= logging.ERROR and 'Payu UC.xlsx' in r.getMessage()
               for r in caplog.records)


def test_un_archivo_ilegible_frena_la_corrida(bandeja_con_archivo, registro_anotado, monkeypatch):
    """El caso del 2026-09-21: subieron a la bandeja de Payu UC un Excel que no
    era, sin la hoja `Inscrip`. Tres cosas tienen que pasar a la vez, y cada una
    cubre un agujero distinto."""
    def _no_se_puede_leer(_a):
        raise KeyError('Worksheet Inscrip does not exist.')

    monkeypatch.setattr(sync_cartera, 'mover_a_historico',
                        lambda *_a: (_ for _ in ()).throw(
                            AssertionError('un archivo que no se pudo leer NO se archiva: '
                                           'esconderlo deja al área sin cómo reemplazarlo')))

    fallo = sync_cartera._procesar_opcional('Payu UC.xlsx', BANDEJA, _no_se_puede_leer)

    # 1. Se reporta el fallo, que es lo que hace que la cadena no siga.
    assert fallo is not None
    assert fallo['archivo'] == ARCHIVO
    assert 'Inscrip' in fallo['motivo']
    # 2. Queda en el registro que mira la pantalla, con el motivo.
    assert len(registro_anotado) == 1
    assert registro_anotado[0]['resultado'] == 'error'
    assert 'Inscrip' in registro_anotado[0]['detalle']
    # 3. (el archivo no se archivó, lo verifica el monkeypatch de arriba)


def test_una_lectura_vacia_no_archiva_el_archivo_ni_frena(bandeja_con_archivo, registro_anotado,
                                                          monkeypatch):
    """Guardo viejo que este cambio no debe aflojar: si `cargar` dice que no
    tocó la tabla, el archivo se queda en su bandeja — archivarlo lo escondería
    sin haber servido para nada.

    Y **no frena la corrida**: un archivo que se leyó y vino vacío no es un
    problema de formato. La regla es "no se pudo leer", no "no trajo nada"."""
    monkeypatch.setattr(sync_cartera, 'mover_a_historico',
                        lambda *_a: (_ for _ in ()).throw(
                            AssertionError('no se debe archivar un archivo que no cargó nada')))

    fallo = sync_cartera._procesar_opcional('Payu UC.xlsx', BANDEJA, lambda _a: 0)

    assert fallo is None
    assert registro_anotado == []


def test_sin_archivo_en_la_bandeja_no_pasa_nada(registro_anotado, monkeypatch):
    """Los 3 son opcionales: que no esté NO es un error y no frena nada.

    Es la otra mitad de la regla, y la que evita que el freno se vuelva
    insoportable: Ingresos PSE y PAYU se sube unos días sí y otros no."""
    monkeypatch.setattr(sync_cartera, 'mas_reciente', lambda _b: None)
    monkeypatch.setattr(sync_cartera, 'mover_a_historico',
                        lambda *_a: (_ for _ in ()).throw(AssertionError('nada que archivar')))

    assert sync_cartera._procesar_opcional('Payu UC.xlsx', BANDEJA, lambda _a: 1) is None
    assert registro_anotado == []


# ── La hoja de Bancolombia 2833 dentro de "Ingresos PSE y PAYU" ──────────────
#
# Caso real del 2026-09-18: el área la renombró de `BANCOL 2833` a
# `PREBANCOLOMBIA 2833`. El archivo entero seguía cargando —sus otras 3 hojas se
# leen perfecto—, así que nadie vio nada; simplemente esa libreta dejó de
# actualizarse, y es la ÚNICA señal de identidad que tienen los pagos de esa
# cuenta (muchos llegan por NEQUI sin documento, solo con un nombre suelto).
# Cuatro días después el sistema seguía usando la copia del 14 de septiembre.


def _ingresos_con_hoja_2833(nombre_hoja: str) -> io.BytesIO:
    """Un 'Ingresos PSE y PAYU' mínimo, con la hoja de 2833 nombrada a pedido.

    Se arma acá en vez de guardar un Excel de muestra porque lo que se prueba es
    justamente el NOMBRE de la hoja, y un fixture congelado lo dejaría fuera de
    la vista."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = nombre_hoja
    ws.append(['BANCO 2833'])  # el título que ocupa la fila 1 del Excel real
    ws.append(['Fecha', 'DESCRIPCIÓN', 'SUCURSAL/CANAL', 'REFERENCIA 1',
               'REFERENCIA 2', 'valor', 'N° de Inscripción'])
    ws.append(['2026-09-19', 'PAGO INTERBANC', 'AVENIDA 19.', '73014917834',
               '73014917834', '960232', '5734PN'])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def test_la_hoja_de_2833_se_lee_normal():
    filas = read_bancolombia_2833(_ingresos_con_hoja_2833('BANCOL 2833'))

    assert [f['referencia_1'] for f in filas] == ['73014917834']


def test_una_hoja_de_2833_renombrada_frena_la_corrida():
    """Antes del 2026-09-21 esto devolvía [] y la corrida seguía tan campante.

    El peor caso que justificaba tragárselo —"los pagos de 2833 se quedan en
    Excepciones"— resultó ser optimista: lo que pasa de verdad es que el área
    sigue anotando en una hoja que el sistema ya no lee, y no hay nada en
    ninguna pantalla que lo diga."""
    with pytest.raises(ValueError, match='BANCOL 2833'):
        read_bancolombia_2833(_ingresos_con_hoja_2833('PREBANCOLOMBIA 2833'))
