"""
Modo puntual: procesar un solo pago sin leer el mundo entero.

La regla que lo justifica y la que lo puede romper son la misma: **recortar una
lectura no es neutral**. Los índices de ambigüedad se construyen sobre las filas
que se leyeron, así que un universo más chico puede hacer que el sistema decida
donde antes se abstenía — y asignar un pago a la inscripción equivocada en
silencio.

Por eso la prueba central de este archivo no comprueba que el modo puntual sea
rápido (eso se mide, no se afirma en un test) sino que **da exactamente el mismo
resultado que la corrida completa**. Es la única forma de saber que los filtros
son suficientes y no solo pequeños.
"""

import pytest

import cruzar


def _pago(matching_key, identification='', email='', metodo='BANCOLOMBIA', monto=500_000):
    return {
        'identification': identification, 'payment_date': '2026-08-01',
        'transaction_code_1': 'PAGO QR', 'transaction_code_2': '',
        'email': email, 'payment_method': metodo, 'program': '', 'phone': '',
        'payment_amount': monto, 'matching_key': matching_key,
        'registration_date': '2026-08-01', 'metodo_de_pago': None,
    }


@pytest.fixture
def mundo_filtrable(monkeypatch):
    """Como el fixture `mundo`, pero respetando los filtros de PostgREST.

    Es lo que hace la prueba honesta: si el fake ignorara los filtros, el modo
    puntual vería el mundo completo y la comparación pasaría siempre, sin haber
    probado nada.
    """
    import sys

    def correr(tablas: dict, solo: str | None = None) -> dict:
        argv = ['cruzar.py'] + (['--solo', solo] if solo else [])
        monkeypatch.setattr(sys, 'argv', argv)
        for clave, valor in {
            'SUPABASE_URL': 'https://falso.supabase.co',
            'SUPABASE_SERVICE_ROLE_KEY': 'k',
            'GOOGLE_SA_JSON': '', 'WOMPI_REPORTE_DRIVE_FOLDER_ID': '',
        }.items():
            monkeypatch.setenv(clave, valor)

        lecturas = []

        def _select(_url, _srk, tabla, select=None, filtros=None, **_kw):
            filas = [dict(f) for f in tablas.get(tabla, [])]
            lecturas.append((tabla, len(filas), filtros))
            for columna, condicion in (filtros or {}).items():
                if condicion.startswith('eq.'):
                    esperado = condicion[3:]
                    filas = [f for f in filas if str(f.get(columna, '')) == esperado]
                elif condicion.startswith('like.'):
                    prefijo = condicion[5:].rstrip('*')
                    filas = [f for f in filas if str(f.get(columna, '')).startswith(prefijo)]
            return filas

        monkeypatch.setattr(cruzar, 'select_all', _select)

        capturado: dict[str, list] = {}

        def _capturar(destino):
            def escribir(*args, **_kw):
                filas = args[-1] if args else []
                capturado.setdefault(destino, []).extend(
                    filas if isinstance(filas, list) else [filas])
            return escribir

        for nombre, destino in [('upsert_cruce', 'cruce_cartera'),
                                ('upsert_pagos_apartados', 'pagos_apartados'),
                                ('update_cruce_valores', 'cruce_update'),
                                ('update_consolidated_campos', 'consolidated_update')]:
            monkeypatch.setattr(cruzar, nombre, _capturar(destino))
        monkeypatch.setattr(cruzar, 'delete_by_keys', lambda *a, **k: None)

        cruzar.main()
        capturado['_lecturas'] = lecturas
        return capturado

    return correr


def _fila(capturado, matching_key):
    for f in capturado.get('cruce_cartera', []):
        if f.get('matching_key') == matching_key:
            return f
    return None


# ── El invariante ────────────────────────────────────────────────────────────

MUNDO = {
    'consolidated_transactions': [
        _pago('PAGO-A', identification='1002003004'),
        _pago('PAGO-B', identification='860004922'),
        _pago('PAGO-C', identification='9999999999'),
    ],
    'cartera_inscrip': [
        {'numero_id': '1002003004', 'id_inscripcion': '4321PN'},
        {'numero_id': '860004922-4', 'id_inscripcion': '430PJ'},
        {'numero_id': '5555555555', 'id_inscripcion': '7777PN'},
    ],
}


@pytest.mark.parametrize('llave', ['PAGO-A', 'PAGO-B', 'PAGO-C'])
def test_puntual_da_lo_mismo_que_completo(mundo_filtrable, llave):
    """El invariante que hace confiable todo el modo puntual."""
    completo = _fila(mundo_filtrable(MUNDO), llave)
    puntual = _fila(mundo_filtrable(MUNDO, solo=llave), llave)

    assert puntual is not None, 'el modo puntual no produjo la fila'
    assert puntual == completo, (
        'el modo puntual decidió distinto que la corrida completa.\n'
        f'  completo: {completo}\n  puntual : {puntual}'
    )


def test_el_nit_con_digito_de_verificacion_sobrevive_al_filtro(mundo_filtrable):
    """La cartera guarda `860004922-4` y el banco manda `860004922`.

    Por eso el filtro usa `like.<doc>*` y no `eq.`: con `eq.` esa fila no
    entraría y el pago de cualquier empresa dejaría de cruzar solo en modo
    puntual — el peor tipo de bug, el que aparece únicamente por un camino.
    """
    fila = _fila(mundo_filtrable(MUNDO, solo='PAGO-B'), 'PAGO-B')
    assert fila['incp'] == '430PJ'


def test_solo_toca_el_pago_pedido(mundo_filtrable):
    capturado = mundo_filtrable(MUNDO, solo='PAGO-A')
    llaves = {f['matching_key'] for f in capturado.get('cruce_cartera', [])}
    assert llaves == {'PAGO-A'}, f'tocó filas que nadie pidió: {llaves - {"PAGO-A"}}'


def test_lee_menos_de_cada_tabla(mundo_filtrable):
    """Es el punto del modo puntual: 22 de los 28 s de un reproceso son leer
    tablas enteras para tocar una fila."""
    completo = mundo_filtrable(MUNDO)['_lecturas']
    puntual = mundo_filtrable(MUNDO, solo='PAGO-A')['_lecturas']

    def con_filtro(lecturas, tabla):
        return any(t == tabla and f for t, _, f in lecturas)

    assert not con_filtro(completo, 'cartera_inscrip'), 'la corrida completa no debe filtrar'
    for tabla in ('cartera_inscrip', 'consolidated_transactions', 'cartera_preventiva'):
        assert con_filtro(puntual, tabla), f'{tabla} se leyó entera en modo puntual'


def test_un_pago_que_no_existe_falla_claro(mundo_filtrable):
    with pytest.raises(SystemExit) as exc:
        mundo_filtrable(MUNDO, solo='NO-EXISTE')
    assert exc.value.code == 1


# ── La trampa que motivó el diseño ───────────────────────────────────────────

def test_la_ambiguedad_de_inscripcion_no_se_pierde_al_filtrar(monkeypatch):
    """Dos inscripciones con la misma base son ambigüedad real: no se resuelve.

    Si el modo puntual leyera `cartera_inscrip` filtrado por documento, esta
    base aparecería con un solo candidato y el pago se asignaría a una
    inscripción equivocada. `_IdInscripcionPorBaseLazy` consulta por la base, no
    por el documento, justo para que eso no pase.
    """
    consultas = []

    def _select(_url, _srk, tabla, select=None, filtros=None, **_kw):
        consultas.append(filtros)
        return [{'id_inscripcion': '3300PN'}, {'id_inscripcion': '3300PJ'}]

    monkeypatch.setattr(cruzar, 'select_all', _select)
    indice = cruzar._IdInscripcionPorBaseLazy('https://x', 'k')

    assert cruzar._resolver_incp_wompi_link('3300', indice) is None, (
        'resolvió una inscripción ambigua: dos candidatos reales para la misma base'
    )
    assert consultas[0] == {'id_inscripcion': 'like.3300*'}, (
        'consultó por documento en vez de por base'
    )


def test_la_base_unica_si_se_resuelve(monkeypatch):
    monkeypatch.setattr(cruzar, 'select_all',
                        lambda *a, **k: [{'id_inscripcion': '3077PN'}])
    indice = cruzar._IdInscripcionPorBaseLazy('https://x', 'k')
    assert cruzar._resolver_incp_wompi_link('3077', indice) == '3077PN'


def test_la_trampa_dentro_de_main(mundo_filtrable, monkeypatch):
    """El escenario completo donde el filtro por documento decidiría mal.

    Esta prueba existe porque la primera versión del invariante **no cazaba
    este caso**: como en los tests no hay reporte de WOMPI, la resolución del
    INCP por link nunca llegaba a ejecutarse dentro de `main()`, y revertir el
    índice a las filas filtradas seguía pasando todo en verde.

    El montaje: dos inscripciones comparten la base `3300` y pertenecen a
    documentos distintos. La corrida completa ve las dos, reconoce la
    ambigüedad y **se abstiene**. Si el modo puntual leyera `cartera_inscrip`
    filtrado por el documento del pago, vería una sola y resolvería — metiendo
    la plata en una inscripción que nadie confirmó.
    """
    mundo = {
        'consolidated_transactions': [
            _pago('PAGO-W', identification='1002003004', metodo='WOMPI PSE'),
        ],
        'cartera_inscrip': [
            {'numero_id': '1002003004', 'id_inscripcion': '3300PN'},
            {'numero_id': '7777777777', 'id_inscripcion': '3300PJ'},
        ],
    }

    # El reporte dice que ese pago corresponde a la inscripción "3300", sin
    # sufijo — que es como lo manda el Sistema Financiero.
    monkeypatch.setattr(cruzar, '_cargar_lookup_wompi_reporte', lambda *_a, **_k: (
        {'PAGO-W': {'id_transaccion': 'PAGO-W', 'pagador': 'ALGUIEN',
                    'comprobante': 'CI-1', 'inscripcion': '3300',
                    'proyecto': 'DIPLOMADO', 'fecha_pago': '2026-08-01'}},
        True, [],
    ))

    completo = _fila(mundo_filtrable(mundo), 'PAGO-W')
    puntual = _fila(mundo_filtrable(mundo, solo='PAGO-W'), 'PAGO-W')

    assert puntual == completo, (
        'el modo puntual resolvió una inscripción ambigua que la corrida '
        f'completa rechaza.\n  completo: {completo}\n  puntual : {puntual}'
    )


def test_no_confunde_bases_parecidas(monkeypatch):
    """`like.330*` trae también `3300`. Si esas colaran, una base con un solo
    candidato parecería ambigua y se dejaría de resolver."""
    monkeypatch.setattr(
        cruzar, 'select_all',
        lambda *a, **k: [{'id_inscripcion': '330PN'}, {'id_inscripcion': '3300PN'}])
    indice = cruzar._IdInscripcionPorBaseLazy('https://x', 'k')
    assert cruzar._resolver_incp_wompi_link('330', indice) == '330PN'
