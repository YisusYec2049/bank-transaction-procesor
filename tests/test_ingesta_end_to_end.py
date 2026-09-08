"""
La ingesta completa: de un archivo del banco a las filas del consolidado.

`procesar_todos.py` es el primer paso de la cadena y **el único sin prueba de
punta a punta** hasta ahora. Si se rompe, no entra ningún pago y todo lo que
viene después trabaja sobre datos viejos sin quejarse.

Se corre el `main()` real con un archivo de banco anonimizado en la bandeja,
sustituyendo únicamente Drive, Sheets y Supabase.
"""

import io
import json
from datetime import datetime
from pathlib import Path

import pytest
import pytz

import procesar_todos
from utils import dry_run, supabase

FIXTURES = Path(__file__).parent / 'fixtures'


class _PaginaFalsa:
    def __init__(self, datos):
        self._d = datos

    def extract_text(self):
        return self._d['text']

    def extract_words(self, **_kw):
        return [dict(w) for w in self._d['words']]


class _PdfFalso:
    def __init__(self, paginas):
        self.pages = [_PaginaFalsa(p) for p in paginas]

    def __enter__(self):
        return self

    def __exit__(self, *_e):
        return False


@pytest.fixture
def bandeja(monkeypatch):
    """Deja un archivo de banco en la bandeja de Drive y corre la ingesta.

    Devuelve lo que se habría escrito, por destino.
    """
    def correr(banco: str, fixture_pdf: str, ya_en_supabase=(), argv_extra=()):
        import sys

        monkeypatch.setattr(sys, 'argv', ['procesar_todos.py', '--bank', banco, *argv_extra])
        for clave, valor in {
            'SUPABASE_URL': 'https://falso.supabase.co',
            'SUPABASE_SERVICE_ROLE_KEY': 'k',
            # Sin esto el depósito está apagado y `procesar_todos.py` saltea
            # TODOS los bancos: desde que Drive se desconectó (2026-09-08),
            # "no hay de dónde leer" es exactamente eso.
            'DEPOSITO_BUCKET': 'archivos-pipeline',
        }.items():
            monkeypatch.setenv(clave, valor)

        # Nada de leer el .env real: una prueba que depende del entorno de quien
        # la corre no prueba lo mismo en dos máquinas.
        monkeypatch.setattr(procesar_todos, 'load_dotenv', lambda *_a, **_k: None)

        # El script pide archivos por BANDEJA y no sabe de dónde salen: se
        # simula la capa `utils/origen`, no Drive. El `origen` que viaja en cada
        # archivo es lo que decide a quién le pide el contenido y dónde lo
        # archiva, así que tiene que estar puesto igual que en producción.
        monkeypatch.setattr(procesar_todos, 'listar',
                            lambda _b: [{'id': 'f1', 'name': 'extracto.pdf',
                                         'origen': 'deposito', 'fuente': banco}])
        monkeypatch.setattr(procesar_todos, 'descargar',
                            lambda _a: io.BytesIO(b'%PDF'))

        paginas = json.loads((FIXTURES / fixture_pdf).read_text(encoding='utf-8'))
        modulo = procesar_todos.BANCOS_BANCOLOMBIA[banco]['mod']
        monkeypatch.setattr(modulo.pdfplumber, 'open', lambda *_a, **_k: _PdfFalso(paginas))

        escrito: dict[str, list] = {}
        movidos: list[str] = []
        anotados: list[dict] = []

        # El registro habla con Supabase, y estas pruebas corren sin red ni
        # credenciales. Se captura en vez de dejarlo fallar: sin esto cada
        # archivo procesado intenta una petición HTTP que muere por DNS, y el
        # error queda tapado por la guarda de `registro.anotar`.
        monkeypatch.setattr(procesar_todos.registro, 'anotar',
                            lambda archivo, **kw: anotados.append({'nombre': archivo['name'], **kw}))

        monkeypatch.setattr(procesar_todos, 'upsert',
                            lambda _u, _k, filas: escrito.setdefault('consolidado', []).extend(filas))
        monkeypatch.setattr(procesar_todos, 'upsert_pagos_apartados',
                            lambda _u, _k, filas: escrito.setdefault('apartados', []).extend(filas))
        monkeypatch.setattr(procesar_todos, 'keys_del_dia_anterior',
                            lambda *_a, **_k: set(ya_en_supabase))
        monkeypatch.setattr(procesar_todos, 'existing_matching_keys',
                            lambda *_a, **_k: set())
        monkeypatch.setattr(procesar_todos, 'select_all', lambda *_a, **_k: [])
        # La caducidad del histórico corre al final de cada corrida y habla con
        # el almacenamiento. Con el depósito encendido —que ahora es siempre—
        # esta prueba se iría a la red y colgaría 30 s por fuente.
        monkeypatch.setattr(procesar_todos.deposito, 'caducar', lambda *_a, **_k: 0)
        # El de verdad NO mueve nada en simulación: el freno vive en la puerta
        # (`utils/deposito.py`), no en este script. El doble tiene que hacer lo
        # mismo, o una prueba de dry-run mediría el doble y no el código.
        monkeypatch.setattr(procesar_todos, 'mover_a_historico',
                            lambda archivo, _b: True if dry_run.activo()
                            else (movidos.append(archivo['id']), True)[1])

        procesar_todos.main()
        escrito['_movidos'] = movidos
        escrito['_anotados'] = anotados
        return escrito

    return correr


def test_un_extracto_entra_al_consolidado(bandeja):
    escrito = bandeja('bc2576', 'bc2576_extracto.json')

    filas = escrito.get('consolidado', [])
    assert filas, 'no entró ningún pago al consolidado'
    assert all(len(f) == 11 for f in filas), 'alguna fila no trae las 11 columnas'
    assert all(f[10] for f in filas), 'hay filas sin matching_key'


def test_el_archivo_procesado_se_mueve_a_historico(bandeja):
    escrito = bandeja('bc2576', 'bc2576_extracto.json')
    assert escrito['_movidos'] == ['f1'], 'el archivo no se archivó tras procesarlo'


def test_en_simulacion_no_se_mueve_el_archivo(bandeja):
    """Si la corrida no escribió, el archivo tiene que quedarse en la bandeja.

    Moverlo lo esconde: es el bug de julio en el que archivos sin filas válidas
    desaparecían en Histórico sin dejar rastro en ninguna tabla.
    """
    escrito = bandeja('bc2576', 'bc2576_extracto.json', argv_extra=('--dry-run',))
    assert escrito['_movidos'] == []
    assert not escrito.get('consolidado')


def test_lo_que_ya_entro_ayer_no_se_repite(bandeja):
    """La dedup: un pago que ya está registrado no se vuelve a escribir."""
    completo = bandeja('bc2576', 'bc2576_extracto.json')
    llaves = [f[10] for f in completo['consolidado']]

    parcial = bandeja('bc2576', 'bc2576_extracto.json', ya_en_supabase=llaves[:2])
    quedaron = {f[10] for f in parcial.get('consolidado', [])}

    assert llaves[0] not in quedaron and llaves[1] not in quedaron
    assert len(quedaron) == len(llaves) - 2


def test_los_cheques_no_llegan_al_consolidado(bandeja):
    """Van a pagos apartados: el área financiera no los maneja."""
    escrito = bandeja('bc2576', 'bc2576_extracto.json')
    for fila in escrito.get('consolidado', []):
        assert 'CHEQUE' not in str(fila[3]).upper()


class _Resp:
    def __init__(self, ok=True, texto='', datos=None):
        self.status_code = 200 if ok else 400
        self.text = texto
        self.url = 'https://x'
        self.request = None
        self._datos = datos if datos is not None else []

    def json(self):
        return self._datos

    @property
    def ok(self):
        return self.status_code < 400

    def raise_for_status(self):
        if not self.ok:
            raise AssertionError(f'HTTP {self.status_code}: {self.text}')


@pytest.fixture
def escribir_consolidado(monkeypatch):
    """Corre el `upsert` real y devuelve lo que le habría mandado a Supabase.

    `con_columna=False` simula una base donde el ALTER TABLE de
    `payment_time` todavía no se corrió.

    `ya_en_base` simula pagos que YA están en el consolidado: un diccionario
    `matching_key -> {'registration_date': ..., 'identification': ...}` con lo
    que la base tiene guardado hoy. Es el grupo que el export acumulado de
    Stripe vuelve a traer todos los días.
    """
    def correr(filas, con_columna=True, ya_en_base=None):
        # El test de simulación de este mismo archivo deja el modo dry-run
        # prendido (es global), y con él `upsert` no escribe nada.
        dry_run.desactivar()
        supabase._PAYMENT_TIME_DISPONIBLE = None
        guardados = ya_en_base or {}
        enviados: list[dict] = []

        def _get(_url, params=None, **_kw):
            params = params or {}
            # La consulta de "¿cuáles de estos pagos ya existen?" se responde con
            # la base simulada, y SOLO con las columnas que pidió — así la prueba
            # mide de verdad qué se consulta, no una respuesta armada a mano.
            if 'matching_key' in params:
                pedidas = str(params.get('select', '')).split(',')
                filas = [{c: {'matching_key': k, **v}.get(c) for c in pedidas}
                         for k, v in guardados.items()]
                return _Resp(datos=filas)
            return _Resp(con_columna,
                         '' if con_columna else
                         'column consolidated_transactions.payment_time does not exist')

        def _post(_url, json=None, **_kw):
            enviados.extend(json or [])
            return _Resp()

        monkeypatch.setattr(supabase.http, 'get', _get)
        monkeypatch.setattr(supabase.http, 'post', _post)

        supabase.upsert('https://x', 'k', filas)
        return enviados

    yield correr
    supabase._PAYMENT_TIME_DISPONIBLE = None


def _fila_wompi(hora='21:45:48'):
    fila = ['190093-1', '62694707', '21-07-2026', '7l5Lun_1', '500534673',
            'quien.paga@example.com', 'WOMPI PSE', 'CUOTA 1 DE 4',
            'JULIAN TORRES', 503125.0, '190093-1']
    return fila + [hora] if hora is not None else fila


def test_la_hora_de_wompi_llega_al_consolidado(escribir_consolidado):
    enviados = escribir_consolidado([_fila_wompi()])
    assert enviados[0]['payment_time'] == '21:45:48'
    assert enviados[0]['payment_date'] == '2026-07-21', 'el día no debe cambiar'


def test_un_banco_sin_hora_no_rompe_el_lote(escribir_consolidado):
    """Todas las filas del lote llevan la misma clave, aunque solo una tenga hora.

    PostgREST rechaza el array entero si los objetos no comparten el set de
    claves (PGRST102) — es la razón por la que los pagos nuevos y los que ya
    existen van en dos POST separados. Un extracto de Bancolombia (11 columnas,
    el PDF no reporta hora) llegando junto a uno de WOMPI tiene que entrar.
    """
    enviados = escribir_consolidado([_fila_wompi(), _fila_wompi(hora=None)])

    assert len(enviados) == 2
    assert all('payment_time' in fila for fila in enviados)
    assert [f['payment_time'] for f in enviados] == ['21:45:48', None]


def test_sin_la_columna_la_ingesta_entra_igual(escribir_consolidado):
    """Desplegar antes de correr el SQL no puede costar los pagos del día.

    Un POST con una columna inexistente se rechaza ENTERO, así que la hora se
    omite del payload mientras la columna no exista. Es la lección del 24 de
    julio, cuando código nuevo que exigía esquema nuevo dejó el motor caído un
    día entero.
    """
    enviados = escribir_consolidado([_fila_wompi()], con_columna=False)

    assert len(enviados) == 1, 'el pago tiene que entrar igual'
    assert 'payment_time' not in enviados[0]
    assert enviados[0]['matching_key'] == '190093-1'


# ---------------------------------------------------------------------------
# Los pagos que YA existen (el atasco de Stripe, 2026-09-02)
#
# El export de Stripe es acumulado: cada archivo trae varios días de pagos, así
# que en cada corrida una parte del lote YA está en el consolidado. Ese grupo se
# mandaba SIN `registration_date` —para no re-sellarle la fecha de ingreso— y
# PostgreSQL valida el NOT NULL antes de resolver el ON CONFLICT, así que
# rechazaba la tanda entera (23502). Efecto: el archivo nunca se archiva, el
# vigilante lo ve como trabajo nuevo y dispara la cadena cada 15 minutos.
# ---------------------------------------------------------------------------

def _fila_stripe(matching_key, identification):
    """Un pago del export de Stripe. El documento llega VACÍO muy seguido."""
    return ['x', identification, '11-08-2026', 'ch_1', '',
            'quien.paga@example.com', 'STRIPE_USA', 'Diplomado',
            '', 169.0, matching_key]


def test_un_pago_que_ya_existe_conserva_su_fecha_de_ingreso(escribir_consolidado):
    """La fecha de ingreso no se re-sella, pero tampoco se omite.

    Omitirla es lo que rompe: la columna es NOT NULL y PostgreSQL la valida
    antes de mirar el ON CONFLICT. Se manda la que la base ya tiene guardada —
    cumple la restricción y el valor no se mueve, que era todo el objetivo.
    """
    enviados = escribir_consolidado(
        [_fila_stripe('Andy Faz_2026-08-11_169', '69715127')],
        ya_en_base={'Andy Faz_2026-08-11_169': {
            'registration_date': '2026-08-11', 'identification': '69715127'}},
    )

    assert len(enviados) == 1
    assert enviados[0]['registration_date'] == '2026-08-11', (
        'ni la de hoy (re-sella) ni ausente (rechaza la tanda entera)')


def test_un_pago_que_ya_existe_no_pierde_el_documento_corregido_a_mano(escribir_consolidado):
    """El archivo NO puede pisar el documento que corrigió una persona.

    Caso real: 59 pagos de Stripe tienen el documento puesto a mano, y en 55 de
    ellos es porque el archivo lo trae VACÍO. Este camino nunca llegó a correr
    en producción (siempre reventaba antes), así que al destrabarlo empezaría a
    escribir la celda vacía encima y esos pagos perderían su cruce en silencio.
    """
    enviados = escribir_consolidado(
        [_fila_stripe('Andy Faz_2026-08-11_169', '')],
        ya_en_base={'Andy Faz_2026-08-11_169': {
            'registration_date': '2026-08-11', 'identification': '69715127'}},
    )

    assert enviados[0]['identification'] == '69715127', (
        'el documento de un pago que ya existe es el que la base tiene')


def test_las_dos_tandas_llevan_las_mismas_claves(escribir_consolidado):
    """PGRST102: un array de merge exige el mismo set de claves en cada objeto.

    Es la razón por la que los pagos nuevos y los que ya existen van en dos POST
    separados. Con la fecha de vuelta en su sitio los dos objetos son iguales,
    así que la diferencia deja de existir — y si alguien vuelve a quitarle una
    clave a un grupo, esta prueba lo caza.
    """
    enviados = escribir_consolidado(
        [_fila_stripe('nuevo_1', '111'), _fila_stripe('viejo_1', '222')],
        ya_en_base={'viejo_1': {
            'registration_date': '2026-08-11', 'identification': '222'}},
    )

    assert len(enviados) == 2
    assert {tuple(sorted(f)) for f in enviados} == {tuple(sorted(enviados[0]))}, (
        'todos los objetos del lote tienen que compartir las claves')


def test_un_pago_nuevo_estrena_la_fecha_de_hoy(escribir_consolidado):
    """Guardo de regresión: el pago que entra por primera vez no cambia.

    Pasa con el código viejo y con el nuevo — está para que arreglar el grupo
    de los que ya existen no le toque la fecha a los que no.
    """
    hoy = datetime.now(pytz.timezone('America/Bogota')).strftime('%Y-%m-%d')
    enviados = escribir_consolidado([_fila_stripe('nuevo_1', '111')])

    assert enviados[0]['registration_date'] == hoy
    assert enviados[0]['identification'] == '111'


def test_dos_corridas_del_mismo_archivo_dan_las_mismas_llaves(bandeja):
    """Idempotencia de la ingesta.

    Importa por la numeración `(pago 2)`: se asigna por POSICIÓN dentro del
    archivo justo para que reprocesarlo no genere llaves distintas. Si cambiara,
    el mismo pago entraría dos veces con dos identidades.
    """
    una = [f[10] for f in bandeja('bc2576', 'bc2576_extracto.json')['consolidado']]
    otra = [f[10] for f in bandeja('bc2576', 'bc2576_extracto.json')['consolidado']]
    assert una == otra


# ── Un archivo que se leyó pero no traía pagos ───────────────────────────────

def _extracto_sin_pagos(monkeypatch, brutas):
    """Simula un extracto que el parser lee bien y cuyas líneas se filtran
    TODAS: solo liquidaciones del datáfono, de PSE, el 4x1000 e intereses."""
    def _parse(_buf, stats=None):
        if stats is not None:
            stats['brutas'] = brutas
        return []
    monkeypatch.setattr(procesar_todos.BANCOS_BANCOLOMBIA['bc2576']['mod'], 'parse_pdf', _parse)


def test_un_extracto_leido_sin_pagos_se_archiva(bandeja, monkeypatch):
    """Ya hizo su trabajo. Dejarlo en la bandeja lo convierte en trabajo eterno:
    el vigilante lo ve como archivo nuevo y dispara la cadena cada 15 minutos —
    dos extractos de 2833 lo hicieron durante 11 días."""
    _extracto_sin_pagos(monkeypatch, brutas=21)

    escrito = bandeja('bc2576', 'bc2576_extracto.json')

    assert escrito['_movidos'] == ['f1'], 'el archivo se quedó atascado en la bandeja'
    assert not escrito.get('consolidado')

    # Y queda constancia: 21 líneas leídas, 0 pagos. Es lo que permite archivarlo
    # sin esconder un filtro roto — la visibilidad ya no depende de que el
    # archivo se atasque.
    anotado = escrito['_anotados'][0]
    assert anotado['filas_leidas'] == 21
    assert anotado['pagos_nuevos'] == 0
    assert anotado.get('resultado', 'ok') == 'ok'


def test_un_extracto_que_no_se_pudo_leer_se_queda_en_la_bandeja(bandeja, monkeypatch):
    """El otro caso, que hasta hoy se confundía con el anterior: si no se
    reconoció NINGÚN movimiento, el archivo puede estar roto y tiene que quedar
    a la vista."""
    _extracto_sin_pagos(monkeypatch, brutas=0)

    escrito = bandeja('bc2576', 'bc2576_extracto.json')

    assert escrito['_movidos'] == [], 'un archivo ilegible no se puede archivar'
    assert escrito['_anotados'][0]['resultado'] == 'error'


def test_un_extracto_sin_pagos_tampoco_se_archiva_en_simulacion(bandeja, monkeypatch):
    _extracto_sin_pagos(monkeypatch, brutas=21)

    escrito = bandeja('bc2576', 'bc2576_extracto.json', argv_extra=('--dry-run',))

    assert escrito['_movidos'] == []
