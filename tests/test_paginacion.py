"""
Traer "todas las filas" tiene que traer todas las filas.

Bug encontrado el 2026-08-02, dormido desde siempre: PostgREST tiene un tope
propio (`max-rows`, 1.000 en Supabase) y **recorta las páginas más grandes sin
avisar** — devuelve 200 con menos filas de las pedidas. `select_all` avanzaba de
a `page_size` y cortaba cuando la página venía corta, así que interpretaba ese
recorte como "ya no hay más datos".

Consecuencia medida: pedir páginas de 5.000 sobre `cartera_ingresos_wompi`
devolvía **1.000 filas de 23.348**, sin error y sin una línea en el log. Con esa
tabla mocha, el cruce habría dejado de reconocer al 95% de la gente y habría
mandado sus pagos a excepciones — el tipo de falla que en este repo se descubre
semanas después mirando una pantalla.

No mordía porque ningún llamador pasaba un `page_size` mayor. Estaba esperando
a que alguien "optimizara" subiéndolo.
"""

import pytest

from utils import supabase

TOPE_DEL_SERVIDOR = 1000


class _RespuestaPagina:
    def __init__(self, filas):
        self._filas = filas
        self.status_code = 200
        self.ok = True
        self.text = ''
        self.url = 'https://x'
        self.request = None

    def json(self):
        return self._filas

    def raise_for_status(self):
        pass


@pytest.fixture
def servidor_con_tope(monkeypatch):
    """Un servidor que responde como el real: nunca más de 1.000 filas por
    página, sin importar cuántas se le pidan."""
    def montar(total_filas: int):
        universo = [{'id': i} for i in range(total_filas)]
        peticiones = []

        def _get(url, params=None, headers=None, **_kw):
            rango = headers['Range']
            desde, hasta = (int(x) for x in rango.split('-'))
            pedidas = hasta - desde + 1
            entrega = min(pedidas, TOPE_DEL_SERVIDOR)
            peticiones.append((desde, pedidas))
            return _RespuestaPagina(universo[desde:desde + entrega])

        monkeypatch.setattr(supabase.http, 'get', _get)
        return peticiones

    return montar


@pytest.mark.parametrize('page_size', [1000, 5000, 25000])
def test_trae_todo_aunque_el_servidor_recorte(servidor_con_tope, page_size):
    servidor_con_tope(23348)

    filas = supabase.select_all('https://x', 'k', 'cartera_ingresos_wompi',
                                page_size=page_size)

    assert len(filas) == 23348, (
        f'con page_size={page_size} se perdieron {23348 - len(filas)} filas: el '
        f'servidor recortó la página y se interpretó como fin de datos'
    )
    assert [f['id'] for f in filas] == list(range(23348)), 'las filas llegaron desordenadas o repetidas'


def test_avanza_por_lo_recibido_no_por_lo_pedido(servidor_con_tope):
    """Si avanzara por lo pedido, se saltaría las filas que el servidor no
    alcanzó a mandar — perdiendo datos en el medio, que es peor que cortar."""
    peticiones = servidor_con_tope(2500)

    supabase.select_all('https://x', 'k', 'tabla', page_size=5000)

    offsets = [desde for desde, _ in peticiones]
    assert offsets == [0, 1000, 2000, 2500], (
        f'el avance no siguió a lo que el servidor devolvió: {offsets}'
    )


def test_tabla_vacia(servidor_con_tope):
    servidor_con_tope(0)
    assert supabase.select_all('https://x', 'k', 'tabla') == []


def test_justo_un_multiplo_del_tope(servidor_con_tope):
    """El caso de borde clásico: exactamente 2.000 filas. Hace falta una
    petición más, la que vuelve vacía, para saber que no hay nada después."""
    peticiones = servidor_con_tope(2000)

    filas = supabase.select_all('https://x', 'k', 'tabla', page_size=1000)

    assert len(filas) == 2000
    assert len(peticiones) == 3, 'faltó la petición que confirma que ya no hay más'
