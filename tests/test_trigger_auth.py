"""
La puerta del servicio HTTP.

`trigger_server.py` está expuesto a internet por Tailscale Funnel y sus
endpoints corren el pipeline entero o cambian la versión de cartera. El token
compartido es la **única** autenticación que hay, así que conviene que su
comparación esté fijada por una prueba.
"""

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
