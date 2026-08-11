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


# ── Correcciones de documento (5 de agosto) ───────────────────────────────────
#
# Las tres pruebas de acá vienen de un mismo día en producción, en el que dos
# personas perdieron su pago por cómo se guardaban las correcciones.


def test_una_correccion_no_toca_el_pago_de_otra_persona(mundo):
    """El caso real del 5 de agosto, con sus documentos.

    Al pago de Fabián le escribieron 1115187678 por error y lo corrigieron seis
    minutos después a 98698854. Pero 1115187678 es el documento REAL de
    Alexander: como la corrección se guardaba por número y se aplicaba a todos
    los pagos que lo trajeran, el pago de Alexander quedó con el documento de
    Fabián y se fue a buscar cuotas que no eran. $695.284 que calzaban exacto
    con su cuota y no la pagaron.
    """
    capturado = mundo(cruzar, tablas={
        'consolidated_transactions': [
            _pago('PAGO-FABIAN', identification='98698854'),
            _pago('PAGO-ALEXANDER', identification='1115187678'),
        ],
        'cartera_inscrip': [
            {'numero_id': '98698854', 'id_inscripcion': '4620PN'},
            {'numero_id': '1115187678', 'id_inscripcion': '3530PN'},
        ],
        'documento_correcciones': [
            {'documento_original': '1115187678', 'documento_corregido': '98698854',
             'matching_key_original': 'PAGO-FABIAN',
             'created_at': '2026-08-05T16:19:37', 'updated_at': '2026-08-05T16:19:37'},
        ],
    })

    filas = _filas(capturado)
    assert filas['PAGO-ALEXANDER']['incp'] == '3530PN', (
        'la corrección hecha en el pago de Fabián se le aplicó al de Alexander: '
        f"quedó en {filas['PAGO-ALEXANDER']['incp']!r} en vez de su propia inscripción"
    )
    assert filas['PAGO-FABIAN']['incp'] == '4620PN'


def test_manda_la_ultima_correccion_del_pago(mundo):
    """Corregir dos veces tiene que dejar el último número, no el primero.

    El 4 de agosto corrigieron un pago a un documento equivocado, lo aplicó a
    la cuota de otra persona, y seis minutos después intentaron devolverlo. No
    se pudo: la corrección vieja seguía viva y volvía a pisar el número en cada
    corrida. $650.614 pagando la cuota de quien no era.
    """
    capturado = mundo(cruzar, tablas={
        'consolidated_transactions': [_pago('PAGO-1', identification='71275931')],
        'cartera_inscrip': [
            {'numero_id': '43186459', 'id_inscripcion': '2492PN'},
            {'numero_id': '71275931', 'id_inscripcion': '9001PN'},
        ],
        'documento_correcciones': [
            {'documento_original': '71275931',
             'documento_corregido': '43186459', 'matching_key_original': 'PAGO-1',
             'created_at': '2026-08-04T17:55:32', 'updated_at': '2026-08-04T17:55:32'},
            {'documento_original': '43186459',
             'documento_corregido': '71275931', 'matching_key_original': 'PAGO-1',
             'created_at': '2026-08-04T18:01:34', 'updated_at': '2026-08-04T18:01:34'},
        ],
    })

    fila = _filas(capturado)['PAGO-1']
    assert fila['incp'] == '9001PN', (
        f'ganó la corrección vieja: el pago quedó en {fila["incp"]!r}, o sea que '
        'devolver un documento mal corregido sigue sin funcionar'
    )


def test_la_correccion_de_un_pago_si_se_le_aplica_a_ese_pago(mundo):
    """La contracara: acotarlo al pago no puede dejarlo sin efecto.

    En Bancolombia 2576/2833 el correo es una copia literal del documento, y la
    plataforma solo corrige el documento — así que sin este paso CORREO(2)
    seguiría buscando con el número viejo.
    """
    capturado = mundo(cruzar, tablas={
        'consolidated_transactions': [
            _pago('PAGO-1', identification='11111111', email='11111111')],
        'cartera_inscrip': [{'numero_id': '22222222', 'id_inscripcion': '4321PN'}],
        'cartera_ingresos_bancolombia_2576': [
            {'referencia_1': '22222222', 'incp': '4321PN'}],
        'documento_correcciones': [
            {'documento_original': '11111111',
             'documento_corregido': '22222222', 'matching_key_original': 'PAGO-1',
             'created_at': '2026-08-05T10:00:00', 'updated_at': '2026-08-05T10:00:00'},
        ],
    })

    fila = _filas(capturado)['PAGO-1']
    assert fila['incp'] == '4321PN', 'la corrección no se aplicó a su propio pago'
    assert fila['correo_2'] == '4321PN', (
        'el correo quedó con el documento viejo: CORREO(2) seguiría buscando mal'
    )


def test_el_documento_corregido_no_se_guarda_en_la_columna_de_correo(mundo):
    """El número corregido sirve para buscar; lo que se GUARDA es el consolidado.

    Reportado el 11 de agosto sobre el documento 901759404-9: en Cartera
    Preventiva el mismo número salía en Documento y en Correo Electrónico,
    mientras el consolidado mostraba otra cosa. La causa era que la corrección
    pisaba el correo y ese valor se guardaba, viajando al cruce y de ahí a la
    cuota. Regla del usuario: *"los datos de la cartera preventiva deben ser
    los mismos que se clasifican en el consolidado"*.
    """
    capturado = mundo(cruzar, tablas={
        'consolidated_transactions': [
            _pago('PAGO-1', identification='11111111', email='11111111')],
        'cartera_inscrip': [{'numero_id': '22222222', 'id_inscripcion': '4321PN'}],
        'cartera_ingresos_bancolombia_2576': [
            {'referencia_1': '22222222', 'incp': '4321PN'}],
        'documento_correcciones': [
            {'documento_original': '11111111',
             'documento_corregido': '22222222', 'matching_key_original': 'PAGO-1',
             'created_at': '2026-08-05T10:00:00', 'updated_at': '2026-08-05T10:00:00'},
        ],
    })

    fila = _filas(capturado)['PAGO-1']
    assert fila['identification'] == '22222222', 'el documento sí se corrige'
    assert fila['email'] == '11111111', (
        f'el correo quedó en {fila["email"]!r}: se guardó el documento corregido '
        'en vez de lo que dice el consolidado'
    )
    # Y el cruce no se degrada por guardar el original: sigue cerrando.
    assert fila['correo_2'] == '4321PN'
