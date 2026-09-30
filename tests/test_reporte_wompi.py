"""El ReportePagosWompi: de dónde se lee y cuándo se archiva.

Es el único archivo que hace falta en CADA corrida aunque ya se haya
procesado: se lee en vivo y no se guarda en ninguna tabla, así que sin él los
pagos de WOMPI del día quedan sin nombre, sin CI, sin método, sin programa y
sin la corrección de documento.

Hasta el 2026-09-30 eso se resolvía dejándolo en la bandeja a propósito. El
costo lo pagaba el área: en la pantalla de carga se veía "esperando a
procesarse" para siempre —no se archiva nunca y tampoco deja fila en "Últimos
archivos procesados"—, así que parecía no leído y se volvía a subir. Hoy se
archiva como todos y se relee del histórico.

El cruce end-to-end sustituye `_cargar_lookup_wompi_reporte` entera, así que
sin este archivo estas dos funciones no tendrían ninguna prueba.
"""

import io

import pytest

import cruzar


@pytest.fixture
def bandeja(monkeypatch):
    """Los archivos que ve cada zona, sin salir a la red.

    Devuelve la lista de lo que se archivó, para poder afirmar sobre ella.
    """
    archivados: list[str] = []

    def _con(entrada=(), historico=None):
        def _archivo(nombre, zona):
            return {'id': f'{zona}/wompi_reporte/{nombre}', 'name': nombre,
                    'origen': 'deposito', 'fuente': 'wompi_reporte', 'zona': zona}

        monkeypatch.setattr(cruzar, 'hay_de_donde_leer', lambda _b: True)
        monkeypatch.setattr(cruzar, 'todos_los_que_contienen',
                            lambda _b, _t: [_archivo(n, 'entrada') for n in entrada])
        monkeypatch.setattr(cruzar, 'ultimo_archivado_que_contiene',
                            lambda _b, _t: _archivo(historico, 'historico') if historico else None)
        monkeypatch.setattr(cruzar, 'descargar', lambda _f: io.BytesIO(b''))
        monkeypatch.setattr(cruzar, 'read_pagos_wompi_reporte',
                            lambda _c: [{'id_transaccion': 'tx-1', 'pagador': 'Quien Sea'}])

        def _mover(archivo, _bandeja):
            if archivo.get('zona') == 'historico':
                return False
            archivados.append(archivo['name'])
            return True

        monkeypatch.setattr(cruzar, 'mover_a_historico', _mover)
        return archivados

    return _con


# ── De dónde se lee ─────────────────────────────────────────────────────────

def test_con_la_bandeja_vacia_se_relee_el_ultimo_archivado(bandeja):
    """Es el estado NORMAL a partir del segundo día: el reporte se archivó al
    terminar la corrida anterior. Sin esto, cada corrida que no traiga reporte
    nuevo dejaría los pagos de WOMPI del día rotulados "PAGOS MANUALES"."""
    bandeja(entrada=(), historico='ReportePagosWompi_20260929.xlsx')

    lookup, disponible, archivos = cruzar._cargar_lookup_wompi_reporte()

    assert disponible is True
    assert 'tx-1' in lookup
    assert [a['name'] for a in archivos] == ['ReportePagosWompi_20260929.xlsx']


def test_si_hay_entrega_nueva_NO_se_mira_el_historico(bandeja):
    """El archivado es lo autoritativo solo cuando no hay nada más nuevo."""
    bandeja(entrada=('ReportePagosWompi_20260930.xlsx',),
            historico='ReportePagosWompi_20260929.xlsx')

    _lookup, _disponible, archivos = cruzar._cargar_lookup_wompi_reporte()

    assert [a['name'] for a in archivos] == ['ReportePagosWompi_20260930.xlsx']


def test_sin_reporte_en_ningun_lado_se_omite_la_regla(bandeja):
    """`disponible=False` no es "ningún pago identificado": es "esta corrida no
    pudo mirar". Tratarlo igual marcaría de golpe TODOS los pagos de WOMPI del
    día como manuales."""
    bandeja(entrada=(), historico=None)

    lookup, disponible, archivos = cruzar._cargar_lookup_wompi_reporte()

    assert (lookup, disponible, archivos) == ({}, False, [])


# ── Cuándo se archiva ───────────────────────────────────────────────────────

def test_el_reporte_leido_SI_se_archiva(bandeja):
    """Lo que cambió el 2026-09-30: antes el más reciente se quedaba en la
    bandeja y el área lo veía "esperando" para siempre."""
    archivados = bandeja(entrada=('ReportePagosWompi_20260930.xlsx',))

    cruzar._archivar_reportes_wompi(
        [{'name': 'ReportePagosWompi_20260930.xlsx', 'zona': 'entrada'}])

    assert archivados == ['ReportePagosWompi_20260930.xlsx']


def test_se_archivan_TODAS_las_entregas_del_dia(bandeja):
    """El lunes se sube el acumulado del fin de semana. Ninguna se queda."""
    archivados = bandeja(entrada=())

    cruzar._archivar_reportes_wompi([
        {'name': 'ReportePagosWompi_20260927.xlsx', 'zona': 'entrada'},
        {'name': 'ReportePagosWompi_20260928.xlsx', 'zona': 'entrada'},
        {'name': 'ReportePagosWompi_20260929.xlsx', 'zona': 'entrada'},
    ])

    assert len(archivados) == 3


def test_el_que_salio_del_historico_no_se_archiva_otra_vez(bandeja):
    """Si no, cada corrida dejaría una copia más con un nombre nuevo."""
    archivados = bandeja(entrada=(), historico='ReportePagosWompi_20260929.xlsx')

    _lookup, _disponible, archivos = cruzar._cargar_lookup_wompi_reporte()
    cruzar._archivar_reportes_wompi(archivos)

    assert archivados == []


def test_un_fallo_al_archivar_no_tumba_la_corrida(bandeja, monkeypatch):
    """La plata ya está escrita: que un archivo no se haya podido mover no
    puede deshacer la corrida. Se queda en la bandeja y se reintenta."""
    bandeja(entrada=('ReportePagosWompi_20260930.xlsx',))
    monkeypatch.setattr(cruzar, 'mover_a_historico',
                        lambda *_a: (_ for _ in ()).throw(ConnectionError('sin red')))

    cruzar._archivar_reportes_wompi(
        [{'name': 'ReportePagosWompi_20260930.xlsx', 'zona': 'entrada'}])
