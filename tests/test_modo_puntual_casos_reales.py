"""
¿El modo puntual sirve para un pago que NO está `pendiente`?

La pregunta salió al implementar la spec en `financial-platform`: hay botones que
corrigen el documento de un pago **ya cruzado** o **ya apartado**, y la spec no
decía si podían mandar el `matching_key`. Si el modo puntual no los cubriera,
mandarlo dejaría el cambio del usuario **sin recalcular** — que es el peor
desenlace posible para un botón.

Se contesta corriendo el `main()` real, no leyendo el código.
"""

import pytest

from tests.test_modo_puntual import _fila, _pago


def test_corregir_el_documento_de_un_pago_YA_CRUZADO_lo_reabre(mundo_filtrable):
    """El caso de "corregir documento" en Transacciones y en Excepciones.

    La fila ya está `cruzado`, que es un estado terminal: el pipeline las salta.
    Pero si el documento cambió, la fila **se reabre** — y esa regla vive en el
    flujo por transacción, no en un pase global, así que el modo puntual la
    conserva. Esto lo comprueba.
    """
    mundo = {
        'consolidated_transactions': [_pago('PAGO-1', identification='1002003004')],
        'cartera_inscrip': [{'numero_id': '1002003004', 'id_inscripcion': '4321PN'}],
        # La fila guardada quedó con el documento VIEJO: eso es lo que dispara
        # la reapertura.
        'cruce_cartera': [{'matching_key': 'PAGO-1', 'estado_cruce': 'cruzado',
                           'incp': 'VIEJO', 'identification': '999999999'}],
    }

    fila = _fila(mundo_filtrable(mundo, solo='PAGO-1'), 'PAGO-1')

    assert fila is not None, (
        'el modo puntual no reabrió la fila: el botón "corregir documento" '
        'sobre un pago ya cruzado se quedaría sin recalcular'
    )
    assert fila['incp'] == '4321PN', 'se reabrió pero no volvió a cruzar'


def test_un_pago_ya_cruzado_SIN_cambios_sigue_sin_tocarse(mundo_filtrable):
    """La otra cara: reabrir por documento cambiado no puede volverse "reabrir
    siempre". Lo que resolvió una persona no se pisa."""
    mundo = {
        'consolidated_transactions': [_pago('PAGO-1', identification='1002003004')],
        'cartera_inscrip': [{'numero_id': '1002003004', 'id_inscripcion': '4321PN'}],
        'cruce_cartera': [{'matching_key': 'PAGO-1', 'estado_cruce': 'cruzado',
                           'incp': 'PUESTO-A-MANO', 'identification': '1002003004'}],
    }

    assert _fila(mundo_filtrable(mundo, solo='PAGO-1'), 'PAGO-1') is None, (
        'se reescribió una fila resuelta que no había cambiado'
    )


def test_guardar_el_incp_de_un_pago_apartado_lo_devuelve_al_cruce(mundo_filtrable):
    """El caso de "Guardar INCP" en Pagos Apartados.

    El pago **sigue apartado** (la fila no se borra): lo único que cambia es que
    alguien le escribió el INCP a mano. Con eso tiene que volver al cruce y
    cerrar con ese INCP forzado, sin recalcularlo por lookup.

    ("Desmarcar" es el otro botón y borra la fila de `pagos_apartados`; ese caso
    lo cubre el invariante de `test_modo_puntual`, porque el pago queda igual a
    cualquier otro.)
    """
    mundo = {
        'consolidated_transactions': [_pago('PAGO-1', identification='1002003004')],
        'cartera_inscrip': [{'numero_id': '1002003004', 'id_inscripcion': '4321PN'}],
        'pagos_apartados': [{'matching_key': 'PAGO-1', 'tipo': 'matricula',
                             'incp_resuelto': '9999PN'}],
    }

    fila = _fila(mundo_filtrable(mundo, solo='PAGO-1'), 'PAGO-1')

    assert fila is not None, (
        'el modo puntual no reintegró el pago apartado: "Guardar INCP" se '
        'quedaría sin efecto hasta la corrida del día siguiente'
    )
    assert fila['incp'] == '9999PN', 'no respetó el INCP puesto a mano'
    assert fila['estado_cruce'] == 'cruzado'


def test_guardar_una_correccion_manual_no_la_pisa(mundo_filtrable):
    """El caso de "Guardar corrección" (INCP/Correo(2)) en Excepciones.

    Era el único de los tres que nadie había medido, y el que más miedo daba:
    la app deja la fila `cruzado` + `corregido_manual` **antes** de disparar el
    reproceso, así que si el modo puntual la volviera a cruzar por lookup,
    borraría lo que acabó de escribir una persona.

    Lo correcto es que `cruzar.py` **no la toque** (es terminal y el documento
    no cambió) y que el recálculo real lo haga la cartera preventiva, que corre
    completa igual. Se comprueba contra la corrida completa para que la
    afirmación sea "hace lo mismo", no solo "no escribe".
    """
    mundo = {
        'consolidated_transactions': [
            _pago('PAGO-1', identification='1002003004', email='ana@x.com'),
        ],
        # El documento SÍ está en cartera y resolvería a otro INCP por lookup:
        # si el modo puntual recruzara, pisaría la corrección con '4321PN'.
        'cartera_inscrip': [{'numero_id': '1002003004', 'id_inscripcion': '4321PN'}],
        'cruce_cartera': [{'matching_key': 'PAGO-1', 'estado_cruce': 'cruzado',
                           'incp': 'PUESTO-A-MANO', 'correo_2': 'PUESTO-A-MANO',
                           'corregido_manual': True,
                           'identification': '1002003004'}],
    }

    puntual = _fila(mundo_filtrable(mundo, solo='PAGO-1'), 'PAGO-1')
    completo = _fila(mundo_filtrable(mundo), 'PAGO-1')

    assert puntual is None, (
        'el modo puntual reescribió una fila corregida a mano: "Guardar '
        'corrección" perdería el INCP que acaba de poner una persona'
    )
    assert puntual == completo, (
        'el modo puntual no se comporta igual que la corrida completa'
    )


def test_un_pago_apartado_sin_resolver_sigue_fuera_del_cruce(mundo_filtrable):
    """Marcar matrícula/cesantías: el pago sale del proceso y no debe volver
    solo."""
    mundo = {
        'consolidated_transactions': [_pago('PAGO-1', identification='1002003004')],
        'cartera_inscrip': [{'numero_id': '1002003004', 'id_inscripcion': '4321PN'}],
        'pagos_apartados': [{'matching_key': 'PAGO-1', 'tipo': 'matricula',
                             'incp_resuelto': None}],
    }

    capturado = mundo_filtrable(mundo, solo='PAGO-1')
    assert _fila(capturado, 'PAGO-1') is None, 'un pago apartado volvió al cruce solo'


@pytest.mark.parametrize('estado', ['pendiente', 'no_identificable'])
def test_una_exclusion_de_incp_reabre_la_fila(mundo_filtrable, estado):
    """El botón "Descartar número" de Excepciones.

    Descarta una de las inscripciones candidatas de un documento ambiguo, para
    que quede una sola y la fila pueda cerrar. El estado real cuando ese botón
    aparece es `pendiente` + `cruce_ambiguo`; se prueba también sobre
    `no_identificable` porque una persona pudo haberla cerrado antes.

    (Sobre una fila `cruzado` NO se reabre, y está bien: ya está resuelta, no
    hay ambigüedad que desempatar. Ese botón tampoco se ofrece ahí.)
    """
    mundo = {
        'consolidated_transactions': [_pago('PAGO-1', identification='1002003004')],
        'cartera_inscrip': [
            {'numero_id': '1002003004', 'id_inscripcion': '3300PN'},
            {'numero_id': '1002003004', 'id_inscripcion': '4400PN'},
        ],
        'cruce_incp_exclusiones': [{'identification': '1002003004',
                                    'id_inscripcion_excluido': '4400PN'}],
        'cruce_cartera': [{'matching_key': 'PAGO-1', 'estado_cruce': estado,
                           'incp': None, 'identification': '1002003004'}],
    }

    fila = _fila(mundo_filtrable(mundo, solo='PAGO-1'), 'PAGO-1')
    assert fila is not None, (
        f'con estado {estado} la fila no volvió a cruzarse: "Descartar número" '
        f'se quedaría sin efecto'
    )
    assert fila['incp'] == '3300PN', 'no tomó el candidato que quedó tras la exclusión'
