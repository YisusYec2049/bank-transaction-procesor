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


# --------------------------------------------------------------------------
# El documento del ReportePagosWompi manda sobre el que tecleó quien pagó
# (19 de agosto). El reporte lo trae del Sistema Financiero; el CSV de WOMPI
# trae lo que la persona escribió en el formulario. Medido sobre los 835 pagos
# de los 15 reportes del Histórico: 771 idénticos, 7 realmente equivocados.
# --------------------------------------------------------------------------

def _reporte(monkeypatch, filas):
    """Pone un ReportePagosWompi en la carpeta de Drive, sin salir a la red."""
    lookup = {f['id_transaccion']: f for f in filas}
    monkeypatch.setattr(cruzar, '_cargar_lookup_wompi_reporte',
                        lambda *_a, **_k: (lookup, True, []))


def _fila_reporte(id_transaccion, documento, pagador='PERSONA DE PRUEBA',
                  inscripcion=''):
    return {'id_transaccion': id_transaccion, 'documento': documento,
            'pagador': pagador, 'comprobante': 'CI-1', 'inscripcion': inscripcion,
            'proyecto': '', 'fecha_pago': '2026-08-01'}


def _documento_escrito(capturado, matching_key):
    for f in capturado.get('consolidated_update', []):
        if f.get('matching_key') == matching_key and 'identification' in f:
            return f['identification']
    return None


def test_el_documento_del_reporte_corrige_el_que_tecleo_quien_pago(mundo, monkeypatch):
    """Caso de Karen Liseth Gómez: al pagar puso su CELULAR (3164363047) donde
    va la cédula. El reporte dice 1143858596, que es donde está su cuota."""
    _reporte(monkeypatch, [_fila_reporte('PAGO-W', 'CC-1143858596', 'Karen Liseth Gomez')])
    capturado = mundo(cruzar, tablas={
        'consolidated_transactions': [_pago('PAGO-W', identification='3164363047',
                                             metodo='WOMPI CARD')],
        'cartera_inscrip': [{'numero_id': '1143858596', 'id_inscripcion': '4399PN'}],
    })

    assert _documento_escrito(capturado, 'PAGO-W') == '1143858596', (
        'el documento equivocado quedó en el consolidado'
    )
    fila = _filas(capturado)['PAGO-W']
    assert fila['identification'] == '1143858596'
    assert fila['incp'] == '4399PN', (
        'el documento se corrigió pero el cruce de esta misma corrida usó el '
        f'viejo: {fila}'
    )


def test_la_correccion_del_reporte_deja_rastro(mundo, monkeypatch):
    """Caso del 21 de agosto (doc 1016008792, John Fredy pagando por Jenyffer):
    el reporte cambia el documento y el número de QUIEN PAGÓ —el que quedó en su
    comprobante— desaparece del sistema. Buscándolo no sale nada en ninguna de
    las tres pantallas, y nada dice que hubo una corrección.

    Con el rastro, ese número sigue siendo rastreable y la pantalla muestra
    "este número ya se corrigió antes", igual que con las correcciones a mano.
    """
    _reporte(monkeypatch, [_fila_reporte('PAGO-W', 'CC-1030581686', 'Jenyffer Rodriguez')])
    capturado = mundo(cruzar, tablas={
        'consolidated_transactions': [_pago('PAGO-W', identification='1016008792',
                                             metodo='WOMPI NEQUI')],
        'cartera_inscrip': [{'numero_id': '1030581686', 'id_inscripcion': '3852PN'}],
    })

    rastro = capturado.get('documento_correcciones', [])
    assert len(rastro) == 1, f'no quedó rastro de la corrección: {rastro}'
    assert rastro[0]['documento_original'] == '1016008792'
    assert rastro[0]['documento_corregido'] == '1030581686'
    assert rastro[0]['matching_key_original'] == 'PAGO-W'
    assert rastro[0]['origen'] == cruzar.ORIGEN_REPORTE_WOMPI, (
        'sin la firma, la corrida siguiente la lee como si la hubiera hecho una '
        'persona y el reporte se queda callado para siempre'
    )


def test_el_rastro_del_pipeline_no_es_memoria_ni_se_repite(mundo, monkeypatch):
    """El rastro dice "esto ya lo corregí yo", no "acá decidió una persona".

    Si entrara a la memoria, el bloque del reporte se saltaría ese pago y se
    perdería el reintento que arregla solo un consolidado que quedó viejo — que
    es justo lo que destapó el bug de la función de base el 21 de agosto: el
    reporte reintentaba en cada corrida porque nadie lo había anotado.
    """
    _reporte(monkeypatch, [_fila_reporte('PAGO-W', 'CC-1030581686')])
    capturado = mundo(cruzar, tablas={
        # El consolidado quedó con el documento viejo (la escritura falló).
        'consolidated_transactions': [_pago('PAGO-W', identification='1016008792',
                                             metodo='WOMPI NEQUI')],
        'documento_correcciones': [
            {'id': 1, 'documento_original': '1016008792',
             'documento_corregido': '1030581686', 'matching_key_original': 'PAGO-W',
             'origen': cruzar.ORIGEN_REPORTE_WOMPI}],
        'cartera_inscrip': [{'numero_id': '1030581686', 'id_inscripcion': '3852PN'}],
    })

    assert _documento_escrito(capturado, 'PAGO-W') == '1030581686', (
        'el rastro se leyó como una corrección a mano y el pago se quedó con el '
        'documento viejo, sin nadie que lo reintente'
    )
    assert capturado.get('documento_correcciones', []) == [], (
        'se anotó dos veces la misma corrección'
    )


def test_si_el_documento_ya_coincide_no_se_escribe_nada(mundo, monkeypatch):
    """771 de los 835 pagos del Histórico traen el mismo documento en los dos
    lados. Sobre esos no se toca la base."""
    _reporte(monkeypatch, [_fila_reporte('PAGO-W', 'CC-1002003004')])
    capturado = mundo(cruzar, tablas={
        'consolidated_transactions': [_pago('PAGO-W', identification='1002003004',
                                             metodo='WOMPI CARD')],
        'cartera_inscrip': [{'numero_id': '1002003004', 'id_inscripcion': '4321PN'}],
    })

    assert _documento_escrito(capturado, 'PAGO-W') is None, (
        'se reescribió un documento que ya estaba bien'
    )


def test_una_correccion_hecha_a_mano_le_gana_al_reporte(mundo, monkeypatch):
    """Regla del usuario: si alguien ya corrigió ese documento, manda la
    persona. El reporte no le pisa el trabajo."""
    _reporte(monkeypatch, [_fila_reporte('PAGO-W', 'CC-1143858596')])
    capturado = mundo(cruzar, tablas={
        'consolidated_transactions': [_pago('PAGO-W', identification='3164363047',
                                             metodo='WOMPI CARD')],
        'documento_correcciones': [{'id': 1, 'documento_original': '3164363047',
                                     'documento_corregido': '9999999',
                                     'matching_key_original': 'PAGO-W'}],
        'cartera_inscrip': [{'numero_id': '9999999', 'id_inscripcion': '5000PN'}],
    })

    fila = _filas(capturado)['PAGO-W']
    assert fila['identification'] == '9999999', (
        'el reporte pisó una corrección hecha a mano: quedó '
        f'{fila["identification"]}'
    )


def test_un_tipo_de_documento_raro_se_lee_igual(mundo, monkeypatch):
    """El reporte no siempre dice "CC": hay 4 pagos con el tipo `5114-`, 7 con
    `CEDULA_DE_EXTRANJERIA-`, 1 con `OTR-` y 5 con un espacio de más
    (`CC- 1012367687`). Todos traen el documento bueno detrás."""
    _reporte(monkeypatch, [
        _fila_reporte('PAGO-A', '5114-1017268987'),
        _fila_reporte('PAGO-B', 'CEDULA_DE_EXTRANJERIA-6235591'),
        _fila_reporte('PAGO-C', 'OTR-Z2036570V'),
        _fila_reporte('PAGO-D', 'CC- 1012367687'),
    ])
    capturado = mundo(cruzar, tablas={
        'consolidated_transactions': [
            _pago('PAGO-A', identification='malo1', metodo='WOMPI CARD'),
            _pago('PAGO-B', identification='malo2', metodo='WOMPI CARD'),
            _pago('PAGO-C', identification='malo3', metodo='WOMPI CARD'),
            _pago('PAGO-D', identification='malo4', metodo='WOMPI CARD'),
        ],
    })

    assert _documento_escrito(capturado, 'PAGO-A') == '1017268987'
    assert _documento_escrito(capturado, 'PAGO-B') == '6235591'
    assert _documento_escrito(capturado, 'PAGO-C') == 'Z2036570V'
    assert _documento_escrito(capturado, 'PAGO-D') == '1012367687', (
        'el espacio después del guion se coló en el documento'
    )


def test_un_documento_ilegible_no_pisa_el_que_ya_hay(mundo, monkeypatch):
    """Ante algo que no parece un documento se deja el que hay y queda avisado
    en el log — antes que escribir basura en el consolidado."""
    _reporte(monkeypatch, [_fila_reporte('PAGO-W', 'CC-')])
    capturado = mundo(cruzar, tablas={
        'consolidated_transactions': [_pago('PAGO-W', identification='1002003004',
                                             metodo='WOMPI CARD')],
    })

    assert _documento_escrito(capturado, 'PAGO-W') is None, (
        'se escribió un documento ilegible sobre el que ya estaba'
    )
    assert _filas(capturado)['PAGO-W']['identification'] == '1002003004'


def test_el_nit_se_guarda_con_su_digito_y_sigue_cruzando(mundo, monkeypatch):
    """El reporte trae `Nit-901104591-7` y el consolidado `901104591`: se guarda
    el completo, que es como lo tiene la cartera.

    Regla del usuario (20 de agosto de 2026): *"necesito que se guarde con el
    dígito y busque con el dígito"*. Hasta ese día se dejaba el corto a
    propósito, porque el índice se armaba sin dígito y buscar `901104591-7` no
    encontraba nada. Ahora la inscripción entra al índice bajo las dos formas
    (`_con_y_sin_digito`), así que las dos cosas se cumplen a la vez — y esta
    prueba vale por el segundo assert: **guardarlo no puede costar el cruce**."""
    _reporte(monkeypatch, [_fila_reporte('PAGO-W', 'Nit-901104591-7',
                                          'CONTROL FINANCIERO Y TRIBUTARIO S.A.S')])
    capturado = mundo(cruzar, tablas={
        'consolidated_transactions': [_pago('PAGO-W', identification='901104591',
                                             metodo='WOMPI CARD')],
        'cartera_inscrip': [{'numero_id': '901104591-7', 'id_inscripcion': '301PJ'}],
    })

    assert _documento_escrito(capturado, 'PAGO-W') == '901104591-7', (
        'el documento debe quedar con su dígito de verificación, como la cartera'
    )
    assert _filas(capturado)['PAGO-W']['incp'] == '301PJ', (
        'el NIT con dígito dejó de cruzar contra su inscripción'
    )


def test_el_pago_sin_digito_sigue_encontrando_al_nit_de_la_cartera(mundo, monkeypatch):
    """La otra mitad: los bancos reportan el NIT sin su dígito y la cartera lo
    guarda con él. Ese cruce es el que arregló la normalización del 8 de julio
    y **no se puede perder** al indexar también la forma completa."""
    _reporte(monkeypatch, [])
    capturado = mundo(cruzar, tablas={
        'consolidated_transactions': [_pago('PAGO-B', identification='901104591',
                                             metodo='BANCOLOMBIA')],
        'cartera_inscrip': [{'numero_id': '901104591-7', 'id_inscripcion': '301PJ'}],
    })

    assert _filas(capturado)['PAGO-B']['incp'] == '301PJ'


def test_el_nit_desempata_el_sufijo_de_la_inscripcion_del_reporte(mundo, monkeypatch):
    """El reporte da la inscripción sin sufijo (`347`) y en la cartera hay dos:
    `347PN` (una persona) y `347PJ` (la empresa). Con un NIT pagando, es la PJ.

    Caso real del 20/08/2026 (CONSTRUIR & MAS SAS, $524.688): sin este
    desempate el pago se iba a Excepciones como "no hay INCP asignada", aunque
    su propio documento ya resolviera `347PJ` sin ninguna duda."""
    _reporte(monkeypatch, [_fila_reporte('PAGO-W', 'Nit-901408499-2',
                                          'CONSTRUIR & MAS SAS', inscripcion='347')])
    capturado = mundo(cruzar, tablas={
        'consolidated_transactions': [_pago('PAGO-W', identification='901408499',
                                             metodo='WOMPI PSE')],
        'cartera_inscrip': [
            {'numero_id': '12618856',    'id_inscripcion': '347PN'},
            {'numero_id': '901408499-2', 'id_inscripcion': '347PJ'},
        ],
    })

    fila = _filas(capturado)['PAGO-W']
    assert fila['incp'] == '347PJ'
    assert fila['estado_cruce'] == 'cruzado', (
        f'quedó en Excepciones por {fila["excepcion_motivo"]!r} teniendo el INCP resuelto'
    )
    assert fila['excepcion_motivo'] is None


def test_correo_2_cae_a_la_referencia_del_extracto_cuando_la_hoja_no_sabe_del_documento(mundo):
    """Corregir el documento no puede apagar el CORREO(2) que ya funcionaba.

    En Bancolombia el correo es copia de la REFERENCIA 1 del extracto, y al
    corregir el documento el pipeline pisa ese campo con el número nuevo para
    poder buscar con él. Pero la hoja de ingresos conoce la REFERENCIA que
    reportó el banco, no el documento real.

    Caso real del 20/08/2026 (ORTEGON PROYECTOS, $721.770): `74500012486` está
    en la hoja apuntando a `330PJ`, y corregido a su NIT `901916551-7` —que la
    hoja no tiene— el CORREO(2) quedó vacío. Acá la cartera NO tiene el NIT, así
    que el INCP no puede rescatarlo: lo que se prueba es el correo solo."""
    capturado = mundo(cruzar, tablas={
        'consolidated_transactions': [_pago('PAGO-1', identification='74500012486',
                                             email='74500012486')],
        'cartera_inscrip': [],
        'cartera_ingresos_bancolombia_2576': [
            {'referencia_1': '74500012486', 'incp': '330PJ', 'fecha': '2026-05-30'},
        ],
        'documento_correcciones': [
            {'documento_original': '74500012486', 'documento_corregido': '901916551-7',
             'matching_key_original': 'PAGO-1',
             'created_at': '2026-08-20T14:53:50', 'updated_at': '2026-08-20T14:53:50'},
        ],
    })

    fila = _filas(capturado)['PAGO-1']
    assert fila['identification'] == '901916551-7', 'el documento corregido manda'
    assert fila['correo_2'] == '330PJ', (
        'corregir el documento apagó el CORREO(2) que la hoja sí sabía resolver'
    )


def test_el_documento_corregido_manda_sobre_la_referencia_vieja(mundo):
    """La caída a la referencia del extracto es un último recurso, no un empate:
    si la hoja sabe del número corregido, ese es el que vale."""
    capturado = mundo(cruzar, tablas={
        'consolidated_transactions': [_pago('PAGO-1', identification='11111111',
                                             email='11111111')],
        'cartera_inscrip': [],
        'cartera_ingresos_bancolombia_2576': [
            {'referencia_1': '11111111', 'incp': '111PN', 'fecha': '2026-05-30'},
            {'referencia_1': '22222222', 'incp': '222PN', 'fecha': '2026-05-30'},
        ],
        'documento_correcciones': [
            {'documento_original': '11111111', 'documento_corregido': '22222222',
             'matching_key_original': 'PAGO-1',
             'created_at': '2026-08-20T14:53:50', 'updated_at': '2026-08-20T14:53:50'},
        ],
    })

    assert _filas(capturado)['PAGO-1']['correo_2'] == '222PN'
