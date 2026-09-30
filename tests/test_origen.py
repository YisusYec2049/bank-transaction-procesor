"""La capa que decide de dónde sale un archivo.

Las pruebas de ingesta sustituyen `listar`/`descargar`/`mover_a_historico`
enteras, así que sin este archivo la capa no tendría ninguna prueba: el día que
alguien la rompa, todo seguiría en verde.

**Google Drive se desconectó el 2026-09-08** y hoy el único origen es el
depósito de la plataforma. Varias de estas pruebas siguen existiendo por lo que
costó llegar acá: dos días de convivencia entre los dos sitios dejaron un
incidente que frenó 152 pagos, y las reglas que lo evitan viven en esta capa.
"""

import io

import pytest

from utils import origen


class _DepositoFalso:
    """Se hace pasar por `utils.deposito`.

    Cada archivo se declara como nombre suelto o como `(nombre, fecha)`.
    """

    def __init__(self, archivos=None, encendido=True, historico=None):
        self.archivos = archivos if archivos is not None else []
        self.historico = historico if historico is not None else []
        self.encendido = encendido
        self.descargados: list[str] = []
        self.archivados: list[tuple[str, str, str]] = []

    def activo(self):
        return self.encendido

    def listar(self, fuente, zona='entrada'):
        salida = []
        for a in (self.historico if zona == 'historico' else self.archivos):
            nombre, fecha = a if isinstance(a, tuple) else (a, None)
            salida.append({'id': f'{zona}/{fuente}/{nombre}', 'name': nombre,
                           'created_at': fecha})
        return salida

    def descargar(self, ruta):
        self.descargados.append(ruta)
        return io.BytesIO(b'del deposito')

    def mover_a_historico(self, ruta, fuente, nombre):
        self.archivados.append((ruta, fuente, nombre))


BANDEJA = origen.Bandeja(fuente='wompi')


@pytest.fixture
def deposito(monkeypatch):
    def _con(archivos, historico=None):
        falso = _DepositoFalso(archivos, historico=historico)
        monkeypatch.setattr(origen, '_deposito', falso)
        return falso
    return _con


# ── Lo básico: listar, descargar, archivar ───────────────────────────────────

def test_cada_archivo_dice_de_donde_vino(deposito):
    """`origen` y `fuente` son lo que después decide a quién se le pide el
    contenido y dónde se archiva. Sin eso, `descargar` no sabría a quién ir."""
    deposito(['extracto.pdf', 'otro.pdf'])

    archivos = origen.listar(BANDEJA)

    assert [a['name'] for a in archivos] == ['extracto.pdf', 'otro.pdf']
    assert all(a['origen'] == origen.DEPOSITO for a in archivos)
    assert all(a['fuente'] == 'wompi' for a in archivos)


def test_sin_deposito_no_se_lista_nada(monkeypatch):
    """`DEPOSITO_BUCKET` vacía apaga el depósito entero. Desde que Drive se
    desconectó eso significa que no hay NADA que procesar, y quien llama tiene
    que poder seguir sin reventar."""
    monkeypatch.setattr(origen, '_deposito', _DepositoFalso([], encendido=False))

    assert origen.listar(BANDEJA) == []
    assert origen.mas_reciente(BANDEJA) is None
    assert origen.hay_de_donde_leer(BANDEJA) is False


def test_con_deposito_hay_de_donde_leer_aunque_no_haya_archivos(deposito):
    """"No hay archivos" y "no hay de dónde leerlos" son cosas distintas.
    Confundirlas era lo que hacía el código viejo: con la carpeta de Drive sin
    configurar, `procesar_todos.py` se salteaba TODOS los bancos."""
    deposito([])

    assert origen.hay_de_donde_leer(BANDEJA) is True


def test_descargar_le_pide_el_contenido_al_deposito(deposito):
    dep = deposito(['extracto.pdf'])
    archivo = origen.listar(BANDEJA)[0]

    assert origen.descargar(archivo).read() == b'del deposito'
    assert dep.descargados == ['entrada/wompi/extracto.pdf']


def test_archivar_manda_el_archivo_al_historico_de_su_fuente(deposito):
    """El destino se deriva de la fuente y no de una variable de entorno: por
    eso una bandeja no puede quedarse 'sin histórico' y releerse para siempre,
    que es como un archivo de Drive terminaba en bucle."""
    dep = deposito(['extracto.pdf'])
    archivo = origen.listar(BANDEJA)[0]

    assert origen.mover_a_historico(archivo, BANDEJA) is True
    assert dep.archivados == [('entrada/wompi/extracto.pdf', 'wompi', 'extracto.pdf')]


@pytest.mark.parametrize('accion', ['descargar', 'mover'])
def test_un_origen_desconocido_falla_a_gritos(accion):
    """Un archivo mal etiquetado no puede pasar en silencio: sería un archivo
    que se da por procesado sin haberse leído."""
    archivo = {'id': 'x', 'name': 'raro.pdf', 'origen': 'inventado', 'fuente': 'wompi'}

    with pytest.raises(ValueError):
        if accion == 'descargar':
            origen.descargar(archivo)
        else:
            origen.mover_a_historico(archivo, BANDEJA)


def test_se_filtra_por_lo_que_contiene_el_nombre(deposito):
    deposito(['extracto.pdf', 'ReportePagosWompi_20260903.xlsx'])

    encontrados = origen.todos_los_que_contienen(BANDEJA, 'reportepagoswompi')

    assert [a['name'] for a in encontrados] == ['ReportePagosWompi_20260903.xlsx']


# ── El orden es POR FECHA ────────────────────────────────────────────────────
#
# Hay dos sitios que leen el ÚLTIMO de la lista como si fuera el más reciente:
# `mas_reciente` (los 3 archivos de referencia) y el archivado del
# ReportePagosWompi en `cruzar.py`. El 2026-09-08, con Drive todavía conectado,
# un orden que no era por fecha dejó vigente un reporte de 4 días antes y
# archivó el del día. Ordenar acá es lo que hace que esa lectura sea cierta.

def test_el_mas_reciente_es_el_de_la_fecha_mas_nueva(deposito):
    deposito([('viejo.xlsx', '2026-08-20T10:00:00.000Z'),
              ('nuevo.xlsx', '2026-09-08T14:56:49.653Z')])

    assert origen.mas_reciente(BANDEJA)['name'] == 'nuevo.xlsx'


def test_el_orden_no_se_hereda_del_almacenamiento(deposito):
    """Aunque el almacenamiento los entregue al revés, la lista sale del más
    viejo al más reciente."""
    deposito([('nuevo.xlsx', '2026-09-08T14:56:49.653Z'),
              ('viejo.xlsx', '2026-08-20T10:00:00.000Z')])

    assert [a['name'] for a in origen.listar(BANDEJA)] == ['viejo.xlsx', 'nuevo.xlsx']


def test_un_archivo_SIN_fecha_no_se_hace_pasar_por_el_mas_nuevo(deposito):
    """Ante la duda, lo desconocido va al fondo: hacerlo pasar por reciente es
    la decisión que puede cargar un Excel viejo encima de la tabla."""
    deposito([('misterioso.xlsx', None), ('del dia.xlsx', '2026-09-08T14:56:49.653Z')])

    assert origen.mas_reciente(BANDEJA)['name'] == 'del dia.xlsx'


def test_una_fecha_ilegible_tampoco(deposito):
    deposito([('roto.xlsx', 'no es una fecha'), ('del dia.xlsx', '2026-09-08T14:56:49.653Z')])

    assert origen.mas_reciente(BANDEJA)['name'] == 'del dia.xlsx'


# ── Releer el ReportePagosWompi ya archivado (2026-09-30) ────────────────────
#
# El reporte es el único archivo que hace falta en CADA corrida aunque ya se
# haya procesado: se lee en vivo y no se guarda en ninguna tabla. Antes eso se
# resolvía dejándolo en la bandeja para siempre, y el área lo veía "esperando a
# procesarse" sin que nada lo sacara de ahí. Hoy se archiva como todos y se
# relee del histórico.

def test_el_reporte_ya_archivado_se_puede_releer(deposito):
    deposito([], historico=[('ReportePagosWompi_20260928.xlsx', '2026-09-28T10:00:00Z'),
                            ('ReportePagosWompi_20260929.xlsx', '2026-09-29T10:00:00Z')])

    ultimo = origen.ultimo_archivado_que_contiene(BANDEJA, 'ReportePagosWompi')

    assert ultimo['name'] == 'ReportePagosWompi_20260929.xlsx'
    assert ultimo['id'] == 'historico/wompi/ReportePagosWompi_20260929.xlsx'


def test_releer_el_archivado_manda_el_MAS_RECIENTE_no_el_ultimo_de_la_lista(deposito):
    """Misma regla que en `listar`: el orden lo pone la FECHA, no cómo venga el
    almacenamiento. Un reporte vencido tomado como vigente deja los pagos del
    día rotulados 'PAGOS MANUALES' — pasó el 2026-09-08 con uno de 4 días
    antes."""
    deposito([], historico=[('ReportePagosWompi_20260929.xlsx', '2026-09-29T10:00:00Z'),
                            ('ReportePagosWompi_20260925.xlsx', '2026-09-25T10:00:00Z')])

    ultimo = origen.ultimo_archivado_que_contiene(BANDEJA, 'ReportePagosWompi')

    assert ultimo['name'] == 'ReportePagosWompi_20260929.xlsx'


def test_sin_nada_archivado_no_hay_reporte_que_releer(deposito):
    deposito([], historico=['otra-cosa.xlsx'])

    assert origen.ultimo_archivado_que_contiene(BANDEJA, 'ReportePagosWompi') is None


def test_un_archivo_del_historico_NO_se_vuelve_a_archivar(deposito):
    """Si se archivara otra vez, cada corrida dejaría una copia con un nombre
    nuevo: el reporte se relee del histórico en todas."""
    dep = deposito([], historico=['ReportePagosWompi_20260929.xlsx'])
    archivo = origen.ultimo_archivado_que_contiene(BANDEJA, 'ReportePagosWompi')

    assert origen.mover_a_historico(archivo, BANDEJA) is False
    assert dep.archivados == []
