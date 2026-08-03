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
from pathlib import Path

import pytest

import procesar_todos

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
            'GOOGLE_SA_JSON': '/dev/null',
        }.items():
            monkeypatch.setenv(clave, valor)
        # Cada banco lee su bandeja y su Histórico de un id propio en el
        # entorno. Los nombres importan: la primera versión de este test usaba
        # `..._HIST_...` en vez de `..._HISTORICO_...` y pasaba igual, porque el
        # `.env` de la máquina traía el nombre bueno. Falló recién en CI, donde
        # no hay `.env` — de ahí el `load_dotenv` neutralizado más abajo.
        for var in ('BC2576_INBOX_FOLDER_ID', 'BC2576_HISTORICO_FOLDER_ID'):
            monkeypatch.setenv(var, 'carpeta')

        # Nada de leer el .env real: una prueba que depende del entorno de quien
        # la corre no prueba lo mismo en dos máquinas.
        monkeypatch.setattr(procesar_todos, 'load_dotenv', lambda *_a, **_k: None)

        monkeypatch.setattr(procesar_todos, '_build_services', lambda: 'drive')
        monkeypatch.setattr(procesar_todos, 'list_files',
                            lambda _d, _f: [{'id': 'f1', 'name': 'extracto.pdf'}])
        monkeypatch.setattr(procesar_todos, 'download_file',
                            lambda _d, _i: io.BytesIO(b'%PDF'))

        paginas = json.loads((FIXTURES / fixture_pdf).read_text(encoding='utf-8'))
        modulo = procesar_todos.BANCOS_BANCOLOMBIA[banco]['mod']
        monkeypatch.setattr(modulo.pdfplumber, 'open', lambda *_a, **_k: _PdfFalso(paginas))

        escrito: dict[str, list] = {}
        movidos: list[str] = []

        monkeypatch.setattr(procesar_todos, 'upsert',
                            lambda _u, _k, filas: escrito.setdefault('consolidado', []).extend(filas))
        monkeypatch.setattr(procesar_todos, 'upsert_pagos_apartados',
                            lambda _u, _k, filas: escrito.setdefault('apartados', []).extend(filas))
        monkeypatch.setattr(procesar_todos, 'keys_del_dia_anterior',
                            lambda *_a, **_k: set(ya_en_supabase))
        monkeypatch.setattr(procesar_todos, 'existing_matching_keys',
                            lambda *_a, **_k: set())
        monkeypatch.setattr(procesar_todos, 'select_all', lambda *_a, **_k: [])
        monkeypatch.setattr(procesar_todos, 'move_file',
                            lambda _d, fid, _dest: movidos.append(fid))

        procesar_todos.main()
        escrito['_movidos'] = movidos
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


def test_dos_corridas_del_mismo_archivo_dan_las_mismas_llaves(bandeja):
    """Idempotencia de la ingesta.

    Importa por la numeración `(pago 2)`: se asigna por POSICIÓN dentro del
    archivo justo para que reprocesarlo no genere llaves distintas. Si cambiara,
    el mismo pago entraría dos veces con dos identidades.
    """
    una = [f[10] for f in bandeja('bc2576', 'bc2576_extracto.json')['consolidado']]
    otra = [f[10] for f in bandeja('bc2576', 'bc2576_extracto.json')['consolidado']]
    assert una == otra
