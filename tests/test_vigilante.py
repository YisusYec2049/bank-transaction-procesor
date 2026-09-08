"""El vigilante: qué mira, y por qué su código de salida es delicado.

Responde con el CÓDIGO DE SALIDA: 0 = "hay trabajo, corré la cadena", 1 = "no
hay nada". Por eso sus dos formas de fallar son caras y opuestas:

  · Decir 0 cuando no hay nada → dispara la cadena completa cada 15 minutos
    para siempre. Es la forma exacta del atasco de Stripe.
  · Decir 1 cuando sí hay algo → un archivo subido tarde no se procesa nunca,
    en silencio. Es justo lo que el vigilante existe para evitar.

Al desconectar Drive (2026-09-08) el código caía en las dos a la vez: sin la
credencial de Google salía con 0, y como su lista de bandejas se armaba con las
carpetas de Drive configuradas, sin ellas se quedaba mirando una lista VACÍA y
habría salido con 1 aunque el área hubiera subido todo.
"""

import pytest

import vigilante
from utils.origen import Bandeja


@pytest.fixture
def deposito(monkeypatch):
    """El depósito encendido, con los archivos que se le pasen por fuente."""
    def _con(archivos_por_fuente):
        monkeypatch.setattr(vigilante.deposito, 'activo', lambda: True)
        monkeypatch.setattr(vigilante, 'listar',
                            lambda b: archivos_por_fuente.get(b.fuente, []))
        monkeypatch.setattr(vigilante, 'todos_los_que_contienen',
                            lambda b, _t: archivos_por_fuente.get(b.fuente, []))
    return _con


@pytest.fixture
def sin_dotenv(monkeypatch):
    """`main()` lee el .env de la máquina; una prueba que depende del entorno de
    quien la corre no prueba lo mismo en dos máquinas."""
    monkeypatch.setattr(vigilante.sys, 'argv', ['vigilante.py'])
    monkeypatch.setattr(vigilante, 'load_dotenv', lambda *_a, **_k: None)


def test_un_archivo_subido_a_la_plataforma_ES_trabajo(deposito):
    """Lo que se rompía al apagar Drive: la lista de bandejas salía de las
    carpetas de Drive, así que sin ellas el vigilante no miraba nada y decía
    "no hay nada" con archivos esperando en la plataforma."""
    deposito({'bc2576': [{'name': 'extracto.pdf'}]})

    assert vigilante.hay_trabajo() is True


def test_sin_archivos_no_hay_trabajo(deposito):
    deposito({})

    assert vigilante.hay_trabajo() is False


def test_tambien_se_vigilan_los_archivos_de_referencia(deposito):
    """En el depósito una bandeja de referencia SIEMPRE es segura de vigilar:
    su destino de archivado se deriva de la fuente, así que no puede quedarse
    sin dónde archivar y entrar en bucle — que era la condición que en Drive
    obligaba a exigir una carpeta de Histórico configurada."""
    deposito({'payu_uc': [{'name': 'Payu UC.xlsx'}]})

    assert vigilante.hay_trabajo() is True


def test_cartera_preventiva_sigue_FUERA_del_vigilante(deposito):
    """Decisión del usuario (2026-08-10): esa la trae el botón "Buscar archivos
    nuevos", que corre solo `sync_cartera.py` (~4 s) en vez de la cadena entera.
    Vigilarla haría que subir una cartera dispare el pipeline completo sin que
    nadie lo haya pedido."""
    deposito({'cartera_prev': [{'name': 'CARTERA PREVENTIVA AGOSTO.xlsx'}]})

    assert vigilante.hay_trabajo() is False


def test_UN_solo_reporte_de_wompi_no_es_trabajo(deposito):
    """Su bandeja conserva a propósito el más reciente (ver
    `_archivar_reportes_wompi` en cruzar.py), así que "tener un archivo" es el
    estado normal y no puede ser la señal. La señal es tener DOS O MÁS."""
    deposito({'wompi_reporte': [{'name': 'ReportePagosWompi_20260908.xlsx'}]})

    assert vigilante.hay_trabajo() is False


def test_DOS_reportes_de_wompi_si_lo_son(deposito):
    deposito({'wompi_reporte': [{'name': 'ReportePagosWompi_20260907.xlsx'},
                                {'name': 'ReportePagosWompi_20260908.xlsx'}]})

    assert vigilante.hay_trabajo() is True


def test_sin_deposito_NO_se_dispara_la_cadena(monkeypatch, sin_dotenv):
    """Sin depósito no hay dónde mirar. Salir con 0 "por las dudas" es disparar
    la cadena completa cada 15 minutos para siempre, sin que nada espere."""
    monkeypatch.setattr(vigilante.deposito, 'activo', lambda: False)

    with pytest.raises(SystemExit) as salida:
        vigilante.main()

    assert salida.value.code == 1


def test_un_fallo_del_almacenamiento_SI_deja_pasar_la_cadena(monkeypatch, deposito, sin_dotenv):
    """Distinto del caso anterior: acá hay depósito y no se pudo consultar. Una
    corrida de más es idempotente y está protegida por el candado; un cargue que
    se queda sin procesar, no."""
    deposito({})
    monkeypatch.setattr(vigilante, 'hay_trabajo',
                        lambda: (_ for _ in ()).throw(ConnectionError('sin red')))

    with pytest.raises(SystemExit) as salida:
        vigilante.main()

    assert salida.value.code == 0


def test_sin_deposito_no_se_vigila_ninguna_bandeja(monkeypatch):
    monkeypatch.setattr(vigilante.deposito, 'activo', lambda: False)

    assert vigilante._bandejas() == []
    assert vigilante._carpetas_referencia() == []
    assert vigilante.hay_de_donde_leer(Bandeja(fuente='bc2576')) is False
