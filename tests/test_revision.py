"""La revisión del archivo al subirlo: los tres veredictos.

El 2026-09-21 el área subió a la caja de **Payu UC** un Excel que no era, apretó
Procesar, y la pantalla contestó *"La corrida terminó. No entró ningún archivo
nuevo"*. Los 188 pagos del día quedaron esperando dos horas.

Este módulo contesta la misma pregunta **en el momento de subir**, que es cuando
la persona todavía está mirando la pantalla y tiene el archivo bueno a mano. Lo
que se prueba acá es que los dos rojos digan cosas distintas, porque el área
tiene que hacer cosas distintas con cada uno: buscar otro archivo, o corregir el
que tiene.
"""

import io

import openpyxl
import pytest

from utils import revision


def _libro(hojas: dict[str, list[list]]) -> io.BytesIO:
    """Un Excel armado acá mismo, con las hojas y filas que se pidan.

    A propósito no se guarda como fixture: lo que se prueba son los NOMBRES de
    hoja y de columna, y un archivo congelado los dejaría fuera de la vista."""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for nombre, filas in hojas.items():
        ws = wb.create_sheet(nombre)
        for fila in filas:
            ws.append(fila)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


PAYU_UC_BUENO = {'Inscrip': [['Numero_ID', 'Id_Inscripcion'],
                             ['1020814497', '5928PN'],
                             ['901032802-6', '430PJ']]}


# ── Los tres veredictos, sobre la caja donde pasó el caso real ───────────────


def test_el_archivo_correcto_pasa_con_sus_filas():
    r = revision.revisar('payu_uc', _libro(PAYU_UC_BUENO))

    assert r['estado'] == 'ok'
    assert r['filas'] == 2


def test_el_archivo_correcto_pasa_aunque_se_llame_distinto():
    """El nombre NO decide nada: la estructura manda. Requisito explícito del
    usuario, y el único que funciona — a las cajas de bancos les caen nombres
    distintos todos los días."""
    # (el nombre ni siquiera viaja hasta acá: `revisar` recibe el contenido)
    assert revision.revisar('payu_uc', _libro(PAYU_UC_BUENO))['estado'] == 'ok'


def test_otro_archivo_en_la_caja_es_ARCHIVO_INCORRECTO():
    """El caso del 2026-09-21: subieron `Actualizacion Drive de Ingresos.xlsx`,
    que tiene hojas parecidas pero ninguna `Inscrip`."""
    otro = _libro({'PAYU UC': [['FECHA', 'DOCUMENTO']], 'BANCOLOMBIA 2576': [[]]})

    r = revision.revisar('payu_uc', otro)

    assert r['estado'] == 'archivo_incorrecto'
    assert r['espera'] == 'PAYU UC'  # el texto que ve el área


def test_el_archivo_con_una_columna_cambiada_es_FORMATO_INCORRECTO():
    """Es el archivo de esta caja —su hoja está— pero le cambiaron algo adentro.
    Son dos rojos distintos porque el área tiene que hacer dos cosas distintas:
    buscar otro archivo, o corregir el que tiene."""
    sin_columna = _libro({'Inscrip': [['Numero_ID', 'Inscripcion'], ['1020814497', '5928PN']]})

    r = revision.revisar('payu_uc', sin_columna)

    assert r['estado'] == 'formato_incorrecto'
    assert 'espera' not in r  # no se le pide otro archivo: este es el que va


# ── "Ingresos PSE y PAYU": cuatro hojas en un archivo ───────────────────────


def _monkey_hojas(monkeypatch, resultados: dict[str, object]):
    """Cada hoja devuelve filas o lanza, según lo que diga `resultados`."""
    lectores = {'BANCOLOMBIA 2576': 'read_bancolombia_2576', 'WOMPI': 'read_wompi',
                'STRIPE_USA': 'read_stripe_usa', 'BANCOL 2833': 'read_bancolombia_2833'}
    for hoja, atributo in lectores.items():
        valor = resultados[hoja]

        def _lector(_buf, _v=valor):
            if isinstance(_v, Exception):
                raise _v
            return [{}] * _v

        monkeypatch.setattr(revision, atributo, _lector)


def test_ingresos_con_sus_cuatro_hojas_pasa(monkeypatch):
    _monkey_hojas(monkeypatch, {'BANCOLOMBIA 2576': 3109, 'WOMPI': 26995,
                                'STRIPE_USA': 1971, 'BANCOL 2833': 695})

    r = revision.revisar('ingresos', _libro({'x': [[]]}))

    assert r == {'estado': 'ok', 'filas': 3109 + 26995 + 1971 + 695}


def test_una_sola_hoja_renombrada_es_FORMATO_INCORRECTO(monkeypatch):
    """El caso del 2026-09-18, que estuvo CUATRO DÍAS sin que nadie lo viera: el
    área renombró `BANCOL 2833` a `PREBANCOLOMBIA 2833`. Las otras tres hojas se
    leen perfecto, así que el archivo SÍ es el de esta caja — lo que hay que
    hacer es corregirlo, no buscar otro.

    Y esa hoja no es un detalle: para los pagos de la 2833 es la única señal de
    identidad que existe (muchos llegan por NEQUI sin documento)."""
    _monkey_hojas(monkeypatch, {
        'BANCOLOMBIA 2576': 3109, 'WOMPI': 26995, 'STRIPE_USA': 1971,
        'BANCOL 2833': KeyError('Worksheet BANCOL 2833 does not exist.'),
    })

    r = revision.revisar('ingresos', _libro({'x': [[]]}))

    assert r['estado'] == 'formato_incorrecto'
    assert 'BANCOL 2833' in r['detalle']


def test_si_ninguna_hoja_se_lee_es_ARCHIVO_INCORRECTO(monkeypatch):
    """Cuatro hojas ilegibles no es un archivo con un problema: es otro
    archivo."""
    _monkey_hojas(monkeypatch, dict.fromkeys(
        ['BANCOLOMBIA 2576', 'WOMPI', 'STRIPE_USA', 'BANCOL 2833'], KeyError('no existe')))

    r = revision.revisar('ingresos', _libro({'x': [[]]}))

    assert r['estado'] == 'archivo_incorrecto'
    assert r['espera'] == 'INGRESOS PSE Y PAYU'


# ── Bancos y pasarelas ──────────────────────────────────────────────────────


def test_un_extracto_real_de_placetopay_pasa():
    with open('tests/fixtures/placetopay_pagos.xlsx', 'rb') as f:
        r = revision.revisar('placetopay', io.BytesIO(f.read()))

    assert r['estado'] == 'ok'
    assert r['filas'] > 0


def test_un_archivo_que_no_es_de_esa_pasarela_es_ARCHIVO_INCORRECTO():
    with open('tests/fixtures/placetopay_pagos.xlsx', 'rb') as f:
        contenido = io.BytesIO(f.read())

    r = revision.revisar('stripe', contenido)

    assert r['estado'] == 'archivo_incorrecto'
    assert r['espera'] == 'STRIPE'


def test_un_extracto_de_wompi_real_pasa():
    with open('tests/fixtures/wompi_reporte.csv', 'rb') as f:
        r = revision.revisar('wompi', io.BytesIO(f.read()))

    assert r['estado'] == 'ok'


# ── La revisión no puede dejar a nadie sin subir ────────────────────────────


@pytest.mark.parametrize('fuente', sorted(revision.ESPERA))
def test_las_13_cajas_tienen_chequeo_y_texto(fuente):
    """Si una caja se quedara sin chequeo, su archivo saldría en rojo siempre.
    Y `ESPERA` es el texto que lee el área, así que las dos listas tienen que
    cubrir las mismas 13 cajas."""
    assert fuente in revision._CHEQUEOS
    assert revision.ESPERA[fuente]


def test_una_fuente_desconocida_no_revienta():
    r = revision.revisar('inventada', io.BytesIO(b'x'))

    assert r['estado'] == 'archivo_incorrecto'


def test_un_archivo_ilegible_no_revienta():
    """Ante cualquier sorpresa se contesta rojo, nunca un verde de más ni una
    excepción: la pantalla tiene que poder mostrar algo siempre."""
    r = revision.revisar('payu_uc', io.BytesIO(b'esto no es un excel'))

    assert r['estado'] == 'archivo_incorrecto'


# ── Las dos cuentas de Bancolombia ──────────────────────────────────────────
#
# El único par que el parser no distingue solo: el mismo PDF del mismo banco,
# bajados la misma mañana, con nombres casi iguales. Cruzarlos cambia el medio
# de pago con el que entra cada pago, y con él la hoja donde se busca su
# CORREO(2).


class _PaginaFalsa:
    def __init__(self, texto): self._texto = texto
    def extract_text(self): return self._texto


class _PdfFalso:
    def __init__(self, texto): self.pages = [_PaginaFalsa(texto)]
    def __enter__(self): return self
    def __exit__(self, *_a): return False


def _extracto_que_dice(monkeypatch, cuenta: str):
    monkeypatch.setattr(revision.pdfplumber, 'open',
                        lambda _b: _PdfFalso(f'Número de Cuenta:{cuenta} Tipo de cuenta:Ahorros'))
    monkeypatch.setattr(revision.mod_bc2576, 'parse_pdf', lambda _b: [{}, {}])
    monkeypatch.setattr(revision.mod_bc2833, 'parse_pdf', lambda _b: [{}, {}])


def test_el_extracto_de_la_otra_cuenta_es_ARCHIVO_INCORRECTO(monkeypatch):
    _extracto_que_dice(monkeypatch, '16869342576')  # es el de la 2576

    r = revision.revisar('bc2833', io.BytesIO(b'%PDF'))

    assert r['estado'] == 'archivo_incorrecto'
    assert r['espera'] == 'BANCOLOMBIA 2833'


def test_cada_extracto_pasa_en_su_caja(monkeypatch):
    _extracto_que_dice(monkeypatch, '16869342576')

    assert revision.revisar('bc2576', io.BytesIO(b'%PDF'))['estado'] == 'ok'


def test_un_encabezado_desconocido_no_pinta_rojo(monkeypatch):
    """Solo se rechaza con evidencia POSITIVA de que es el otro extracto. Si el
    banco cambia el encabezado y no aparece ninguna cuenta, se deja pasar al
    parser: un rojo de más sobre un archivo bueno le enseña al área a
    desconfiar del semáforo, que es lo único que este cambio tiene para dar."""
    _extracto_que_dice(monkeypatch, '')

    assert revision.revisar('bc2833', io.BytesIO(b'%PDF'))['estado'] == 'ok'
