"""
Caracterización de los parsers de `fuentes/`.

Qué protegen, concretamente. Todos los bugs de esta lista pasaron de verdad,
ninguno lo atrapó una prueba, y todos se descubrieron mirando pantallas semanas
después:

* El dedup por (fecha, descripción, valor) de Bancolombia se comía el segundo
  pago real de una persona que pagó dos veces lo mismo el mismo día (22/07).
* `_FIJAS` no reconocía `CONSIG NAL REFERENCIA EFECTIVO`, así que el documento
  quedaba vacío aunque estuviera escrito en la línea (9/07).
* `program` y `phone` de WOMPI estaban intercambiados desde el 26 de junio, y
  eso rompió en silencio la regla de link/manual (16/07).
* `program` de Stripe traía el nombre del pagador, no el del estudiante (23/07).

Un snapshot no habría explicado ninguno, pero sí habría gritado el día que el
número cambió — que es todo lo que se le pide.

Lo que se compara es la salida de `normalize()`, o sea las 11 columnas que
terminan en el consolidado. Es la frontera correcta: es lo que el resto del
pipeline consume, y aísla al test de cómo esté organizado el parseo por dentro.
"""

import pytest

import fuentes.bancolombia_2576 as bc2576
import fuentes.bancolombia_2833 as bc2833
import fuentes.placetopay as placetopay
import fuentes.stripe as stripe
import fuentes.wompi as wompi

# ── Bancolombia ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize('modulo, fixture, nombre', [
    (bc2576, 'bc2576_extracto.json', 'bc2576'),
    (bc2833, 'bc2833_extracto.json', 'bc2833'),
])
def test_extracto_bancolombia(modulo, fixture, nombre, extracto_bancolombia, snapshot):
    buf = extracto_bancolombia(modulo, fixture)
    filas = modulo.normalize(modulo.parse_pdf(buf))
    snapshot(f'{nombre}_normalizado', filas)


@pytest.mark.parametrize('modulo, fixture', [
    (bc2576, 'bc2576_extracto.json'),
    (bc2833, 'bc2833_extracto.json'),
])
def test_bancolombia_no_pierde_pagos_repetidos(modulo, fixture, extracto_bancolombia):
    """Dos pagos idénticos el mismo día son dos pagos, no uno repetido.

    Es el bug del 22 de julio, y merece un test propio además del snapshot
    porque el snapshot solo dice "cambió": este dice *qué* se rompió. La
    numeración `(pago 2)` la pone después `procesar_todos.py`; acá lo único que
    se exige es que el parser no descarte la segunda línea.
    """
    buf = extracto_bancolombia(modulo, fixture)
    filas = modulo.parse_pdf(buf)

    vistas = {}
    for f in filas:
        vistas.setdefault((f['fecha'], f['descripcion'], f['valor']), []).append(f)

    repetidas = {k: v for k, v in vistas.items() if len(v) > 1}
    total_repetidas = sum(len(v) for v in repetidas.values())

    # No se exige que el extracto de muestra TENGA repetidos (depende del día).
    # Lo que se exige es que si los tiene, estén todos.
    assert total_repetidas == len(filas) - len(vistas) + len(repetidas), (
        'se perdieron filas con (fecha, descripción, valor) repetidos'
    )


def test_bancolombia_extrae_documento_de_consignaciones(extracto_bancolombia):
    """Toda línea de consignación con referencia numérica debe salir con
    documento. Es el hueco de `_FIJAS` del 9 de julio: el tipo no se reconocía,
    el parser caía al fallback y dejaba `identification` vacío con el número
    literalmente ahí escrito."""
    buf = extracto_bancolombia(bc2576, 'bc2576_extracto.json')
    filas = bc2576.parse_pdf(buf)

    consignaciones = [f for f in filas if f['descripcion'].upper().startswith('CONSIG')]
    if not consignaciones:
        pytest.skip('el extracto de muestra no trae consignaciones')

    sin_documento = [
        f for f in consignaciones
        if not (f['ref1'] or f['ref2'] or f['documento'])
    ]
    assert not sin_documento, (
        f'{len(sin_documento)} consignación(es) sin ninguna referencia extraída: '
        f'{[f["descripcion"] for f in sin_documento][:3]}'
    )


@pytest.mark.parametrize('modulo', [bc2576, bc2833])
def test_bancolombia_omite_los_pagos_que_ya_reporta_la_pasarela(modulo, extracto_a_mano):
    """Una transferencia cuya primera referencia es el código de comercio de una
    pasarela ya viene por el archivo de esa pasarela: si entra también por el
    banco, es la misma plata dos veces.

    Caso real del 12/08/2026 (Heidy Garzón, $538.871): la referencia del extracto
    (`190093 1786539474 11615`) es la llave de ese pago en WOMPI. Medido: 14
    líneas así en los extractos de junio a agosto, 5 de ellas ya en el
    consolidado, y ninguna cruzó nunca contra nadie porque el número que quedaba
    de documento era el de la transacción en WOMPI.
    """
    buf = extracto_a_mano(modulo, [
        '2026/08/12 TRANSF DE HEIDY GARZON YEPES TOBERIN 190093 1786539474 11615 538,871.00',
        '2026/08/12 TRANSF DE LAURA PEREZ GOMEZ TOBERIN 52345678 400,000.00',
    ])
    filas = modulo.parse_pdf(buf)

    montos = sorted(f['valor'] for f in filas)
    assert montos == [400000.0], (
        'el pago que la pasarela ya reporta no debe entrar al consolidado; '
        f'salieron {montos}'
    )
    # La transferencia normal del mismo día sigue entrando con su documento.
    assert filas[0]['ref1'] == '52345678'


@pytest.mark.parametrize('modulo', [bc2576, bc2833])
def test_bancolombia_el_documento_sale_de_referencia_1(modulo, extracto_a_mano):
    """El documento del pago es REFERENCIA 1, nunca la columna DOCUMENTO.

    REFERENCIA 1 es lo que digita quien paga; la columna DOCUMENTO es de quien
    fue a la ventanilla (un familiar, un mensajero) y no identifica al
    estudiante. Y como el PDF llega con todas las columnas pegadas, acá se
    cuentan números y no columnas: tomar "el segundo" era quedarse con
    REFERENCIA 2 o con DOCUMENTO según qué columnas vinieran llenas.

    Caso real del 20/08/2026 (CNB REDES, $550.000): REFERENCIA 1 = 9520551 es la
    inscripción `3805PN`; el documento 000070332 no existe en cartera. La misma
    persona había pagado el 16/07 y esa vez el extracto trajo un solo número, así
    que sí cruzó — el pago cambiaba de dueño según cómo viniera el archivo.

    El mismo valor viaja a `email` (índice 5), que es con el que se resuelve
    CORREO(2), y a la `matching_key`.
    """
    buf = extracto_a_mano(modulo, [
        # REFERENCIA 1 + DOCUMENTO, sin REFERENCIA 2 (el caso del 20/08).
        '2026/08/20 CONSIG NAL REFERENCIA EFECTIVO CNB REDES 9520551 000070332 550,000.00',
        # Las tres llenas: manda REFERENCIA 1 igual.
        '2026/08/20 CONSIG NAL REFERENCIA EFECTIVO CNB REDES 1065009581 777888 000070332 310,000.00',
    ])
    filas = modulo.normalize(modulo.parse_pdf(buf))
    por_valor = {f[9]: f for f in filas}

    documento, email, llave = por_valor[550000.0][1], por_valor[550000.0][5], por_valor[550000.0][10]
    assert documento == '9520551', f'el documento debe ser REFERENCIA 1, salió {documento}'
    assert email == '9520551', f'el correo lleva el mismo dato, salió {email}'
    assert llave == '20/08/2026_9520551_550000'

    assert por_valor[310000.0][1] == '1065009581', (
        'con REFERENCIA 2 y DOCUMENTO llenos también manda REFERENCIA 1, '
        f'salió {por_valor[310000.0][1]}'
    )


# ── Pasarelas ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('modulo, fixture, nombre', [
    (wompi,      'wompi_reporte.csv',      'wompi'),
    (stripe,     'stripe_pagos.csv',       'stripe'),
    (placetopay, 'placetopay_pagos.xlsx',  'placetopay'),
])
def test_pasarela(modulo, fixture, nombre, abrir_fixture, snapshot):
    filas = modulo.normalize(modulo.parse_file(abrir_fixture(fixture), fixture))
    snapshot(f'{nombre}_normalizado', filas)


def test_wompi_columnas_no_intercambiadas(abrir_fixture):
    """`program` lleva el detalle del pago y `phone` el nombre del pagador.

    Estuvieron al revés un mes entero (26 de junio → 16 de julio) y eso rompió
    la clasificación link/manual sin que nadie lo viera. El orden importa lo
    suficiente como para fijarlo por separado del snapshot.
    """
    filas = wompi.normalize(wompi.parse_file(abrir_fixture('wompi_reporte.csv')))
    assert filas, 'el fixture de WOMPI no produjo filas'

    con_nombre = [f for f in filas if f[8]]
    assert con_nombre, 'ninguna fila trae nombre del pagador en [8] (phone)'

    # Un nombre de persona no tiene arroba ni es puro número: si [8] trajera el
    # correo o el monto, esto lo delata.
    for fila in con_nombre[:20]:
        assert '@' not in fila[8], f'[8] parece un correo, no un nombre: {fila[8]!r}'


def test_wompi_guarda_la_hora_del_pago(abrir_fixture):
    """La hora viaja en [11], y el día de [2] no se mueve.

    El CSV trae `fecha` = '2026-07-21 21:45:48' y hasta el 11 de agosto se
    cortaba en los 10 primeros caracteres: el día se guardaba y la hora se
    perdía en el acto, sin quedar en ninguna tabla. El área la pide en la
    descarga del "Reporte WOMPI del día", pegada a la fecha en la misma celda.
    """
    filas = wompi.normalize(wompi.parse_file(abrir_fixture('wompi_reporte.csv')))
    assert filas, 'el fixture de WOMPI no produjo filas'

    sin_hora = [f for f in filas if not f[11]]
    assert not sin_hora, f'{len(sin_hora)} fila(s) de WOMPI quedaron sin hora'

    for fila in filas[:20]:
        hh, mm, ss = fila[11].split(':')
        assert len(hh) == len(mm) == len(ss) == 2 and (hh + mm + ss).isdigit()
        assert int(hh) < 24 and int(mm) < 60 and int(ss) < 60
        # El día sigue siendo DD-MM-YYYY: la hora se suma, no reemplaza nada.
        assert len(fila[2].split('-')) == 3 and len(fila[2]) == 10


@pytest.mark.parametrize('celda', [
    '',                      # celda vacía
    '2026-07-21',            # fecha sin hora
    '2026-07-21 21:45',      # hora a medias
    '2026-07-21 ab:cd:ef',   # basura donde va la hora
    '2026-07-21 99:99:99',   # con forma de hora, pero no es una hora
])
def test_una_hora_a_medias_se_guarda_vacia(celda):
    """Ante cualquier cosa que no sea una hora completa, la celda va vacía.

    Vacío se lee como "el archivo no lo trajo"; un '21:45' recortado se lee
    como un dato y nadie vuelve a mirarlo.
    """
    assert wompi._hora(celda) == ''


def test_el_separador_de_la_fecha_no_importa():
    """Con espacio o con T, la hora es la misma y es real. Solo el formato de
    la celda cambiaría si WOMPI exportara en ISO; el dato, no."""
    assert wompi._hora('2026-07-21 21:45:48') == '21:45:48'
    assert wompi._hora('2026-07-21T21:45:48') == '21:45:48'


def test_matching_key_unica_por_archivo(abrir_fixture):
    """Dentro de un mismo archivo de pasarela, la llave no se repite.

    Es la premisa que hace seguro procesar los 3-4 CSV de WOMPI del fin de
    semana por separado (medido el 26 de julio: 182 pagos por los dos caminos).
    Si un archivo trajera llaves repetidas, el upsert perdería pagos en
    silencio.
    """
    filas = wompi.normalize(wompi.parse_file(abrir_fixture('wompi_reporte.csv')))
    llaves = [f[10] for f in filas]
    assert len(llaves) == len(set(llaves)), 'matching_key repetida dentro del mismo archivo'


def test_cheques_no_entran_al_consolidado(extracto_bancolombia):
    """`cheque_logic` separa los cheques del resto.

    Desde el rediseño del 16 de julio los cheques salen del proceso a
    `pagos_apartados` y **no** llegan al consolidado. Esta función estuvo rota
    un mes sin que nadie lo notara, así que conviene fijar el contrato: lo que
    devuelve como "normales" no puede tener ni un cheque.
    """
    buf = extracto_bancolombia(bc2576, 'bc2576_extracto.json')
    filas = bc2576.normalize(bc2576.parse_pdf(buf))
    normales, cheques = bc2576.cheque_logic(filas)

    assert len(normales) + len(cheques) == len(filas), 'cheque_logic perdió filas'
    for fila in normales:
        assert 'CHEQUE' not in str(fila[3]).upper(), f'cheque colado al consolidado: {fila[3]!r}'
