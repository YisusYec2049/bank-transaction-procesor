"""El emparejamiento de los dos archivos de PayU.

PayU necesita DOS archivos para producir un pago, y hasta ahora se juntaban por
ORDEN DE LLEGADA: el primero de una bandeja con el primero de la otra. Eso es
correcto solo si siempre se suben juntos y en orden, y está anotado como riesgo
desde agosto — un par mal armado no falla, produce pagos con el monto de otra
tanda.

La pantalla de carga sube los dos con el mismo prefijo de lote, y eso permite
emparejarlos de forma explícita.
"""

from procesar_todos import _emparejar_payu, _lote_de


def _a(nombre):
    return {'id': nombre, 'name': nombre, 'origen': 'deposito', 'fuente': 'payu'}


def test_el_lote_se_lee_del_nombre():
    assert _lote_de('L17__reporte.xls') == 'L17'
    assert _lote_de('reporte.xls') == ''
    # El nombre real puede traer guiones bajos: solo cuenta el primer separador.
    assert _lote_de('L17__mi__reporte.xls') == 'L17'


def test_dos_archivos_del_mismo_lote_se_emparejan():
    pares, sp, sm = _emparejar_payu([_a('L1__payu.xls')], [_a('L1__moneda.csv')])

    assert [(p['name'], m['name']) for p, m in pares] == [('L1__payu.xls', 'L1__moneda.csv')]
    assert (sp, sm) == ([], [])


def test_el_lote_manda_sobre_el_orden_de_llegada():
    """Es el bug que este cambio corrige: subidos al revés, por orden se
    armarían dos pares equivocados."""
    payu   = [_a('L1__payu.xls'), _a('L2__payu.xls')]
    moneda = [_a('L2__moneda.csv'), _a('L1__moneda.csv')]

    pares, _, _ = _emparejar_payu(payu, moneda)

    assert sorted((p['name'], m['name']) for p, m in pares) == [
        ('L1__payu.xls', 'L1__moneda.csv'),
        ('L2__payu.xls', 'L2__moneda.csv'),
    ]


def test_un_archivo_con_lote_espera_a_su_pareja_en_vez_de_agarrar_otra():
    """Si su Moneda no llegó, NO se empareja con la que haya: agarrar otra es
    exactamente el error que se está corrigiendo."""
    pares, sp, sm = _emparejar_payu([_a('L1__payu.xls')], [_a('moneda_suelta.csv')])

    assert pares == []
    assert [f['name'] for f in sp] == ['L1__payu.xls']
    assert [f['name'] for f in sm] == ['moneda_suelta.csv']


def test_los_de_drive_se_siguen_emparejando_por_orden():
    """Mientras Drive viva van a seguir llegando pares sin lote."""
    pares, sp, sm = _emparejar_payu([_a('payu.xls')], [_a('moneda.csv')])

    assert [(p['name'], m['name']) for p, m in pares] == [('payu.xls', 'moneda.csv')]
    assert (sp, sm) == ([], [])


def test_conviven_un_par_con_lote_y_uno_sin_lote():
    payu   = [_a('payu_viejo.xls'), _a('L9__payu.xls')]
    moneda = [_a('L9__moneda.csv'), _a('moneda_viejo.csv')]

    pares, sp, sm = _emparejar_payu(payu, moneda)

    assert sorted((p['name'], m['name']) for p, m in pares) == [
        ('L9__payu.xls', 'L9__moneda.csv'),
        ('payu_viejo.xls', 'moneda_viejo.csv'),
    ]
    assert (sp, sm) == ([], [])


def test_un_payu_sin_moneda_queda_de_sobrante():
    pares, sp, sm = _emparejar_payu([_a('payu.xls'), _a('otro.xls')], [_a('moneda.csv')])

    assert len(pares) == 1
    assert [f['name'] for f in sp] == ['otro.xls']
    assert sm == []
