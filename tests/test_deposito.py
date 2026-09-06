"""El depósito: hablar con el almacenamiento de Supabase.

Lo que se prueba acá son las trampas del formato, no la lógica de negocio: qué
cuenta como archivo, qué pasa cuando la carpeta trae más de una página, y qué
hace cuando el almacenamiento falla. Las tres tienen la misma forma de error —
se procesa de menos y nadie se entera.
"""

import io

import pytest

from utils import deposito, dry_run


class _Resp:
    def __init__(self, datos=None, contenido=b'', status=200):
        self._datos = datos if datos is not None else []
        self.content = contenido
        self.status_code = status

    def json(self):
        return self._datos

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f'HTTP {self.status_code}')


@pytest.fixture(autouse=True)
def entorno(monkeypatch):
    monkeypatch.setenv('DEPOSITO_BUCKET', 'archivos-pipeline')
    monkeypatch.setenv('SUPABASE_URL', 'https://falso.supabase.co')
    monkeypatch.setenv('SUPABASE_SERVICE_ROLE_KEY', 'k')
    dry_run.desactivar()


def _objeto(nombre, con_id=True):
    return {'name': nombre, 'id': 'uuid' if con_id else None}


def test_sin_bucket_el_deposito_esta_apagado(monkeypatch):
    """Es lo que permite desplegar el código antes de crear el bucket."""
    monkeypatch.setenv('DEPOSITO_BUCKET', '')
    assert deposito.activo() is False
    assert deposito.listar('wompi') == []


def test_lista_los_archivos_de_su_fuente(monkeypatch):
    pedidos = []

    def _post(url, **kw):
        pedidos.append((url, kw['json']))
        return _Resp([_objeto('extracto.pdf'), _objeto('otro.pdf')])

    monkeypatch.setattr(deposito.http, 'post', _post)

    archivos = deposito.listar('bc2576')

    assert [a['name'] for a in archivos] == ['extracto.pdf', 'otro.pdf']
    assert [a['id'] for a in archivos] == ['entrada/bc2576/extracto.pdf',
                                           'entrada/bc2576/otro.pdf']
    assert pedidos[0][1]['prefix'] == 'entrada/bc2576/'
    # Del más viejo al más nuevo: los 3 archivos de referencia toman el último,
    # y en PayU el orden decide qué se empareja con cuál.
    assert pedidos[0][1]['sortBy'] == {'column': 'created_at', 'order': 'asc'}


def test_las_carpetas_y_el_marcador_de_vacio_no_son_archivos(monkeypatch):
    """Supabase deja un objeto `.emptyFolderPlaceholder` para que una carpeta
    exista sin archivos, y las subcarpetas vienen sin `id`. Ninguno de los dos
    se puede intentar procesar como si fuera un extracto."""
    monkeypatch.setattr(deposito.http, 'post',
                        lambda *_a, **_k: _Resp([
                            _objeto('.emptyFolderPlaceholder'),
                            _objeto('subcarpeta', con_id=False),
                            _objeto('bueno.pdf'),
                        ]))

    assert [a['name'] for a in deposito.listar('wompi')] == ['bueno.pdf']


def test_una_carpeta_con_mas_de_una_pagina_se_lee_entera(monkeypatch):
    """Sin paginar, los que se pierden son los ÚLTIMOS — o sea los más nuevos,
    justo los del día. Es el mismo bug que ya había en el listado de Drive."""
    paginas = [
        [_objeto(f'a{i}.pdf') for i in range(100)],
        [_objeto('ultimo.pdf')],
    ]
    llamadas = []

    def _post(_url, **kw):
        llamadas.append(kw['json']['offset'])
        return _Resp(paginas[len(llamadas) - 1])

    monkeypatch.setattr(deposito.http, 'post', _post)

    archivos = deposito.listar('stripe')

    assert len(archivos) == 101
    assert archivos[-1]['name'] == 'ultimo.pdf'
    assert llamadas == [0, 100]


def test_si_el_almacenamiento_falla_no_se_cae_la_corrida_pero_se_grita(monkeypatch, caplog):
    """Cortar la corrida dejaría fuera también lo que entra por Drive. Pero
    'vacío' acá significa que lo que subió el área no se procesa, así que tiene
    que quedar en ERROR: es la forma exacta del atasco de Stripe."""
    def _explota(*_a, **_k):
        raise RuntimeError('almacenamiento caído')

    monkeypatch.setattr(deposito.http, 'post', _explota)

    with caplog.at_level('ERROR'):
        assert deposito.listar('wompi') == []

    assert any(r.levelname == 'ERROR' for r in caplog.records)


def test_descargar_pide_el_objeto_por_su_ruta(monkeypatch):
    urls = []

    def _get(url, **_kw):
        urls.append(url)
        return _Resp(contenido=b'%PDF')

    monkeypatch.setattr(deposito.http, 'get', _get)

    assert deposito.descargar('entrada/wompi/x.csv').read() == b'%PDF'
    assert urls == ['https://falso.supabase.co/storage/v1/object/archivos-pipeline/entrada/wompi/x.csv']


def test_archivar_manda_el_objeto_a_historico(monkeypatch):
    cuerpos = []

    def _post(_url, **kw):
        cuerpos.append(kw['json'])
        return _Resp()

    monkeypatch.setattr(deposito.http, 'post', _post)

    deposito.mover_a_historico('entrada/wompi/x.csv', 'wompi', 'x.csv')

    assert cuerpos == [{'bucketId': 'archivos-pipeline',
                        'sourceKey': 'entrada/wompi/x.csv',
                        'destinationKey': 'historico/wompi/x.csv'}]


def test_en_simulacion_no_se_archiva_nada(monkeypatch, tmp_path):
    """Mover un archivo al histórico es de las escrituras que más duelen: si la
    corrida no lo procesó bien, moverlo lo esconde."""
    def _explota(*_a, **_k):
        raise AssertionError('en dry-run no se debe tocar el almacenamiento')

    monkeypatch.setattr(deposito.http, 'post', _explota)
    dry_run.activar('prueba', str(tmp_path / 'salida.jsonl'))
    try:
        deposito.mover_a_historico('entrada/wompi/x.csv', 'wompi', 'x.csv')
    finally:
        dry_run.desactivar()


def test_descargar_sin_bucket_falla_a_gritos(monkeypatch):
    """A diferencia del listado, acá no hay degradación posible: si no se puede
    leer el archivo no hay nada que procesar."""
    monkeypatch.setenv('DEPOSITO_BUCKET', '')
    with pytest.raises(RuntimeError):
        deposito.descargar('entrada/wompi/x.csv')


def test_el_contenido_vuelve_como_algo_leible(monkeypatch):
    """Los parsers reciben un archivo, no bytes sueltos: tiene que comportarse
    igual que lo que devuelve Drive."""
    monkeypatch.setattr(deposito.http, 'get', lambda *_a, **_k: _Resp(contenido=b'abc'))
    assert isinstance(deposito.descargar('entrada/wompi/x.csv'), io.BytesIO)


# ── La caducidad a 3 meses ───────────────────────────────────────────────────

def _obj_con_fecha(nombre, dias_atras):
    from datetime import datetime, timedelta, timezone
    creado = (datetime.now(timezone.utc) - timedelta(days=dias_atras)).isoformat()
    return {'name': nombre, 'id': 'uuid', 'created_at': creado}


def test_se_borra_lo_viejo_y_se_conserva_lo_reciente(monkeypatch):
    borrados = []

    monkeypatch.setattr(deposito.http, 'post',
                        lambda *_a, **_k: _Resp([_obj_con_fecha('viejo.pdf', 120),
                                                 _obj_con_fecha('nuevo.pdf', 10)]))
    monkeypatch.setattr(deposito.http, 'delete',
                        lambda _u, **kw: borrados.extend(kw['json']['prefixes']) or _Resp())

    assert deposito.caducar(['wompi'], dias=90) == 1
    assert borrados == ['historico/wompi/viejo.pdf']


def test_un_archivo_sin_fecha_no_se_borra(monkeypatch):
    """Ante la duda se conserva: perder un archivo es irreversible, que sobre
    uno solo ocupa lugar."""
    monkeypatch.setattr(deposito.http, 'post',
                        lambda *_a, **_k: _Resp([{'name': 'x.pdf', 'id': 'u'}]))
    monkeypatch.setattr(deposito.http, 'delete',
                        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError('no debía borrar')))

    assert deposito.caducar(['wompi'], dias=90) == 0


def test_en_simulacion_no_se_borra_nada(monkeypatch, tmp_path):
    monkeypatch.setattr(deposito.http, 'post',
                        lambda *_a, **_k: _Resp([_obj_con_fecha('viejo.pdf', 200)]))
    monkeypatch.setattr(deposito.http, 'delete',
                        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError('no debía borrar')))

    dry_run.activar('prueba', str(tmp_path / 'salida.jsonl'))
    try:
        assert deposito.caducar(['wompi'], dias=90) == 0
    finally:
        dry_run.desactivar()


def test_la_caducidad_solo_mira_el_historico(monkeypatch):
    """Nunca la entrada: ahí está lo que el área acaba de subir y todavía no se
    procesa."""
    prefijos = []
    monkeypatch.setattr(deposito.http, 'post',
                        lambda _u, **kw: prefijos.append(kw['json']['prefix']) or _Resp([]))

    deposito.caducar(['wompi', 'stripe'], dias=90)

    assert prefijos == ['historico/wompi/', 'historico/stripe/']
