"""
La puerta del servicio HTTP.

`trigger_server.py` está expuesto a internet por Tailscale Funnel y sus
endpoints corren el pipeline entero o cambian la versión de cartera. El token
compartido es la **única** autenticación que hay, así que conviene que su
comparación esté fijada por una prueba.
"""

import contextlib
import os

import pytest

os.environ.setdefault('TRIGGER_TOKEN', 'token-de-prueba')

flask = pytest.importorskip('flask', reason='flask solo está instalado en el VPS')

import trigger_server  # noqa: E402


@pytest.fixture
def peticion(monkeypatch):
    """Arma un contexto de petición con la cabecera que se quiera."""
    def con_cabecera(valor: str | None):
        entorno = {}
        if valor is not None:
            entorno['HTTP_AUTHORIZATION'] = valor
        return trigger_server.app.test_request_context('/trigger/cruce', environ_base=entorno)
    return con_cabecera


def test_acepta_el_token_correcto(peticion):
    with peticion(f'Bearer {trigger_server.TRIGGER_TOKEN}'):
        assert trigger_server._autorizado() is True


@pytest.mark.parametrize('cabecera', [
    None,                       # sin cabecera
    '',                         # vacía
    'Bearer ',                  # sin token
    'Bearer equivocado',        # token que no es
    'token-de-prueba',          # sin el prefijo Bearer
    'bearer token-de-prueba',   # prefijo en minúscula
])
def test_rechaza_todo_lo_demas(peticion, cabecera):
    with peticion(cabecera):
        assert trigger_server._autorizado() is False


def test_la_comparacion_es_en_tiempo_constante():
    """`==` sobre cadenas corta en el primer carácter distinto, así que el
    tiempo de respuesta filtra cuántos caracteres se acertaron y el token se
    puede adivinar de a uno. `compare_digest` recorre siempre todo.

    Se comprueba sobre el código y no cronometrando: medir microsegundos en un
    test da falsos negativos según la carga de la máquina, y lo que importa
    acá es que no se vuelva a `==` en un descuido.

    Se mira el árbol de sintaxis, no el texto del archivo. La primera versión
    de esta prueba buscaba la cadena "compare_digest" en el código fuente de la
    función — y el docstring de arriba la menciona, así que **seguía pasando
    con la comparación revertida a `==`**. Un test que se prueba a sí mismo con
    su propia documentación no prueba nada.
    """
    import ast
    import inspect
    import textwrap

    arbol = ast.parse(textwrap.dedent(inspect.getsource(trigger_server._autorizado)))
    funcion = arbol.body[0]
    # Fuera el docstring: solo importa lo que se ejecuta.
    cuerpo = [n for n in funcion.body
              if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]

    llamadas = {ast.unparse(n.func) for n in ast.walk(ast.Module(body=cuerpo, type_ignores=[]))
                if isinstance(n, ast.Call)}
    assert any('compare_digest' in c for c in llamadas), (
        f'la comparación del token no usa compare_digest; llamadas encontradas: {llamadas}'
    )

    comparaciones = [n for n in ast.walk(ast.Module(body=cuerpo, type_ignores=[]))
                     if isinstance(n, ast.Compare)]
    assert not comparaciones, (
        'quedó una comparación directa en el chequeo del token: '
        f'{[ast.unparse(c) for c in comparaciones]}'
    )


def test_todos_los_endpoints_que_escriben_piden_token():
    """Ninguna ruta que dispare trabajo puede quedar abierta.

    Se revisa por introspección para que valga también para las rutas que
    alguien agregue después, que es cuando este tipo de descuido pasa.
    """
    import inspect

    sin_proteger = []
    for regla in trigger_server.app.url_map.iter_rules():
        if regla.endpoint == 'static':
            continue
        metodos = regla.methods or set()
        if not ({'POST', 'PUT', 'PATCH', 'DELETE'} & metodos):
            continue
        vista = trigger_server.app.view_functions[regla.endpoint]
        if '_autorizado' not in inspect.getsource(vista):
            sin_proteger.append(str(regla))

    assert not sin_proteger, f'endpoints que escriben sin pedir token: {sin_proteger}'


# ── Cola de reprocesos con modo puntual ──────────────────────────────────────

def test_la_cadena_pasa_el_pago_a_cruzar(monkeypatch):
    """`--solo` tiene que llegar a `cruzar.py`, y solo a él."""
    ejecutados = []

    class _Res:
        returncode, stdout, stderr = 0, '', ''

    monkeypatch.setattr(trigger_server.subprocess, 'run',
                        lambda cmd, **_kw: ejecutados.append(cmd) or _Res())

    trigger_server._correr_cadena(sync=False, solo='PAGO-1')

    assert ['--solo', 'PAGO-1'] == ejecutados[0][2:], 'cruzar.py no recibió el pago'
    assert '--solo' not in ejecutados[1], (
        'se le pasó --solo a cartera preventiva, que no lo soporta'
    )


def test_sin_pago_la_cadena_corre_completa(monkeypatch):
    ejecutados = []

    class _Res:
        returncode, stdout, stderr = 0, '', ''

    monkeypatch.setattr(trigger_server.subprocess, 'run',
                        lambda cmd, **_kw: ejecutados.append(cmd) or _Res())

    trigger_server._correr_cadena(sync=True, solo=None)

    assert len(ejecutados) == 3, 'con sync deben correr los tres scripts'
    assert all('--solo' not in cmd for cmd in ejecutados)


def test_dos_pagos_encolados_obligan_a_correr_completo():
    """Si mientras corre uno se encolan dos pagos distintos, la re-corrida
    tiene que cubrir a los dos.

    Quedarse con uno dejaría el otro cambio sin aplicar — justo lo que esta
    cola existe para evitar. Ante la duda, gana el alcance más amplio.
    """
    trigger_server._state['status'] = 'running'
    trigger_server._pendiente = None
    try:
        with trigger_server.app.test_request_context('/'):
            trigger_server._disparar(sync=False, solo='PAGO-1')
            trigger_server._disparar(sync=False, solo='PAGO-2')
        assert trigger_server._pendiente['solo'] is None
        assert trigger_server._pendiente['sync'] is False
    finally:
        trigger_server._state['status'] = 'idle'
        trigger_server._pendiente = None


def test_el_mismo_pago_encolado_dos_veces_sigue_siendo_puntual():
    trigger_server._state['status'] = 'running'
    trigger_server._pendiente = None
    try:
        with trigger_server.app.test_request_context('/'):
            trigger_server._disparar(sync=False, solo='PAGO-1')
            trigger_server._disparar(sync=False, solo='PAGO-1')
        assert trigger_server._pendiente['solo'] == 'PAGO-1'
    finally:
        trigger_server._state['status'] = 'idle'
        trigger_server._pendiente = None


def test_un_pedido_con_sync_gana_sobre_uno_puntual():
    """"Actualizar cruce" (que baja archivos) es más amplio que un reproceso de
    un pago: si los dos se encolan, tiene que ganar el completo."""
    trigger_server._state['status'] = 'running'
    trigger_server._pendiente = None
    try:
        with trigger_server.app.test_request_context('/'):
            trigger_server._disparar(sync=False, solo='PAGO-1')
            trigger_server._disparar(sync=True, solo=None)
        assert trigger_server._pendiente['sync'] is True
        assert trigger_server._pendiente['solo'] is None
    finally:
        trigger_server._state['status'] = 'idle'
        trigger_server._pendiente = None


# ── /trigger/sync: solo traer archivos ───────────────────────────────────────

def test_sync_solo_corre_sync_cartera(monkeypatch):
    """El botón "Buscar archivos nuevos" no debe recalcular nada.

    Antes usaba el disparador que además cruza y reparte pagos: minutos de
    trabajo para responder "¿llegó una cartera nueva?".
    """
    ejecutados = []

    class _Res:
        returncode, stdout, stderr = 0, '', ''

    monkeypatch.setattr(trigger_server.subprocess, 'run',
                        lambda cmd, **_kw: ejecutados.append(cmd) or _Res())

    trigger_server._correr_cadena(sync=True, solo_sync=True)

    assert len(ejecutados) == 1, f'corrió de más: {[c[-1] for c in ejecutados]}'
    assert ejecutados[0][-1] == 'sync_cartera.py'


def test_si_se_encola_un_recalculo_deja_de_ser_solo_sync():
    """Ante la duda, el alcance más amplio.

    Si mientras corre un sync alguien pide un reproceso, la re-corrida tiene que
    incluirlo: quedarse en "solo sync" dejaría ese cambio sin aplicar.
    """
    trigger_server._state['status'] = 'running'
    trigger_server._pendiente = None
    try:
        with trigger_server.app.test_request_context('/'):
            trigger_server._disparar(sync=True, solo_sync=True)
            trigger_server._disparar(sync=False, solo=None)
        assert trigger_server._pendiente['solo_sync'] is False
    finally:
        trigger_server._state['status'] = 'idle'
        trigger_server._pendiente = None


def test_dos_syncs_encolados_siguen_siendo_solo_sync():
    trigger_server._state['status'] = 'running'
    trigger_server._pendiente = None
    try:
        with trigger_server.app.test_request_context('/'):
            trigger_server._disparar(sync=True, solo_sync=True)
            trigger_server._disparar(sync=True, solo_sync=True)
        assert trigger_server._pendiente['solo_sync'] is True
    finally:
        trigger_server._state['status'] = 'idle'
        trigger_server._pendiente = None


def test_sync_comparte_el_carril_del_pipeline():
    """`sync_cartera.py` reemplaza las tablas que `cruzar.py` lee. Correrlos a
    la vez le daría al cruce tablas a medio actualizar — el mismo motivo por el
    que el cron los encadena en vez de lanzarlos en paralelo."""
    trigger_server._state['status'] = 'running'
    trigger_server._pendiente = None
    try:
        with trigger_server.app.test_request_context('/'):
            resp, codigo = trigger_server._disparar(sync=True, solo_sync=True)
        assert codigo == 202
        assert resp.get_json()['status'] == 'queued', 'no se encoló: correría en paralelo'
    finally:
        trigger_server._state['status'] = 'idle'
        trigger_server._pendiente = None


# ── La ingesta: la corrida que lee los archivos ──────────────────────────────

def _espiar_scripts(monkeypatch):
    ejecutados = []

    class _Res:
        returncode, stdout, stderr = 0, '', ''

    monkeypatch.setattr(trigger_server.subprocess, 'run',
                        lambda cmd, **_kw: ejecutados.append(cmd) or _Res())
    return ejecutados


def test_la_ingesta_lee_los_archivos_y_despues_cruza(monkeypatch):
    """Es el único disparador que corre `procesar_todos.py`: los otros tres
    trabajan sobre pagos que YA entraron. Y el orden importa — `cruzar.py` deja
    el cruce al día y cartera preventiva lee de ahí."""
    ejecutados = _espiar_scripts(monkeypatch)

    trigger_server._correr_cadena(sync=True, ingesta=True)

    nombres = [cmd[1].split('/')[-1] for cmd in ejecutados]
    assert nombres == ['sync_cartera.py', 'procesar_todos.py',
                       'cruzar.py', 'cruzar_cartera_preventiva.py']


def test_la_ingesta_NO_sella(monkeypatch):
    """El sello lo pone únicamente la corrida de las 9:30. Un botón no puede
    cerrarle la puerta a un pago: el sello no se deshace desde la pantalla."""
    ejecutados = _espiar_scripts(monkeypatch)

    trigger_server._correr_cadena(sync=True, ingesta=True)

    assert all('--cierre-diario' not in cmd for cmd in ejecutados)


def test_ningun_otro_disparador_lee_archivos(monkeypatch):
    """Si `procesar_todos.py` se colara en el reproceso de un pago, corregir un
    documento metería archivos nuevos de contrabando."""
    for kwargs in ({'sync': True, 'solo': None}, {'sync': False, 'solo': 'PAGO-1'},
                   {'sync': True, 'solo_sync': True}):
        ejecutados = _espiar_scripts(monkeypatch)
        trigger_server._correr_cadena(**kwargs)
        assert all('procesar_todos.py' not in cmd[1] for cmd in ejecutados), kwargs


def test_una_ingesta_encolada_le_gana_a_un_pago_puntual():
    """La cola colapsa siempre al alcance MÁS AMPLIO: si mientras corre algo se
    piden una ingesta y un pago suelto, la re-corrida tiene que ser la ingesta
    completa. Quedarse con el pago dejaría los archivos sin entrar."""
    trigger_server._pendiente = {'sync': False, 'solo': 'PAGO-1',
                                 'solo_sync': False, 'ingesta': False}
    trigger_server._state['status'] = 'running'
    try:
        with trigger_server.app.test_request_context('/'):
            trigger_server._disparar(sync=True, ingesta=True)
        assert trigger_server._pendiente['ingesta'] is True
        assert trigger_server._pendiente['solo'] is None
        assert trigger_server._pendiente['solo_sync'] is False
    finally:
        trigger_server._pendiente = None
        trigger_server._state['status'] = 'idle'


# ── El candado que se comparte con el cron ───────────────────────────────────

def test_el_boton_espera_a_que_el_cron_suelte_el_candado(monkeypatch, tmp_path):
    """Hasta el 2026-09-06 el cron y este servicio NO se protegían entre sí: el
    cron usaba un archivo en /tmp y el servicio un candado en memoria, y encima
    la unidad de systemd le da su propio /tmp. Dos corridas a la vez reparten
    plata sobre las mismas cuotas."""
    import fcntl

    candado = tmp_path / 'pipeline.lock'
    monkeypatch.setattr(trigger_server, 'CANDADO', str(candado))

    otro = open(candado, 'w')
    fcntl.flock(otro, fcntl.LOCK_EX)
    try:
        with pytest.raises(TimeoutError):
            with trigger_server._candado_del_pipeline(espera_s=0):
                raise AssertionError('no debía entrar con el candado tomado')
    finally:
        fcntl.flock(otro, fcntl.LOCK_UN)
        otro.close()


def test_con_el_candado_libre_la_corrida_entra(monkeypatch, tmp_path):
    monkeypatch.setattr(trigger_server, 'CANDADO', str(tmp_path / 'pipeline.lock'))

    with trigger_server._candado_del_pipeline(espera_s=0):
        pass

    # Y queda libre al salir: dos corridas seguidas no se bloquean entre sí.
    with trigger_server._candado_del_pipeline(espera_s=0):
        pass


def test_el_candado_se_suelta_aunque_la_corrida_falle(monkeypatch, tmp_path):
    """Si un error dejara el candado tomado, el cron se quedaría sin correr
    para siempre y nadie lo notaría hasta que faltaran pagos."""
    monkeypatch.setattr(trigger_server, 'CANDADO', str(tmp_path / 'pipeline.lock'))

    with contextlib.suppress(RuntimeError):
        with trigger_server._candado_del_pipeline(espera_s=0):
            raise RuntimeError('revienta a mitad')

    with trigger_server._candado_del_pipeline(espera_s=0):
        pass
