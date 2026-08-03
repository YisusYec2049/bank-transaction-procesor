"""
La aplicación de pagos sobre las cuotas, de punta a punta.

Corre el `main()` real de `cruzar_cartera_preventiva.py`. Es **el paso que mueve
plata**: decide qué cuota queda saldada con qué pago. Los bugs de este archivo
son los caros — el del 17 de julio hizo que la Fase 4 no aplicara un solo pago
en producción durante días, y nadie se enteró porque no fallaba: no hacía nada.
"""

import cruzar_cartera_preventiva as ccp

_IDS: dict[str, int] = {}


def _id_de(llave: str) -> int:
    """Id estable por llave. No se usa `hash()`: Python lo aleatoriza entre
    procesos, así que los ids cambiarían de una corrida de pytest a otra."""
    return _IDS.setdefault(llave, len(_IDS) + 1)


def _cuota(llave, inscrip, documento, valor, vence, **extra):
    return {
        'id': _id_de(llave), 'llave': llave, 'inscrip': inscrip,
        'cruce_access': documento, 'correo': 'alguien@example.com',
        'fecha_vencimiento': vence, 'valor_cuota': valor, 'valor_a_cobrar': valor,
        'cliente': 'PERSONA DE PRUEBA', 'sistema_financiero': None, 'moneda': 'COP',
        'programa': 'DIPLOMADO', 'fecha_pago': None, 'valor_pago': None,
        'fecha_cruce': None, 'diferencia': None, 'notificacion': None,
        'codigo_transaccion_1': None, 'pago': None, 'pago_confirmado': None,
        **extra,
    }


def _pago_cruzado(matching_key, documento, incp, monto, fecha='2026-08-01'):
    return {
        'matching_key': matching_key, 'identification': documento, 'incp': incp,
        'estado_cruce': 'cruzado', 'payment_amount': monto, 'payment_date': fecha,
        'email': 'alguien@example.com', 'payment_method': 'BANCOLOMBIA',
        'transaction_code_1': 'PAGO QR', 'transaction_code_2': '',
        'metodo_de_pago': None, 'program': '',
    }


def _por_llave(capturado):
    """Las filas de cierre se identifican por `id`, no por `llave` — el upsert
    solo manda las columnas del resultado del cruce, sin repetir la identidad de
    la cuota. Se traduce de vuelta para que los tests se lean por llave."""
    llave_de_id = {v: k for k, v in _IDS.items()}
    escritas = {}
    for f in capturado.get('cartera_preventiva', []):
        llave = f.get('llave') or llave_de_id.get(f.get('id'))
        if llave:
            escritas[llave] = f
    return escritas


def test_un_pago_exacto_salda_su_cuota(mundo):
    capturado = mundo(ccp, tablas={
        'cartera_preventiva': [_cuota('INS1-A', 'INS1', '1002003004', 500_000, '2026-07-01')],
        'cruce_cartera': [_pago_cruzado('PAGO-1', '1002003004', 'INS1', 500_000)],
    })

    cuota = _por_llave(capturado).get('INS1-A')
    assert cuota is not None, 'la cuota no se tocó: el pago no se aplicó'
    assert float(cuota['valor_pago']) == 500_000
    assert cuota['fecha_cruce'] is not None

    asociaciones = capturado.get('pago_asociaciones', [])
    assert any(a['matching_key'] == 'PAGO-1' and a['llave'] == 'INS1-A'
               for a in asociaciones), 'no quedó registro de qué pago cubrió qué cuota'


def test_el_pago_va_primero_a_la_cuota_mas_vieja(mundo):
    """FIFO: la deuda más antigua se cobra primero. Es la regla del proceso
    manual que este pipeline reemplaza, y no es negociable."""
    capturado = mundo(ccp, tablas={
        'cartera_preventiva': [
            _cuota('INS1-NUEVA', 'INS1', '1002003004', 500_000, '2026-09-01'),
            _cuota('INS1-VIEJA', 'INS1', '1002003004', 500_000, '2026-06-01'),
        ],
        'cruce_cartera': [_pago_cruzado('PAGO-1', '1002003004', 'INS1', 500_000)],
    })

    tocadas = _por_llave(capturado)
    assert 'INS1-VIEJA' in tocadas, 'el pago no fue a la cuota más antigua'
    assert tocadas.get('INS1-NUEVA', {}).get('valor_pago') in (None, 0), (
        'se cobró la cuota nueva teniendo una más vieja abierta'
    )


def test_un_pago_grande_cubre_varias_cuotas(mundo):
    capturado = mundo(ccp, tablas={
        'cartera_preventiva': [
            _cuota('INS1-A', 'INS1', '1002003004', 300_000, '2026-06-01'),
            _cuota('INS1-B', 'INS1', '1002003004', 300_000, '2026-07-01'),
        ],
        'cruce_cartera': [_pago_cruzado('PAGO-1', '1002003004', 'INS1', 600_000)],
    })

    tocadas = _por_llave(capturado)
    assert {'INS1-A', 'INS1-B'} <= set(tocadas), 'el pago debió cubrir las dos cuotas'

    asociado = sum(float(a['monto']) for a in capturado.get('pago_asociaciones', [])
                   if a['matching_key'] == 'PAGO-1')
    assert asociado == 600_000, 'lo repartido no suma lo que entró'


def test_dos_inscripciones_debiendo_no_se_aplica_solo(mundo):
    """Si el documento tiene dos inscripciones con deuda, el sistema no adivina.

    Es el bloqueo que se diseñó el 16 de julio a partir de un caso real (doc
    1004376520): el sistema viejo aplicaba a la inscripción equivocada. Queda
    para asociación manual.
    """
    capturado = mundo(ccp, tablas={
        'cartera_preventiva': [
            _cuota('INS1-A', 'INS1', '1002003004', 300_000, '2026-06-01'),
            _cuota('INS2-A', 'INS2', '1002003004', 300_000, '2026-06-01'),
        ],
        'cruce_cartera': [_pago_cruzado('PAGO-1', '1002003004', 'INS1', 300_000)],
    })

    asociaciones = capturado.get('pago_asociaciones', [])
    llaves = {a['llave'] for a in asociaciones}
    assert 'INS2-A' not in llaves, 'se aplicó plata a una inscripción que no era la del pago'


def test_un_pago_ya_repartido_no_se_vuelve_a_aplicar(mundo):
    """La garantía más importante del sistema: un pago se reparte una vez.

    Si esto se rompiera, cada corrida volvería a cobrar los mismos pagos sobre
    las cuotas — y como el pipeline corre solo, nadie lo vería hasta que la
    cartera estuviera destruida.
    """
    tablas = {
        'cartera_preventiva': [_cuota('INS1-A', 'INS1', '1002003004', 500_000, '2026-07-01')],
        'cruce_cartera': [_pago_cruzado('PAGO-1', '1002003004', 'INS1', 500_000)],
        'pago_asociaciones': [{'id': 1, 'matching_key': 'PAGO-1', 'llave': 'INS1-A',
                               'monto': 500_000, 'origen': 'automatico'}],
    }
    capturado = mundo(ccp, tablas=tablas)

    nuevas = [a for a in capturado.get('pago_asociaciones', [])
              if a['matching_key'] == 'PAGO-1']
    assert not nuevas, 'un pago ya repartido se volvió a aplicar'


def test_correr_dos_veces_no_cobra_dos_veces(mundo):
    """Idempotencia sobre el paso que mueve plata."""
    tablas = {
        'cartera_preventiva': [_cuota('INS1-A', 'INS1', '1002003004', 500_000, '2026-07-01')],
        'cruce_cartera': [_pago_cruzado('PAGO-1', '1002003004', 'INS1', 500_000)],
    }

    primera = mundo(ccp, tablas=tablas)
    asociaciones_1 = [(a['matching_key'], a['llave'], float(a['monto']))
                      for a in primera.get('pago_asociaciones', [])]

    # La segunda corrida ve el mundo como quedó: con la asociación ya escrita.
    tablas_despues = dict(tablas)
    tablas_despues['pago_asociaciones'] = [
        {'id': i, 'matching_key': mk, 'llave': ll, 'monto': m, 'origen': 'automatico'}
        for i, (mk, ll, m) in enumerate(asociaciones_1, start=1)
    ]
    segunda = mundo(ccp, tablas=tablas_despues)

    assert not [a for a in segunda.get('pago_asociaciones', [])
                if a['matching_key'] == 'PAGO-1'], (
        'la segunda corrida volvió a repartir el mismo pago'
    )


def test_pago_sin_cuotas_pendientes_no_rompe_nada(mundo):
    """Un pago cuyo documento no debe nada se salta sin escribir, y sigue
    elegible para corridas futuras (no se marca ni se pierde)."""
    capturado = mundo(ccp, tablas={
        'cartera_preventiva': [],
        'cruce_cartera': [_pago_cruzado('PAGO-1', '1002003004', 'INS1', 500_000)],
    })

    assert not capturado.get('pago_asociaciones')


# ── El pago que se queda atrapado en la inscripción equivocada ───────────────
#
# Caso real reportado el 3 de agosto de 2026 (doc 1090462164, Dany Yurley):
# una persona tiene DOS inscripciones —una en Access y otra en el sistema
# nuevo— y el pago cruzó contra la que NO debe nada. La plata se queda quieta:
# en Cruce de Cartera el pago se ve perfecto y en Cartera Preventiva la cuota
# sigue diciendo "sin pago identificado". Nadie las une, y nada lo señala.
#
# Medido en producción ese día: 7 pagos, $6.795.184. No es solo "Access contra
# sistema nuevo" — 2 de los 7 van al revés.


def test_un_pago_no_paga_cuotas_de_otra_inscripcion_del_mismo_documento(mundo):
    """Retrata el problema, no lo corrige.

    La regla es deliberada (16 de julio): un pago solo cubre cuotas de SU
    inscripción, para que la plata no caiga en el programa equivocado. El
    efecto secundario es este caso, y por eso hace falta una salida manual.
    """
    capturado = mundo(ccp, tablas={
        # La deuda vive en la inscripción de Access.
        'cartera_preventiva': [_cuota('40313-A', '40313', '1090462164',
                                      1_050_000, '2026-07-15')],
        # El pago cruzó contra la del sistema nuevo, que no debe nada.
        'cruce_cartera': [_pago_cruzado('PAGO-1', '1090462164', '4052PN', 1_050_000)],
    })

    assert not capturado.get('pago_asociaciones'), (
        'el pago se aplicó a una inscripción que no es la suya'
    )
    assert '40313-A' not in _por_llave(capturado), 'la cuota no debería haberse tocado'


def test_corregir_el_incp_a_mano_dirige_el_pago_a_la_cuota_correcta(mundo):
    """La salida que pidió el usuario: corregir el INCP desde la plataforma.

    Con el INCP apuntando a la inscripción que de verdad debe, la cascada
    normal hace el resto — no hace falta ninguna regla nueva en el pipeline.

    Comprueba además lo que el usuario preguntó expresamente: que el **cruce a
    la inversa** quede lleno. Se calcula sobre la cuota que el pago cubrió de
    verdad, no comparando el INCP con la inscripción, así que un INCP corregido
    a mano cuenta igual que uno resuelto por el sistema.
    """
    capturado = mundo(ccp, tablas={
        'cartera_preventiva': [_cuota('40313-A', '40313', '1090462164',
                                      1_050_000, '2026-07-15')],
        # Lo único que cambia respecto de la prueba anterior: el INCP corregido.
        'cruce_cartera': [_pago_cruzado('PAGO-1', '1090462164', '40313', 1_050_000)],
    })

    cuota = _por_llave(capturado).get('40313-A')
    assert cuota is not None, 'la cuota no se saldó con el INCP corregido'
    assert float(cuota['valor_pago']) == 1_050_000
    assert cuota['fecha_cruce'] is not None

    assert any(a['matching_key'] == 'PAGO-1' and a['llave'] == '40313-A'
               for a in capturado.get('pago_asociaciones', [])), (
        'no quedó registro de qué cuota cubrió el pago'
    )

    inverso = {u['matching_key']: u.get('cruce')
               for u in capturado.get('cruce_cartera_update', [])}
    assert inverso.get('PAGO-1'), (
        'el cruce a la inversa quedó vacío: en pantalla seguiría pareciendo '
        'que el pago no llegó a ninguna cuota'
    )
