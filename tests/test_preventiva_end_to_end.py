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


# ── Pagos anteriores a la carga de cartera activa (3 de agosto) ────────────
#
# Regla del usuario: la carga de cartera es LA oportunidad. Lo que no encontró
# cuota ese día deja de aplicarse solo y queda para que una persona lo asocie.
# Nace del caso 1020838689: un pago del 16 de julio, de un ciclo de cartera
# anterior, se aplicó solo el 3 de agosto y se mezcló con el pago que sí
# correspondía.

def _carga(fecha_utc: str, estado: str = 'activa'):
    return {'fecha': fecha_utc, 'estado': estado}


def _hoy_bogota() -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo('America/Bogota')).strftime('%Y-%m-%d')


def test_un_pago_anterior_a_la_carga_no_se_aplica_solo(mundo):
    """El caso reportado. La cartera se cargó el 30 de julio y el pago es del
    16: hoy ya no es el día de la carga, así que no se toca la cuota."""
    capturado = mundo(ccp, tablas={
        'cartera_cargas': [_carga('2026-07-30T15:23:32+00:00')],
        'cartera_preventiva': [_cuota('INS1-A', 'INS1', '1002003004', 136_361, '2026-10-19')],
        'cruce_cartera': [_pago_cruzado('PAGO-VIEJO', '1002003004', 'INS1',
                                        11_561, fecha='2026-07-16')],
    })

    assert not capturado.get('pago_asociaciones'), (
        'un pago de un ciclo de cartera anterior se aplicó solo'
    )
    assert 'INS1-A' not in _por_llave(capturado), 'la cuota no debía tocarse'


def test_un_pago_posterior_a_la_carga_se_aplica_normal(mundo):
    """La contraparte, con el MISMO mundo: lo único que cambia es la fecha del
    pago. Sin esta prueba, la regla podría estar bloqueando todo."""
    capturado = mundo(ccp, tablas={
        'cartera_cargas': [_carga('2026-07-30T15:23:32+00:00')],
        'cartera_preventiva': [_cuota('INS2-A', 'INS2', '1002003005', 136_361, '2026-10-19')],
        'cruce_cartera': [_pago_cruzado('PAGO-NUEVO', '1002003005', 'INS2',
                                        136_361, fecha='2026-07-31')],
    })

    cuota = _por_llave(capturado).get('INS2-A')
    assert cuota is not None, 'un pago posterior a la carga no se aplicó'
    assert float(cuota['valor_pago']) == 136_361


def test_el_dia_de_la_carga_si_entran_los_pagos_viejos(mundo):
    """El flujo normal del cambio de cartera: llega la cartera del mes y la
    plata de las semanas previas se acomoda ese día. Medido en producción — el
    30 de julio se aplicaron así 118 pagos. Si esta prueba se cae, la regla se
    comió el cambio de cartera entero."""
    capturado = mundo(ccp, tablas={
        'cartera_cargas': [_carga(f'{_hoy_bogota()}T15:23:32+00:00')],
        'cartera_preventiva': [_cuota('INS3-A', 'INS3', '1002003006', 500_000, '2026-10-19')],
        'cruce_cartera': [_pago_cruzado('PAGO-VIEJO', '1002003006', 'INS3',
                                        500_000, fecha='2026-07-16')],
    })

    cuota = _por_llave(capturado).get('INS3-A')
    assert cuota is not None, (
        'el día de la carga no se aplicó un pago viejo: se rompe el cambio de cartera'
    )


def test_una_cuota_corta_muestra_su_deuda_aunque_tenga_saldo_a_favor_encima(mundo):
    """Caso 1020838689. A una cuota cubierta por dos pagos se le descartó uno:
    la plataforma dejó `valor_pago` en lo que quedó aplicado y en `diferencia`
    un número POSITIVO (el saldo a favor del cliente). La cuota quedaba
    debiendo $11.561 y en pantalla decía "saldo a favor $275.200".

    El pase de reconciliación se saltaba la fila porque `valor_pago` ya
    coincidía con lo asociado. Ahora también mira si la cuota está corta."""
    capturado = mundo(ccp, tablas={
        'cartera_cargas': [_carga(f'{_hoy_bogota()}T15:23:32+00:00')],
        'cartera_preventiva': [
            _cuota('INS4-A', 'INS4', '1002003007', 136_361, '2026-10-19',
                   fecha_pago='2026-07-31', valor_pago=124_800,
                   fecha_cruce='2026-08-03', diferencia=275_200),
        ],
        'pago_asociaciones': [
            {'id': 9001, 'matching_key': 'PAGO-GRANDE', 'llave': 'INS4-A',
             'monto': 124_800, 'origen': 'automatico'},
        ],
        'cartera_saldos_favor': [
            {'id': 9101, 'matching_key': 'PAGO-GRANDE', 'llave_origen': 'INS4-A', 'monto': 275_200,
             'disponible': 275_200, 'aplicado': False, 'origen': 'sobrante',
             'documento': '1002003007', 'correo': 'alguien@example.com',
             'inscrip': 'INS4', 'cliente': 'PERSONA DE PRUEBA', 'fecha': '2026-07-31'},
        ],
        'cruce_cartera': [_pago_cruzado('PAGO-GRANDE', '1002003007', 'INS4',
                                        400_000, fecha='2026-07-31')],
    })

    # Una cuota se escribe en varias pasadas (cierre, saldo a favor,
    # notificación) y cada una manda solo sus columnas. Lo que queda en la base
    # es la acumulación, así que se juntan para leer la fila como se vería.
    fila = {}
    for f in capturado.get('cartera_preventiva', []):
        if f.get('id') == _id_de('INS4-A'):
            fila.update(f)
    assert fila.get('diferencia') is not None, (
        'la cuota corta no se recalculó: la fila conserva la diferencia '
        'positiva que dejó el descarte, o sea "saldo a favor" sobre una deuda'
    )
    assert float(fila['diferencia']) == -11_561, (
        f"la cuota debe $11.561 y la fila dice {fila['diferencia']}: "
        'en pantalla parecería que sobra plata donde falta'
    )
