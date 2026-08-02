"""
La escritura en lote de `cruce_cartera`, y su respaldo.

Era el mayor cuello de botella del pipeline: un PATCH por fila, 1.080 filas,
~433 ms cada una — casi 8 minutos en un solo bucle. En lote es una petición.

Lo que se prueba acá no es la velocidad (eso se mide, no se afirma en un test)
sino las tres cosas que pueden salir mal al cambiarlo:

1. Que siga siendo un UPDATE y nunca un upsert. Un insert parcial sobre esta
   tabla revienta con 23502 y así se cayó el motor un día entero el 24/07.
2. Que si la función de base no existe, el código no se rompa: siga andando por
   el camino viejo. Sin eso, desplegar antes de correr la migración deja el
   pipeline muerto.
3. Que una llave repetida en el mismo lote termine igual que antes.
"""

import pytest

from utils import dry_run, supabase


class _Respuesta:
    def __init__(self, status=200, text='', url='https://x'):
        self.status_code = status
        self.text = text
        self.url = url
        self.request = None

    @property
    def ok(self):
        return self.status_code < 400

    def raise_for_status(self):
        if not self.ok:
            raise AssertionError(f'HTTP {self.status_code}: {self.text}')


@pytest.fixture(autouse=True)
def estado_limpio():
    """El módulo recuerda si la función de lote existe; cada test arranca sin
    esa memoria para no depender del orden en que corran."""
    supabase._LOTE_DISPONIBLE.clear()
    yield
    supabase._LOTE_DISPONIBLE.clear()
    dry_run.desactivar()


@pytest.fixture
def espia(monkeypatch):
    """Registra cada petición que se intenta, sin hacer ninguna."""
    llamadas = {'post': [], 'patch': []}

    def _post(url, json=None, **_kw):
        llamadas['post'].append((url, json))
        return _Respuesta()

    def _patch(url, params=None, json=None, **_kw):
        llamadas['patch'].append((url, params, json))
        return _Respuesta()

    monkeypatch.setattr(supabase.http, 'post', _post)
    monkeypatch.setattr(supabase.http, 'patch', _patch)
    return llamadas


def test_manda_todo_en_una_sola_peticion(espia):
    filas = [{'matching_key': f'PAGO-{i}', 'cruce': f'Cliente {i}'} for i in range(500)]

    supabase.update_cruce_valores('https://x', 'k', filas)

    assert len(espia['post']) == 1, 'debió ser una sola petición'
    assert not espia['patch'], 'no debió quedar ninguna escritura fila por fila'

    url, cuerpo = espia['post'][0]
    assert url.endswith('/rpc/actualizar_cruce_valores')
    assert len(cuerpo['cambios']) == 500


def test_nunca_usa_upsert(espia):
    """Un POST a la tabla (en vez de a la función) sería un insert parcial, y
    eso es el crash 23502 del 24 de julio."""
    supabase.update_cruce_valores('https://x', 'k', [{'matching_key': 'A', 'cruce': 'x'}])

    for url, _ in espia['post']:
        assert '/rpc/' in url, f'se escribió directo a la tabla: {url}'
        assert 'on_conflict' not in url


def test_si_la_funcion_no_existe_sigue_por_el_camino_viejo(monkeypatch):
    """Desplegar el código antes de correr la migración no puede romper nada.

    El acoplamiento inverso —código nuevo que exige SQL ya corrido— es lo que
    dejó el pipeline sin escribir durante un día entero el 24 de julio.
    """
    patches = []

    def _post_sin_funcion(url, json=None, **_kw):
        return _Respuesta(
            404, '{"code":"PGRST202","message":"Could not find the function '
                 'public.actualizar_cruce_valores"}')

    def _patch(url, params=None, json=None, **_kw):
        patches.append((params, json))
        return _Respuesta()

    monkeypatch.setattr(supabase.http, 'post', _post_sin_funcion)
    monkeypatch.setattr(supabase.http, 'patch', _patch)

    filas = [{'matching_key': 'A', 'cruce': 'x'}, {'matching_key': 'B', 'cruce': 'y'}]
    supabase.update_cruce_valores('https://x', 'k', filas)

    assert len(patches) == 2, 'debió caer al camino fila por fila'
    assert patches[0][0] == {'matching_key': 'eq.A'}
    assert patches[0][1] == {'cruce': 'x'}, 'la llave no viaja en el cuerpo del PATCH'


def test_no_reintenta_la_funcion_en_cada_llamada(monkeypatch):
    """Una vez que se sabe que no está, no se vuelve a preguntar: sería una
    petición perdida por cada lote."""
    intentos = {'post': 0}

    def _post(url, json=None, **_kw):
        intentos['post'] += 1
        return _Respuesta(404, 'actualizar_cruce_valores no existe')

    monkeypatch.setattr(supabase.http, 'post', _post)
    monkeypatch.setattr(supabase.http, 'patch',
                        lambda *a, **k: _Respuesta())

    for _ in range(3):
        supabase.update_cruce_valores('https://x', 'k', [{'matching_key': 'A', 'cruce': 'x'}])

    assert intentos['post'] == 1, 'volvió a probar la función que ya sabía que no existe'


def test_llave_repetida_gana_la_ultima(espia):
    """El bucle de PATCH aplicaba las dos en orden, así que mandaba la última.
    En lote, Postgres elegiría una sin criterio si no se resolviera antes."""
    supabase.update_cruce_valores('https://x', 'k', [
        {'matching_key': 'A', 'cruce': 'primero'},
        {'matching_key': 'A', 'cruce': 'ultimo'},
    ])

    cambios = espia['post'][0][1]['cambios']
    assert len(cambios) == 1
    assert cambios[0]['cruce'] == 'ultimo'


def test_columnas_heterogeneas_viajan_tal_cual(espia):
    """La fase 9.4 manda `incp` solo cuando lo resolvió.

    Por eso la función de base tiene que mirar qué claves trae cada objeto: si
    rellenara las ausentes con NULL, borraría el INCP de las filas donde no
    venía.
    """
    supabase.update_cruce_valores('https://x', 'k', [
        {'matching_key': 'A', 'nombre': 'Persona', 'incp': '4321PN'},
        {'matching_key': 'B', 'nombre': 'Otra'},
    ])

    cambios = espia['post'][0][1]['cambios']
    assert 'incp' in cambios[0]
    assert 'incp' not in cambios[1], (
        'se agregó una clave que no venía: la función la interpretaría como '
        '"poner en NULL" y borraría el INCP existente'
    )


def test_lista_vacia_no_hace_nada(espia):
    supabase.update_cruce_valores('https://x', 'k', [])
    assert not espia['post'] and not espia['patch']


def test_en_simulacion_no_escribe(espia, tmp_path):
    dry_run.activar('test', str(tmp_path / 's.jsonl'))
    supabase.update_cruce_valores('https://x', 'k', [{'matching_key': 'A', 'cruce': 'x'}])
    assert not espia['post'] and not espia['patch']


# ── consolidated_transactions: el mismo mecanismo, la otra tabla ──────────────

def test_consolidated_tambien_va_en_lote(espia):
    """825 de las 1.212 filas del consolidado son WOMPI, y esta función las
    reescribía una por una: ~3,4 minutos por corrida."""
    filas = [{'matching_key': f'W-{i}', 'metodo_de_pago': 'WOMPI (Genera Link)'}
             for i in range(825)]

    supabase.update_consolidated_campos('https://x', 'k', filas)

    assert len(espia['post']) == 1
    assert espia['post'][0][0].endswith('/rpc/actualizar_consolidated_campos')
    assert not espia['patch']


def test_consolidated_ignora_objetos_sin_nada_que_escribir(espia):
    """Un objeto con solo la llave no tiene columnas que cambiar: mandarlo haría
    que la función pisara la fila sin motivo."""
    supabase.update_consolidated_campos('https://x', 'k', [
        {'matching_key': 'A'},
        {'matching_key': 'B', 'program': 'DIPLOMADO'},
    ])

    cambios = espia['post'][0][1]['cambios']
    assert [c['matching_key'] for c in cambios] == ['B']


def test_cada_tabla_recuerda_su_funcion_por_separado(monkeypatch):
    """Que falte la función de una tabla no puede degradar a la otra.

    Se guardan por nombre de función justamente para eso: si se llevara un solo
    booleano, un 404 en una dejaría a la otra escribiendo fila por fila para
    siempre, sin que nadie lo note.
    """
    def _post(url, json=None, **_kw):
        if 'actualizar_cruce_valores' in url:
            return _Respuesta(404, 'actualizar_cruce_valores no existe')
        return _Respuesta()

    monkeypatch.setattr(supabase.http, 'post', _post)
    monkeypatch.setattr(supabase.http, 'patch', lambda *a, **k: _Respuesta())

    supabase.update_cruce_valores('https://x', 'k', [{'matching_key': 'A', 'cruce': 'x'}])
    supabase.update_consolidated_campos('https://x', 'k', [{'matching_key': 'B', 'program': 'D'}])

    assert supabase._LOTE_DISPONIBLE['actualizar_cruce_valores'] is False
    assert supabase._LOTE_DISPONIBLE['actualizar_consolidated_campos'] is True
