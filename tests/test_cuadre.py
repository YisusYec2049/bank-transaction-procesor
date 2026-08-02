"""
El chequeo de cuadre: por cada pago, aplicado + disponible == lo que entró.

Es la única prueba que corre **sobre producción, en cada corrida**, y por eso
importa que no dé falsas alarmas: un chequeo que grita seguido se aprende a
ignorar, y entonces no sirve para nada el día que tiene razón.

Medido contra producción antes de instalarlo (2026-08-02): 154 pagos con
movimiento, 0 descuadres.
"""

import pytest

import cruzar_cartera_preventiva as ccp


@pytest.fixture
def base_falsa(monkeypatch):
    """Sustituye la lectura de la base por datos puestos a mano.

    `verificar_cuadre` es la única función de este módulo que se puede probar
    aislada hoy, justamente porque no depende del `main()` de 687 líneas: recibe
    lo que necesita y relee dos tablas. Partir el resto así es la Fase 5.
    """
    def montar(asociaciones, saldos):
        def _select(_url, _k, tabla, select=None, **_kw):
            return {'pago_asociaciones': asociaciones,
                    'cartera_saldos_favor': saldos}[tabla]
        monkeypatch.setattr(ccp, 'select_all', _select)
    return montar


def _pago(mk, monto):
    return {mk: {'matching_key': mk, 'payment_amount': monto}}


def test_pago_repartido_completo_cuadra(base_falsa):
    base_falsa(
        asociaciones=[{'matching_key': 'P1', 'llave': 'L1', 'monto': 600_000},
                      {'matching_key': 'P1', 'llave': 'L2', 'monto': 400_000}],
        saldos=[],
    )
    assert ccp.verificar_cuadre('u', 'k', _pago('P1', 1_000_000)) == []


def test_pago_con_sobrante_cuadra(base_falsa):
    """Parte aplicada y parte esperando: sigue estando toda la plata."""
    base_falsa(
        asociaciones=[{'matching_key': 'P1', 'llave': 'L1', 'monto': 700_000}],
        saldos=[{'matching_key': 'P1', 'disponible': 300_000}],
    )
    assert ccp.verificar_cuadre('u', 'k', _pago('P1', 1_000_000)) == []


def test_falta_plata(base_falsa):
    """El caso que más duele: entró más de lo que aparece en algún lado."""
    base_falsa(
        asociaciones=[{'matching_key': 'P1', 'llave': 'L1', 'monto': 400_000}],
        saldos=[],
    )
    fallos = ccp.verificar_cuadre('u', 'k', _pago('P1', 1_000_000))
    assert len(fallos) == 1
    assert fallos[0]['motivo'] == 'falta'
    assert fallos[0]['diferencia'] == -600_000


def test_sobra_plata(base_falsa):
    """Aplicado por encima de lo que entró = plata contada dos veces."""
    base_falsa(
        asociaciones=[{'matching_key': 'P1', 'llave': 'L1', 'monto': 800_000},
                      {'matching_key': 'P1', 'llave': 'L2', 'monto': 800_000}],
        saldos=[],
    )
    fallos = ccp.verificar_cuadre('u', 'k', _pago('P1', 1_000_000))
    assert len(fallos) == 1
    assert fallos[0]['motivo'] == 'sobra'


def test_el_bug_real_del_28_de_julio(base_falsa):
    """Dos pagos iguales el mismo día; el sobrante del segundo se registró a
    nombre del primero.

    Es el caso 4166PN: dos pagos de $524.688, uno cubrió la cuota y el otro
    quedó libre. La suma total cuadraba —por eso nadie lo vio— pero el segundo
    pago se quedaba sin ningún rastro de haberse usado, así que cada corrida lo
    volvía a mirar como nuevo. Mirando pago por pago, salta.
    """
    base_falsa(
        asociaciones=[{'matching_key': 'P1', 'llave': 'L1', 'monto': 524_688}],
        saldos=[{'matching_key': 'P1', 'disponible': 524_688}],   # debería ser de P2
    )
    pagos = {**_pago('P1', 524_688), **_pago('P2', 524_688)}

    fallos = ccp.verificar_cuadre('u', 'k', pagos)
    assert len(fallos) == 1
    assert fallos[0]['matching_key'] == 'P1'
    assert fallos[0]['motivo'] == 'sobra'


def test_movimiento_sin_pago_en_el_cruce(base_falsa):
    """Plata repartida cuyo pago ya no está en el cruce: quedó colgando."""
    base_falsa(
        asociaciones=[{'matching_key': 'FANTASMA', 'llave': 'L1', 'monto': 100_000}],
        saldos=[],
    )
    fallos = ccp.verificar_cuadre('u', 'k', {})
    assert len(fallos) == 1
    assert fallos[0]['motivo'] == 'sin pago en cruce_cartera'


def test_el_redondeo_no_dispara_alarma(base_falsa):
    """Un peso de diferencia es ruido de redondeo, no un problema.

    La tolerancia importa: sin ella el chequeo gritaría en cada corrida y se
    volvería invisible.
    """
    base_falsa(
        asociaciones=[{'matching_key': 'P1', 'llave': 'L1', 'monto': 999_999.6}],
        saldos=[],
    )
    assert ccp.verificar_cuadre('u', 'k', _pago('P1', 1_000_000)) == []


def test_un_pago_malo_no_esconde_a_los_buenos(base_falsa):
    base_falsa(
        asociaciones=[{'matching_key': 'BUENO', 'llave': 'L1', 'monto': 500_000},
                      {'matching_key': 'MALO',  'llave': 'L2', 'monto': 100_000}],
        saldos=[],
    )
    pagos = {**_pago('BUENO', 500_000), **_pago('MALO', 900_000)}

    fallos = ccp.verificar_cuadre('u', 'k', pagos)
    assert [f['matching_key'] for f in fallos] == ['MALO']
