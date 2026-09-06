"""La capa que decide de dónde sale un archivo.

Las pruebas de ingesta sustituyen `listar`/`descargar`/`mover_a_historico`
enteras, así que sin este archivo la capa nueva no tendría ninguna prueba: el
día que alguien rompa el despacho por origen, todo seguiría en verde.

Acá se prueba la capa contra un Drive simulado, que es la única dependencia
externa que tiene hoy.
"""

import io

import pytest

from utils import origen


class _DriveFalso:
    """Se hace pasar por `utils.drive`, contando lo que le piden."""

    def __init__(self, archivos=None):
        self.archivos = archivos if archivos is not None else []
        self.descargados: list[str] = []
        self.movidos: list[tuple[str, str]] = []

    def build_drive_service(self, _sa_json):
        return 'servicio'

    def list_files(self, _svc, folder_id):
        assert folder_id, 'no se debe listar una carpeta vacía'
        return self.archivos

    def download_pdf(self, _svc, file_id):
        self.descargados.append(file_id)
        return io.BytesIO(b'contenido')

    def move_file(self, _svc, file_id, destino):
        self.movidos.append((file_id, destino))


@pytest.fixture
def drive(monkeypatch):
    falso = _DriveFalso([
        {'id': 'a', 'name': 'extracto viejo.pdf'},
        {'id': 'b', 'name': 'ReportePagosWompi_20260903.xlsx'},
    ])
    monkeypatch.setattr(origen, '_drive', falso)
    origen.reiniciar()
    yield falso
    origen.reiniciar()


BANDEJA = origen.Bandeja(fuente='wompi', drive_entrada='carpeta-in',
                         drive_historico='carpeta-hist')


def test_cada_archivo_dice_de_donde_vino(drive):
    """`origen` y `fuente` son lo que después decide a quién se le pide el
    contenido y dónde se archiva. Sin eso, `descargar` no sabría a quién ir."""
    archivos = origen.listar(BANDEJA)

    assert [a['id'] for a in archivos] == ['a', 'b']
    assert all(a['origen'] == origen.DRIVE for a in archivos)
    assert all(a['fuente'] == 'wompi' for a in archivos)


def test_una_bandeja_sin_lado_de_drive_no_lista_nada(drive):
    """El día que se apague Drive, las bandejas se quedan sin ese lado y esto
    tiene que devolver vacío en vez de reventar."""
    assert origen.listar(origen.Bandeja(fuente='wompi')) == []


def test_el_mas_reciente_es_el_ultimo(drive):
    """La lista viene del más viejo al más nuevo — es de lo que dependen los 3
    archivos de referencia, que toman el último que haya en su carpeta."""
    assert origen.mas_reciente(BANDEJA)['id'] == 'b'


def test_sin_archivos_no_hay_mas_reciente(monkeypatch):
    monkeypatch.setattr(origen, '_drive', _DriveFalso([]))
    origen.reiniciar()
    assert origen.mas_reciente(BANDEJA) is None


def test_se_filtra_por_lo_que_contiene_el_nombre(drive):
    encontrados = origen.todos_los_que_contienen(BANDEJA, 'reportepagoswompi')
    assert [a['id'] for a in encontrados] == ['b']


def test_descargar_le_pide_el_contenido_a_drive(drive):
    archivo = origen.listar(BANDEJA)[0]
    assert origen.descargar(archivo).read() == b'contenido'
    assert drive.descargados == ['a']


def test_archivar_manda_el_archivo_al_historico_de_su_bandeja(drive):
    archivo = origen.listar(BANDEJA)[0]
    assert origen.mover_a_historico(archivo, BANDEJA) is True
    assert drive.movidos == [('a', 'carpeta-hist')]


def test_sin_historico_configurado_el_archivo_se_queda_donde_esta(drive):
    """Dejarlo en la bandeja es molesto (se relee la próxima corrida) pero es
    reversible; perderlo de vista no. Es la regla vieja de `procesar_todos`."""
    bandeja = origen.Bandeja(fuente='wompi', drive_entrada='carpeta-in')
    archivo = origen.listar(bandeja)[0]

    assert origen.mover_a_historico(archivo, bandeja) is False
    assert drive.movidos == []


@pytest.mark.parametrize('accion', ['descargar', 'mover'])
def test_un_origen_desconocido_falla_a_gritos(drive, accion):
    """Cuando entre el depósito, un archivo mal etiquetado no puede pasar en
    silencio: sería un archivo que se da por procesado sin haberse leído."""
    archivo = {'id': 'x', 'name': 'raro.pdf', 'origen': 'inventado', 'fuente': 'wompi'}

    with pytest.raises(ValueError):
        if accion == 'descargar':
            origen.descargar(archivo)
        else:
            origen.mover_a_historico(archivo, BANDEJA)
