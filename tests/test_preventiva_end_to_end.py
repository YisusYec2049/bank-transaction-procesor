"""
La aplicación de pagos sobre las cuotas, de punta a punta.

Corre el `main()` real de `cruzar_cartera_preventiva.py`. Es **el paso que mueve
plata**: decide qué cuota queda saldada con qué pago. Los bugs de este archivo
son los caros — el del 17 de julio hizo que la Fase 4 no aplicara un solo pago
en producción durante días, y nadie se enteró porque no fallaba: no hacía nada.
"""

import cruzar_cartera_preventiva as ccp

_IDS: dict[str, int] = {}


def _hoy_bogota() -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo('America/Bogota')).strftime('%Y-%m-%d')


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
    # `registration_date` = hoy por defecto: un pago se reparte el día que entra
    # al sistema (regla del 3 de agosto), así que el pago "normal" de un test es
    # uno que entró hoy. Los tests de esa regla lo sobrescriben a propósito.
    return {
        'matching_key': matching_key, 'identification': documento, 'incp': incp,
        'estado_cruce': 'cruzado', 'payment_amount': monto, 'payment_date': fecha,
        'registration_date': _hoy_bogota(),
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


# ── Un pago se reparte el día que entra (3 de agosto) ─────────────────────
#
# Regla del usuario: si un pago no se asignó el día que entró al sistema, no se
# asigna solo nunca más. Nace del caso 1022389618: pagó el 16 de julio sin tener
# cuota, y el 30 de julio la cartera nueva le trajo una — el pago viejo cayó ahí
# solo, y cuando pagó de verdad esa cuota el 2 de agosto su plata ya no tenía
# dónde entrar.


def _carga(fecha_utc: str, estado: str = 'activa'):
    return {'fecha': fecha_utc, 'estado': estado}


def test_un_pago_que_no_entro_hoy_no_se_aplica_solo(mundo):
    """El caso reportado: el pago entró hace días y hoy le aparece una cuota."""
    pago = _pago_cruzado('PAGO-VIEJO', '1002003004', 'INS1', 503_125,
                         fecha='2026-07-16')
    pago['registration_date'] = '2026-07-17'
    capturado = mundo(ccp, tablas={
        'cartera_cargas': [_carga(f'{_hoy_bogota()}T15:23:32+00:00')],
        'cartera_preventiva': [_cuota('INS1-A', 'INS1', '1002003004', 503_125,
                                      '2026-08-13')],
        'cruce_cartera': [pago],
    })

    assert not capturado.get('pago_asociaciones'), (
        'un pago que entró hace días cayó solo sobre una cuota que apareció después'
    )
    assert 'INS1-A' not in _por_llave(capturado), 'la cuota no debía tocarse'


def test_un_pago_que_entro_hoy_se_aplica_normal(mundo):
    """La contraparte, con el MISMO mundo: lo único que cambia es el día en que
    el pago entró al sistema. Sin esto, la regla podría estar bloqueando todo."""
    pago = _pago_cruzado('PAGO-DE-HOY', '1002003005', 'INS2', 503_125,
                         fecha='2026-08-02')
    pago['registration_date'] = _hoy_bogota()
    capturado = mundo(ccp, tablas={
        'cartera_cargas': [_carga(f'{_hoy_bogota()}T15:23:32+00:00')],
        'cartera_preventiva': [_cuota('INS2-A', 'INS2', '1002003005', 503_125,
                                      '2026-08-13')],
        'cruce_cartera': [pago],
    })

    cuota = _por_llave(capturado).get('INS2-A')
    assert cuota is not None, 'un pago que entró hoy no se aplicó'
    assert float(cuota['valor_pago']) == 503_125


def test_el_dia_de_la_carga_tampoco_rescata_un_pago_viejo(mundo):
    """La puerta que el usuario mandó cerrar. Antes, el día en que se cargaba la
    cartera del mes entraba toda la plata vieja de golpe (84 pagos el 30 de
    julio) — justo cuando el área ya ajustó los pagos a mano en ese Excel."""
    pago = _pago_cruzado('PAGO-VIEJO', '1002003006', 'INS3', 500_000,
                         fecha='2026-07-16')
    pago['registration_date'] = '2026-07-17'
    capturado = mundo(ccp, tablas={
        # La cartera se carga HOY: aun así el pago viejo no entra.
        'cartera_cargas': [_carga(f'{_hoy_bogota()}T15:23:32+00:00')],
        'cartera_preventiva': [_cuota('INS3-A', 'INS3', '1002003006', 500_000,
                                      '2026-08-13')],
        'cruce_cartera': [pago],
    })

    assert not capturado.get('pago_asociaciones'), (
        'el día de la carga volvió a entrar plata vieja'
    )


def test_un_pago_sin_fecha_de_ingreso_no_se_bloquea(mundo):
    """Sin el dato no se decide en contra: perder plata en silencio es peor que
    aplicarla de más, que al menos se ve."""
    pago = _pago_cruzado('PAGO-SIN-FECHA', '1002003009', 'INS6', 500_000)
    pago['registration_date'] = None
    capturado = mundo(ccp, tablas={
        'cartera_cargas': [_carga(f'{_hoy_bogota()}T15:23:32+00:00')],
        'cartera_preventiva': [_cuota('INS6-A', 'INS6', '1002003009', 500_000,
                                      '2026-08-13')],
        'cruce_cartera': [pago],
    })

    assert _por_llave(capturado).get('INS6-A') is not None, (
        'se bloqueó un pago por no traer fecha de ingreso'
    )


# ── El sello: la ventana de un pago y su cierre (5 de agosto) ────────────────
#
# La ventana va desde que el pago entra hasta la corrida diaria del día
# siguiente. Al terminar esa corrida queda sellado y no vuelve a mover plata
# nunca — ni solo, ni a mano, ni aunque después aparezca una cuota que calce
# exacto. Sellar de más congela plata de gente que debe; sellar de menos deja
# que la cartera del mes se pague con dinero de otro mes.

def _dia_relativo(dias: int) -> str:
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    return (datetime.now(ZoneInfo('America/Bogota'))
            + timedelta(days=dias)).strftime('%Y-%m-%d')


def _mundo_de_un_pago(pago, cuotas=None):
    return {
        'cartera_cargas': [_carga(f'{_hoy_bogota()}T15:23:32+00:00')],
        'cartera_preventiva': cuotas if cuotas is not None else [],
        'cruce_cartera': [pago],
    }


def test_un_pago_sellado_no_se_aplica_aunque_la_cuota_calce_exacto(mundo):
    """El corazón de la regla. La cuota calza al peso y aun así no se paga."""
    pago = _pago_cruzado('PAGO-SELLADO', '1002003100', 'INS10', 500_000)
    pago['registration_date'] = _dia_relativo(-1)
    pago['aplicacion_cerrada_at'] = _dia_relativo(-1)
    capturado = mundo(ccp, tablas=_mundo_de_un_pago(
        pago, [_cuota('INS10-A', 'INS10', '1002003100', 500_000, '2026-08-13')]))

    assert not capturado.get('pago_asociaciones'), (
        'un pago sellado volvió a pagar una cuota'
    )
    assert 'INS10-A' not in _por_llave(capturado), 'la cuota no debía tocarse'


def test_un_pago_de_ayer_todavia_alcanza_la_corrida_de_hoy(mundo):
    """La ventana llega hasta la corrida diaria del día siguiente: el pago de
    ayer tiene acá su última oportunidad. Con el mismo mundo del test de arriba,
    y lo único que cambia es el sello."""
    pago = _pago_cruzado('PAGO-DE-AYER', '1002003101', 'INS11', 500_000)
    pago['registration_date'] = _dia_relativo(-1)
    capturado = mundo(ccp, tablas=_mundo_de_un_pago(
        pago, [_cuota('INS11-A', 'INS11', '1002003101', 500_000, '2026-08-13')]))

    cuota = _por_llave(capturado).get('INS11-A')
    assert cuota is not None, 'el pago de ayer no alcanzó la corrida de hoy'
    assert float(cuota['valor_pago']) == 500_000


def test_un_pago_de_anteayer_ya_no_entra(mundo):
    """El otro borde: dos días es tarde aunque nadie lo haya sellado todavía.
    Es la red que evita que, el día que se agregue la columna, TODO lo viejo
    quede elegible de golpe."""
    pago = _pago_cruzado('PAGO-DE-ANTEAYER', '1002003102', 'INS12', 500_000)
    pago['registration_date'] = _dia_relativo(-2)
    capturado = mundo(ccp, tablas=_mundo_de_un_pago(
        pago, [_cuota('INS12-A', 'INS12', '1002003102', 500_000, '2026-08-13')]))

    assert not capturado.get('pago_asociaciones'), 'entró un pago de anteayer'


def test_el_cierre_diario_sella_el_pago_que_se_quedo_sin_cuota(mundo):
    pago = _pago_cruzado('PAGO-SIN-CUOTA', '1002003103', 'INS13', 500_000)
    pago['registration_date'] = _dia_relativo(-1)
    capturado = mundo(ccp, tablas=_mundo_de_un_pago(pago),
                       argv=['--cierre-diario'])

    assert capturado.get('sellados') == ['PAGO-SIN-CUOTA']
    assert capturado.get('sellados_fecha') == _hoy_bogota()


def test_un_reproceso_manual_no_sella_nada(mundo):
    """La protección crítica: el mismo script lo corren los reprocesos que
    dispara la plataforma y el vigilante de la tarde. Si sellaran, un reproceso
    de las 08:00 mataría los pagos de ayer ANTES de que la corrida de las 10:30
    les dé su última oportunidad — lo contrario de la regla."""
    pago = _pago_cruzado('PAGO-SIN-CUOTA', '1002003104', 'INS14', 500_000)
    pago['registration_date'] = _dia_relativo(-1)
    capturado = mundo(ccp, tablas=_mundo_de_un_pago(pago))

    assert not capturado.get('sellados'), 'un reproceso manual selló pagos'


def test_el_cierre_diario_no_sella_un_pago_que_entro_hoy(mundo):
    """Todavía le queda la corrida de mañana."""
    pago = _pago_cruzado('PAGO-DE-HOY', '1002003105', 'INS15', 500_000)
    pago['registration_date'] = _hoy_bogota()
    capturado = mundo(ccp, tablas=_mundo_de_un_pago(pago),
                       argv=['--cierre-diario'])

    assert not capturado.get('sellados'), 'se selló un pago en su propio día'


def test_el_cierre_diario_no_sella_un_pago_que_si_pago_su_cuota(mundo):
    pago = _pago_cruzado('PAGO-APLICADO', '1002003106', 'INS16', 500_000)
    pago['registration_date'] = _dia_relativo(-1)
    capturado = mundo(ccp, tablas=_mundo_de_un_pago(
        pago, [_cuota('INS16-A', 'INS16', '1002003106', 500_000, '2026-08-13')]),
        argv=['--cierre-diario'])

    assert _por_llave(capturado).get('INS16-A') is not None, 'el pago no se aplicó'
    assert not capturado.get('sellados'), 'se selló un pago que sí pagó una cuota'


def test_el_cierre_diario_no_sella_un_pago_con_sobrante(mundo):
    """El sobrante tiene su propio reloj: vive mientras se opera y muere en el
    cierre de cartera, no acá. Sellarlo con el pago apagaría el 'Asociar saldo'
    que el área usa todos los días."""
    pago = _pago_cruzado('PAGO-CON-SOBRANTE', '1002003107', 'INS17', 800_000)
    pago['registration_date'] = _dia_relativo(-1)
    tablas = _mundo_de_un_pago(pago)
    tablas['cartera_saldos_favor'] = [{
        'id': 900, 'inscrip': 'INS17', 'cliente': 'PERSONA DE PRUEBA',
        'documento': '1002003107', 'correo': 'alguien@example.com',
        'monto': 300_000, 'disponible': 300_000, 'origen': 'sobrante',
        'llave_origen': 'INS17-A', 'matching_key': 'PAGO-CON-SOBRANTE',
        'fecha': _dia_relativo(-1), 'aplicado': False,
    }]
    capturado = mundo(ccp, tablas=tablas, argv=['--cierre-diario'])

    assert not capturado.get('sellados'), 'se selló un pago que todavía tiene sobrante'


def test_el_cierre_diario_no_sella_un_pago_aplicado_bajo_la_cartera_ANTERIOR(mundo):
    """Un pago que pagó cuotas bajo la cartera de julio SÍ usó su oportunidad:
    su registro vive en `pago_asociaciones_archivo` porque el swap lo archivó.
    Sellarlo sería decir que nunca la usó.

    Lo cazó la simulación en el VPS (5 de agosto): sin este descarte se sellaban
    550 pagos en vez de 16, y 534 eran justamente estos."""
    pago = _pago_cruzado('PAGO-DE-CARTERA-VIEJA', '1002003109', 'INS19', 500_000)
    pago['registration_date'] = _dia_relativo(-1)
    tablas = _mundo_de_un_pago(pago)
    tablas['pago_asociaciones_archivo'] = [{
        'matching_key': 'PAGO-DE-CARTERA-VIEJA',
        'created_at': f'{_dia_relativo(-15)}T15:00:00+00:00',
    }]
    capturado = mundo(ccp, tablas=tablas, argv=['--cierre-diario'])

    assert not capturado.get('sellados'), (
        'se selló un pago que ya había pagado cuotas bajo la cartera anterior'
    )


def test_el_cierre_diario_no_sella_un_pago_que_el_excel_VIEJO_trae_cobrado(mundo):
    """El proceso manual anota en el Excel el pago que cobró
    (`codigo_transaccion_1`). Ese pago está aplicado, solo que su registro no
    vive en `pago_asociaciones` — y cuando esa cartera se archiva, la única
    memoria de que se cobró queda en `cartera_preventiva_archivo`.

    La cartera VIVA no sirve para probar esto: por ahí el pago ya queda cubierto
    al armar las llaves de cada pago, así que el test pasaría igual con el
    descarte quitado. Lo destapó una mutación."""
    pago = _pago_cruzado('PAGO-COBRADO-EN-EXCEL', '1002003110', 'INS20', 500_000)
    pago['registration_date'] = _dia_relativo(-1)
    tablas = _mundo_de_un_pago(pago)
    tablas['cartera_preventiva_archivo'] = [
        {'codigo_transaccion_1': 'PAGO-COBRADO-EN-EXCEL'}]
    capturado = mundo(ccp, tablas=tablas, argv=['--cierre-diario'])

    assert not capturado.get('sellados'), (
        'se selló un pago que una cartera anterior ya traía cobrado a mano'
    )


def test_el_cierre_diario_no_sella_los_pagos_de_excepciones(mundo):
    """Decisión del usuario (5 de agosto): mientras un pago está sin identificar
    no es un pago listo que se ignoró, es trabajo pendiente — y corregirle el
    INCP días después tiene que seguir sirviendo."""
    pago = _pago_cruzado('PAGO-EN-EXCEPCIONES', '1002003108', 'INS18', 500_000)
    pago['registration_date'] = _dia_relativo(-1)
    pago['estado_cruce'] = 'pendiente'
    capturado = mundo(ccp, tablas=_mundo_de_un_pago(pago),
                       argv=['--cierre-diario'])

    assert not capturado.get('sellados'), 'se selló un pago que sigue en Excepciones'


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


# ── El aviso de plata sin repartir (3 de agosto) ───────────────────────────
#
# Regla del usuario: el aviso vive mientras al pago le quede plata sin
# repartir por encima del umbral, y se para en la última cuota a la que se le
# asignó. Antes era todo o nada: repartir un peso lo apagaba entero.

def _mundo_con_saldo(disponible: float, monto: float = 275_200):
    """Una cuota de $136.361 cubierta por un pago que dejó `monto` de sobrante,
    del cual ya se repartió lo que falte hasta `disponible`.

    El monto del pago se deriva para que la plata CUADRE (lo aplicado + lo
    disponible == lo que entró); si no, el chequeo de cuadre del propio
    pipeline llena el log de errores y un descuadre de verdad pasaría
    desapercibido entre ellos."""
    return {
        'cartera_cargas': [_carga(f'{_hoy_bogota()}T15:23:32+00:00')],
        'cartera_preventiva': [
            _cuota('INS5-A', 'INS5', '1002003008', 136_361, '2026-10-19',
                   fecha_pago='2026-07-31', valor_pago=136_361,
                   fecha_cruce='2026-08-03', diferencia=disponible),
        ],
        'pago_asociaciones': [
            {'id': 9201, 'matching_key': 'PAGO-GRANDE', 'llave': 'INS5-A',
             'monto': 136_361, 'origen': 'automatico'},
        ],
        'cartera_saldos_favor': [
            {'id': 9301, 'matching_key': 'PAGO-GRANDE', 'llave_origen': 'INS5-A',
             'monto': monto, 'disponible': disponible, 'aplicado': False,
             'origen': 'sobrante', 'documento': '1002003008',
             'correo': 'alguien@example.com', 'inscrip': 'INS5',
             'cliente': 'PERSONA DE PRUEBA', 'fecha': '2026-07-31'},
        ],
        'cruce_cartera': [_pago_cruzado('PAGO-GRANDE', '1002003008', 'INS5',
                                        136_361 + disponible, fecha='2026-07-31')],
    }


def _notificacion_de(capturado, llave):
    valor = '__sin_escribir__'
    for f in capturado.get('cartera_preventiva', []):
        if f.get('id') == _id_de(llave) and 'notificacion' in f:
            valor = f['notificacion']
    return valor


def test_el_aviso_sigue_aunque_se_haya_repartido_una_parte(mundo):
    """Caso 1020838689: de $275.200 se asignaron $11.561 y el aviso se apagaba
    con $263.639 —casi dos cuotas— todavía sin repartir."""
    capturado = mundo(ccp, tablas=_mundo_con_saldo(263_639))

    assert _notificacion_de(capturado, 'INS5-A') == '2 CUOTAS + ABONO', (
        'el aviso se apagó teniendo $263.639 sin repartir'
    )


def test_el_aviso_se_apaga_cuando_lo_que_queda_no_vale_la_pena(mundo):
    """El umbral de $1.000 sigue mandando: un resto de redondeo no avisa.
    Sin esta prueba, quitar el "todo o nada" podría dejar avisos por $2."""
    capturado = mundo(ccp, tablas=_mundo_con_saldo(500, monto=500))

    assert _notificacion_de(capturado, 'INS5-A') in (None, '__sin_escribir__'), (
        'avisó por $500, que es ruido de redondeo'
    )


def test_un_pago_que_no_paga_ninguna_cuota_no_avisa_nada(mundo):
    """Caso 1022389618. La cuota la pagó exacto el pago del 2 de agosto; el de
    julio se había descartado de esa misma cuota y quedó como plata libre.

    La fila decía "PAGA DOS CUOTAS": contaba la cuota donde se paraba —que ese
    pago ya no pagaba— más lo que sobraba. El aviso habla de un pago que pagó
    cuotas, y ese pago no paga ninguna."""
    pago_bueno = _pago_cruzado('PAGO-DE-HOY', '1002003010', 'INS7', 503_125,
                               fecha='2026-08-02')
    pago_descartado = _pago_cruzado('PAGO-VIEJO', '1002003010', 'INS7', 503_125,
                                    fecha='2026-07-16')
    pago_descartado['registration_date'] = '2026-07-17'
    capturado = mundo(ccp, tablas={
        'cartera_cargas': [_carga(f'{_hoy_bogota()}T15:23:32+00:00')],
        'cartera_preventiva': [
            _cuota('INS7-A', 'INS7', '1002003010', 503_125, '2026-08-13',
                   fecha_pago='2026-08-02', valor_pago=503_125,
                   fecha_cruce=_hoy_bogota(), diferencia=0),
        ],
        'pago_asociaciones': [
            {'id': 9401, 'matching_key': 'PAGO-DE-HOY', 'llave': 'INS7-A',
             'monto': 503_125, 'origen': 'automatico'},
        ],
        # El descarte dejó la plata del pago viejo anclada a esa misma cuota.
        'cartera_saldos_favor': [
            {'id': 9501, 'matching_key': 'PAGO-VIEJO', 'llave_origen': 'INS7-A',
             'monto': 503_125, 'disponible': 503_125, 'aplicado': False,
             'origen': 'descarte', 'documento': '1002003010',
             'correo': 'alguien@example.com', 'inscrip': 'INS7',
             'cliente': 'PERSONA DE PRUEBA', 'fecha': '2026-07-16'},
        ],
        'cruce_cartera': [pago_bueno, pago_descartado],
    })

    assert _notificacion_de(capturado, 'INS7-A') in (None, '__sin_escribir__'), (
        'la cuota avisa sobre un pago que ya no la paga'
    )

    # Tampoco le pinta el saldo a favor encima: la cuota está pagada exacto y
    # esa plata es de un pago que no la pagó (regla del usuario, 3 de agosto).
    fila = {}
    for f in capturado.get('cartera_preventiva', []):
        if f.get('id') == _id_de('INS7-A'):
            fila.update(f)
    assert float(fila.get('diferencia') or 0) == 0, (
        f"la cuota muestra saldo a favor {fila.get('diferencia')} de un pago que no la pagó"
    )


# ── `valor_pago` muestra lo que entró por ese pago (3 de agosto) ───────────
#
# Regla del usuario: un pago de $1.000.000 contra una única cuota de $500.000
# deja la fila con `valor_pago = 1.000.000` y `diferencia = 500.000`. Se rompe
# cuando ese excedente se asocia a otra cuota: ahí cada una muestra lo suyo.

def _valor_pago_de(capturado, llave):
    valor = None
    for f in capturado.get('cartera_preventiva', []):
        if f.get('id') == _id_de(llave) and 'valor_pago' in f:
            valor = f['valor_pago']
    return valor


def test_valor_pago_muestra_el_pago_completo_si_el_excedente_no_se_repartio(mundo):
    """El ejemplo textual del usuario: entra $1.000.000, la única cuota es de
    $500.000. La fila tiene que decir que entró un millón, no medio."""
    capturado = mundo(ccp, tablas={
        'cartera_cargas': [_carga(f'{_hoy_bogota()}T15:23:32+00:00')],
        'cartera_preventiva': [_cuota('INS8-A', 'INS8', '1002003011', 500_000,
                                      '2026-08-13')],
        'cruce_cartera': [_pago_cruzado('PAGO-1M', '1002003011', 'INS8', 1_000_000)],
    })

    assert _valor_pago_de(capturado, 'INS8-A') == 1_000_000, (
        'la fila muestra lo aplicado a la cuota, no lo que pagó la persona'
    )
    # La fila se escribe en varias pasadas; lo que queda en la base es la
    # acumulación de todas.
    fila = {}
    for f in capturado.get('cartera_preventiva', []):
        if f.get('id') == _id_de('INS8-A'):
            fila.update(f)
    assert float(fila['diferencia']) == 500_000, 'el excedente debe verse en la diferencia'


def test_valor_pago_vuelve_a_lo_suyo_cuando_el_excedente_se_reparte(mundo):
    """"Se rompe eso" — el excedente ya se asoció a una segunda cuota, así que
    cada fila vuelve a mostrar lo que recibió. Entre las dos suman el millón."""
    capturado = mundo(ccp, tablas={
        'cartera_cargas': [_carga(f'{_hoy_bogota()}T15:23:32+00:00')],
        'cartera_preventiva': [
            _cuota('INS9-A', 'INS9', '1002003012', 500_000, '2026-08-13',
                   fecha_pago='2026-08-01', valor_pago=1_000_000,
                   fecha_cruce=_hoy_bogota(), diferencia=500_000),
            _cuota('INS9-B', 'INS9', '1002003012', 500_000, '2026-09-13',
                   fecha_pago='2026-08-01', valor_pago=500_000,
                   fecha_cruce=_hoy_bogota(), diferencia=0),
        ],
        'pago_asociaciones': [
            {'id': 9601, 'matching_key': 'PAGO-1M', 'llave': 'INS9-A',
             'monto': 500_000, 'origen': 'automatico'},
            {'id': 9602, 'matching_key': 'PAGO-1M', 'llave': 'INS9-B',
             'monto': 500_000, 'origen': 'manual'},
        ],
        # El saldo quedó en cero: ya no hay nada sin repartir.
        'cartera_saldos_favor': [
            {'id': 9701, 'matching_key': 'PAGO-1M', 'llave_origen': 'INS9-A',
             'monto': 500_000, 'disponible': 0, 'aplicado': True,
             'origen': 'sobrante', 'documento': '1002003012',
             'correo': 'alguien@example.com', 'inscrip': 'INS9',
             'cliente': 'PERSONA DE PRUEBA', 'fecha': '2026-08-01'},
        ],
        'cruce_cartera': [_pago_cruzado('PAGO-1M', '1002003012', 'INS9', 1_000_000)],
    })

    assert _valor_pago_de(capturado, 'INS9-A') == 500_000, (
        'la primera cuota sigue mostrando el millón después de repartir el excedente'
    )


def test_plata_aparcada_de_un_pago_ajeno_no_infla_el_valor_pago(mundo):
    """Caso 1022389618: la cuota la pagó exacto un pago, y al lado había otro
    descartado por el mismo monto. Sumarlo diría que entró el doble."""
    pago_bueno = _pago_cruzado('PAGO-BUENO', '1002003013', 'INS10', 503_125)
    pago_descartado = _pago_cruzado('PAGO-VIEJO', '1002003013', 'INS10', 503_125,
                                    fecha='2026-07-16')
    pago_descartado['registration_date'] = '2026-07-17'
    capturado = mundo(ccp, tablas={
        'cartera_cargas': [_carga(f'{_hoy_bogota()}T15:23:32+00:00')],
        'cartera_preventiva': [
            _cuota('INS10-A', 'INS10', '1002003013', 503_125, '2026-08-13',
                   fecha_pago='2026-08-02', valor_pago=503_125,
                   fecha_cruce=_hoy_bogota(), diferencia=0),
        ],
        'pago_asociaciones': [
            {'id': 9801, 'matching_key': 'PAGO-BUENO', 'llave': 'INS10-A',
             'monto': 503_125, 'origen': 'automatico'},
        ],
        'cartera_saldos_favor': [
            {'id': 9901, 'matching_key': 'PAGO-VIEJO', 'llave_origen': 'INS10-A',
             'monto': 503_125, 'disponible': 503_125, 'aplicado': False,
             'origen': 'descarte', 'documento': '1002003013',
             'correo': 'alguien@example.com', 'inscrip': 'INS10',
             'cliente': 'PERSONA DE PRUEBA', 'fecha': '2026-07-16'},
        ],
        'cruce_cartera': [pago_bueno, pago_descartado],
    })

    escrito = _valor_pago_de(capturado, 'INS10-A')
    assert escrito in (None, 503_125), (
        f'la fila dice que entraron {escrito} cuando el pago fue de 503.125'
    )


def test_el_valor_pago_inflado_no_oscila_entre_corridas(mundo):
    """El riesgo real de esta regla: la pasada que reconcilia compara
    `valor_pago` contra lo aplicado, y la que lo ajusta le suma el excedente.
    Si no hablan el mismo idioma, una lo sube y la otra lo baja en cada
    corrida, y el número parpadea para siempre.

    Este es el mundo TAL COMO QUEDA después de la corrida anterior."""
    capturado = mundo(ccp, tablas={
        'cartera_cargas': [_carga(f'{_hoy_bogota()}T15:23:32+00:00')],
        'cartera_preventiva': [
            _cuota('INS11-A', 'INS11', '1002003014', 500_000, '2026-08-13',
                   fecha_pago='2026-08-01', valor_pago=1_000_000,
                   fecha_cruce=_hoy_bogota(), diferencia=500_000),
        ],
        'pago_asociaciones': [
            {'id': 9111, 'matching_key': 'PAGO-1M', 'llave': 'INS11-A',
             'monto': 500_000, 'origen': 'automatico'},
        ],
        'cartera_saldos_favor': [
            {'id': 9222, 'matching_key': 'PAGO-1M', 'llave_origen': 'INS11-A',
             'monto': 500_000, 'disponible': 500_000, 'aplicado': False,
             'origen': 'sobrante', 'documento': '1002003014',
             'correo': 'alguien@example.com', 'inscrip': 'INS11',
             'cliente': 'PERSONA DE PRUEBA', 'fecha': '2026-08-01'},
        ],
        'cruce_cartera': [_pago_cruzado('PAGO-1M', '1002003014', 'INS11', 1_000_000)],
    })

    escrituras = [f for f in capturado.get('cartera_preventiva', [])
                  if f.get('id') == _id_de('INS11-A')
                  and ('valor_pago' in f or 'diferencia' in f)]
    assert not escrituras, (
        f'la fila ya estaba correcta y se reescribió igual: {escrituras}'
    )


# ---------------------------------------------------------------------------
# NIT con dígito de verificación (4 de agosto)
#
# Caso real que lo destapó: AMCH SAS, NIT 901317423-2, inscripción 293PJ. Pagó
# $802.125 por WOMPI y su cuota seguía diciendo "Sin pago identificado". Las dos
# tablas guardaban el documento IDÉNTICO, con su DV — la diferencia la creaba el
# propio bucle de agrupación, que normalizaba el lado de las cuotas y no el de
# los pagos. Eran 4 pagos por $4.010.625 esperando desde el 16 de julio.
#
# El DV se calcula a partir del número base (fórmula DIAN), así que no distingue
# empresas: comparar por la base es la comparación correcta, no un atajo.
# ---------------------------------------------------------------------------

def test_nit_con_digito_de_verificacion_de_los_dos_lados(mundo):
    """El caso real: cuota y pago traen el MISMO documento, con DV. Antes del
    4 de agosto no se juntaban, y no había forma de verlo en pantalla porque los
    dos datos se veían iguales."""
    capturado = mundo(ccp, tablas={
        'cartera_preventiva': [_cuota('293PJ-A', '293PJ', '901317423-2', 802_125, '2026-07-10')],
        'cruce_cartera': [_pago_cruzado('PAGO-NIT', '901317423-2', '293PJ', 802_125)],
    })

    cuota = _por_llave(capturado).get('293PJ-A')
    assert cuota is not None, 'la cuota no se tocó: el pago no encontró su documento'
    assert float(cuota['valor_pago']) == 802_125


def test_nit_con_digito_solo_en_la_cuota(mundo):
    """La combinación que motivó la normalización original (8 de julio): el
    Excel de cartera trae el NIT completo y el banco lo manda sin DV."""
    capturado = mundo(ccp, tablas={
        'cartera_preventiva': [_cuota('INS20-A', 'INS20', '860004922-4', 300_000, '2026-07-01')],
        'cruce_cartera': [_pago_cruzado('PAGO-N2', '860004922', 'INS20', 300_000)],
    })

    assert _por_llave(capturado).get('INS20-A') is not None, (
        'un pago sin DV dejó de encontrar su cuota con DV'
    )


def test_nit_con_digito_solo_en_el_pago(mundo):
    """La combinación inversa: el documento se corrigió a mano con el NIT
    completo y la cuota quedó sin DV."""
    capturado = mundo(ccp, tablas={
        'cartera_preventiva': [_cuota('INS21-A', 'INS21', '860004922', 300_000, '2026-07-01')],
        'cruce_cartera': [_pago_cruzado('PAGO-N3', '860004922-4', 'INS21', 300_000)],
    })

    assert _por_llave(capturado).get('INS21-A') is not None, (
        'un pago con DV dejó de encontrar su cuota sin DV'
    )


def test_normalizar_el_dv_no_junta_dos_documentos_distintos(mundo):
    """Salvaguarda: quitar el DV no puede hacer que el pago de una empresa caiga
    en la cuota de otra. Sin `-<un dígito>` al final no se toca nada, así que
    "9013174232" (diez dígitos, sin guion) sigue siendo otro documento."""
    capturado = mundo(ccp, tablas={
        'cartera_preventiva': [_cuota('INS22-A', 'INS22', '9013174232', 500_000, '2026-07-01')],
        'cruce_cartera': [_pago_cruzado('PAGO-N4', '901317423-2', 'INS22', 500_000)],
    })

    assert _por_llave(capturado).get('INS22-A') is None, (
        'el pago cayó en la cuota de un documento distinto'
    )
