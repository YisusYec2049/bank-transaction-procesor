"""
El cruce completo, de punta a punta, sin tocar la red.

Corre el `main()` **real** de `cruzar.py` contra tablas puestas a mano. Es la
primera prueba del repo que ejercita la decisión que de verdad importa: a qué
inscripción pertenece cada pago, y cuál se queda en excepciones.

Las reglas que se fijan acá no son inventadas: cada una viene de una decisión
registrada, y varias existen porque antes se rompieron en producción.
"""

import pytest

import cruzar


def _pago(matching_key, identification='', email='', metodo='BANCOLOMBIA',
          monto=500_000, fecha='2026-08-01'):
    return {
        'identification': identification, 'payment_date': fecha,
        'transaction_code_1': 'PAGO QR', 'transaction_code_2': '',
        'email': email, 'payment_method': metodo, 'program': '', 'phone': '',
        'payment_amount': monto, 'matching_key': matching_key,
        'registration_date': fecha, 'metodo_de_pago': None,
    }


def _filas(capturado):
    return {f['matching_key']: f for f in capturado.get('cruce_cartera', [])}


def test_documento_conocido_cierra_cruzado(mundo):
    """La regla base: si el documento del pago está en cartera, se cierra solo.

    El documento es la señal fuerte del sistema — un número de identificación
    que coincide alcanza para cerrar sin que nadie mire.
    """
    capturado = mundo(cruzar, tablas={
        'consolidated_transactions': [_pago('PAGO-1', identification='1002003004')],
        'cartera_inscrip': [{'numero_id': '1002003004', 'id_inscripcion': '4321PN'}],
    })

    fila = _filas(capturado)['PAGO-1']
    assert fila['estado_cruce'] == 'cruzado'
    assert fila['incp'] == '4321PN'


def test_documento_desconocido_queda_en_excepciones(mundo):
    """Sin señal, el pipeline NO cierra: deja el pago para que alguien lo mire.

    Es la regla del 22 de julio, y costó $97M aprenderla: cerrar solo como
    "no identificable" convertía "hoy nadie puede resolverlo" en "nunca se va a
    reintentar", porque ese estado es terminal.
    """
    capturado = mundo(cruzar, tablas={
        'consolidated_transactions': [_pago('PAGO-1', identification='9999999999')],
        'cartera_inscrip': [{'numero_id': '1002003004', 'id_inscripcion': '4321PN'}],
    })

    fila = _filas(capturado)['PAGO-1']
    assert fila['estado_cruce'] == 'pendiente'
    assert fila['estado_cruce'] != 'no_identificable', (
        'el pipeline no puede cerrar un pago como no_identificable por su cuenta'
    )


def test_nit_con_digito_de_verificacion_cruza(mundo):
    """La cartera guarda el NIT con dígito de verificación; el banco no.

    Sin normalizar esto, ningún pago de una empresa cruzaba nunca — eran 594 de
    8.853 inscripciones (8 de julio).
    """
    capturado = mundo(cruzar, tablas={
        'consolidated_transactions': [_pago('PAGO-1', identification='860004922')],
        'cartera_inscrip': [{'numero_id': '860004922-4', 'id_inscripcion': '430PJ'}],
    })

    assert _filas(capturado)['PAGO-1']['incp'] == '430PJ'


def test_mismo_numero_con_sufijo_distinto_no_es_ambiguedad(mundo):
    """`3300` y `3300PN` son la misma inscripción escrita de dos formas.

    Antes de esta regla, la mitad de las ambigüedades de WOMPI eran falsas.
    Gana la forma con sufijo, que es la del sistema financiero.
    """
    capturado = mundo(cruzar, tablas={
        'consolidated_transactions': [_pago('PAGO-1', identification='1002003004')],
        'cartera_inscrip': [
            {'numero_id': '1002003004', 'id_inscripcion': '3300'},
            {'numero_id': '1002003004', 'id_inscripcion': '3300PN'},
        ],
    })

    fila = _filas(capturado)['PAGO-1']
    assert fila['incp'] == '3300PN'
    assert fila['estado_cruce'] == 'cruzado'


def test_dos_inscripciones_distintas_son_ambiguedad_real(mundo):
    """Dos diplomados distintos del mismo documento: eso sí lo decide una
    persona, no el pipeline."""
    capturado = mundo(cruzar, tablas={
        'consolidated_transactions': [_pago('PAGO-1', identification='1002003004')],
        'cartera_inscrip': [
            {'numero_id': '1002003004', 'id_inscripcion': '3300PN'},
            {'numero_id': '1002003004', 'id_inscripcion': '4400PN'},
        ],
    })

    fila = _filas(capturado)['PAGO-1']
    assert fila['estado_cruce'] == 'pendiente'
    assert fila['excepcion_motivo'] == 'cruce_ambiguo'


def test_pago_por_llave_sale_del_proceso(mundo):
    """Los identificadores de canal de la universidad no son cédulas de nadie.

    Un pago que llega por ahí no pertenece a ninguna persona: se aparta en vez
    de intentar cruzarlo (rediseño del 16 de julio).
    """
    capturado = mundo(cruzar, tablas={
        'consolidated_transactions': [_pago('PAGO-1', identification='800138188')],
        'cartera_inscrip': [],
    })

    apartados = capturado.get('pagos_apartados', [])
    assert any(a.get('matching_key') == 'PAGO-1' for a in apartados), (
        'el pago por llave debió apartarse del proceso'
    )
    assert 'PAGO-1' not in _filas(capturado), 'un pago apartado no va al cruce'


def test_una_fila_ya_cerrada_no_se_recalcula(mundo):
    """Lo que resolvió una persona no se pisa.

    Es la garantía que protege todas las correcciones manuales hechas desde la
    plataforma; si se rompiera, cada corrida las borraría.
    """
    capturado = mundo(cruzar, tablas={
        'consolidated_transactions': [_pago('PAGO-1', identification='1002003004')],
        'cartera_inscrip': [{'numero_id': '1002003004', 'id_inscripcion': '4321PN'}],
        'cruce_cartera': [{'matching_key': 'PAGO-1', 'estado_cruce': 'cruzado',
                           'incp': 'PUESTO-A-MANO', 'identification': '1002003004'}],
    })

    escritas = _filas(capturado)
    assert 'PAGO-1' not in escritas, (
        'una fila ya resuelta se volvió a escribir: la corrección manual '
        f'("PUESTO-A-MANO") se habría pisado con {escritas.get("PAGO-1", {}).get("incp")!r}'
    )


def test_correr_dos_veces_da_lo_mismo(mundo):
    """Idempotencia: es la garantía que permite reprocesar sin miedo.

    Una corrida interrumpida se arregla volviéndola a correr, y el botón de la
    plataforma dispara reprocesos varias veces al día.
    """
    tablas = {
        'consolidated_transactions': [
            _pago('PAGO-1', identification='1002003004'),
            _pago('PAGO-2', identification='9999999999'),
        ],
        'cartera_inscrip': [{'numero_id': '1002003004', 'id_inscripcion': '4321PN'}],
    }

    primera = _filas(mundo(cruzar, tablas=tablas))
    segunda = _filas(mundo(cruzar, tablas=tablas))

    def comparable(filas):
        return {k: {c: v for c, v in f.items() if c != 'updated_at'}
                for k, f in filas.items()}

    assert comparable(primera) == comparable(segunda)


@pytest.mark.parametrize('metodo', ['BANCOLOMBIA', 'PREBANCOLOMBIA', 'WOMPI PSE',
                                    'STRIPE_USA', 'Placetopay (PSE)'])
def test_ninguna_pasarela_revienta_el_cruce(mundo, metodo):
    """Cada pasarela toma una rama distinta del cruce. Ninguna puede tumbarlo.

    `PREBANCOLOMBIA` (Bancolombia 2833) no tuvo rama propia hasta el 27 de
    julio: sus pagos caían siempre en "sin cruce" aunque el equipo ya los
    tuviera identificados.
    """
    capturado = mundo(cruzar, tablas={
        'consolidated_transactions': [
            _pago('PAGO-1', identification='1002003004',
                  email='alguien@example.com', metodo=metodo)],
        'cartera_inscrip': [{'numero_id': '1002003004', 'id_inscripcion': '4321PN'}],
    })

    assert 'PAGO-1' in _filas(capturado)
