"""
Piezas compartidas por los tests.

La idea de fondo: este pipeline solo toca el mundo exterior por tres puertas —
`utils/supabase.py`, `utils/drive.py` y `utils/sheets.py`. Ningún script llama a
la red por su cuenta. Eso permite correr el código **de verdad** (los `main()`
completos, no una réplica) contra datos fijos, sustituyendo únicamente esas
puertas. Es la técnica que se usó a mano para validar el arreglo del ledger el
28 de julio; acá queda instalada para no rehacerla cada vez.
"""

import io
import json
import os
import sys
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / 'fixtures'
SNAPSHOTS = Path(__file__).parent / 'snapshots'


# ── PDF de Bancolombia sin pdfplumber ─────────────────────────────────────────

class _PaginaFalsa:
    """Una página tal como la ve `parse_pdf`: texto plano y palabras con
    posición. Reproduce solo lo que el parser consulta."""

    def __init__(self, datos: dict):
        self._texto = datos['text']
        self._words = datos['words']

    def extract_text(self) -> str:
        return self._texto

    def extract_words(self, **_kwargs) -> list[dict]:
        # Copia defensiva: el parser agrupa y ordena estas listas, y un test no
        # debe poder ensuciar el fixture para el siguiente.
        return [dict(w) for w in self._words]


class _PdfFalso:
    def __init__(self, paginas: list[dict]):
        self.pages = [_PaginaFalsa(p) for p in paginas]

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


@pytest.fixture
def extracto_bancolombia(monkeypatch):
    """Devuelve una función `cargar(modulo, nombre_fixture)` que deja al módulo
    del banco leyendo el extracto guardado en vez de un PDF real.

    Se parchea `pdfplumber.open` dentro del módulo bajo prueba, así que
    `parse_pdf()` corre entero: el pegado de líneas partidas, el recorte del
    nombre de sucursal, `_separar_sucursal_desc`, `_extraer_refs` y los filtros
    de descripción. Lo único que no se ejerce es la extracción del PDF en sí,
    que es de pdfplumber y no de este repo.
    """
    def cargar(modulo, nombre_fixture: str):
        paginas = json.loads((FIXTURES / nombre_fixture).read_text(encoding='utf-8'))
        monkeypatch.setattr(modulo.pdfplumber, 'open', lambda *_a, **_k: _PdfFalso(paginas))
        return io.BytesIO(b'%PDF-falso')

    return cargar


# ── Archivos tal cual los recibe cada parser ──────────────────────────────────

@pytest.fixture
def mundo(monkeypatch):
    """Corre el `main()` REAL de un script contra tablas puestas a mano.

    No es una réplica de la lógica ni un fragmento: es el mismo `main()` que
    corre en el VPS, con las lecturas respondidas desde un diccionario y las
    escrituras capturadas en vez de enviadas. Es la única forma hoy de probar
    el cruce de punta a punta — las dos `main()` pasan de 600 líneas y no se
    pueden llamar por partes. (Partirlas es la Fase 5; esto no depende de eso
    y por eso se puede hacer ya.)

    Uso:

        capturado = mundo(cruzar, tablas={'cartera_inscrip': [...], ...})
        assert capturado['cruce_cartera'][0]['estado_cruce'] == 'cruzado'
    """
    def correr(modulo, tablas: dict, entorno: dict | None = None) -> dict:
        # `main()` parsea sus propios flags desde sys.argv, y ahí están los de
        # pytest: sin esto, argparse se queja de "unrecognized arguments" y
        # tumba el test con SystemExit.
        monkeypatch.setattr(sys, 'argv', [f'{modulo.__name__}.py'])

        for clave, valor in {
            'SUPABASE_URL': 'https://falso.supabase.co',
            'SUPABASE_SERVICE_ROLE_KEY': 'llave-de-prueba',
            # Vacías a propósito: con estas sin configurar, el lector del
            # ReportePagosWompi se corta solo y no intenta salir a Drive.
            'GOOGLE_SA_JSON': '',
            'WOMPI_REPORTE_DRIVE_FOLDER_ID': '',
            **(entorno or {}),
        }.items():
            monkeypatch.setenv(clave, valor)

        # Una tabla que el mundo no define se ve vacía, no explota: así un test
        # solo declara lo que le importa a su caso.
        def _select(_url, _srk, tabla, select=None, **_kw):
            return [dict(f) for f in tablas.get(tabla, [])]

        monkeypatch.setattr(modulo, 'select_all', _select)

        capturado: dict[str, list] = {}

        def _capturar(destino):
            def escribir(*args, **_kw):
                filas = args[-1] if args else []
                capturado.setdefault(destino, []).extend(
                    filas if isinstance(filas, list) else [filas])
            return escribir

        for nombre, destino in [
            ('upsert_cruce', 'cruce_cartera'),
            ('upsert_pagos_apartados', 'pagos_apartados'),
            ('update_cruce_valores', 'cruce_cartera_update'),
            ('update_consolidated_campos', 'consolidated_update'),
            ('upsert_cartera_preventiva', 'cartera_preventiva'),
            ('insert_cartera_preventiva_lineas', 'cartera_preventiva_lineas'),
            ('upsert_pago_asociaciones', 'pago_asociaciones'),
            ('upsert_cartera_saldos_favor', 'cartera_saldos_favor'),
        ]:
            if hasattr(modulo, nombre):
                monkeypatch.setattr(modulo, nombre, _capturar(destino))

        # delete_by_keys tiene la llave en otra posición (tabla, columna, valores).
        if hasattr(modulo, 'delete_by_keys'):
            def _borrar(_url, _srk, tabla, _col, valores, **_kw):
                capturado.setdefault(f'borrado:{tabla}', []).extend(valores)
            monkeypatch.setattr(modulo, 'delete_by_keys', _borrar)

        modulo.main()
        return capturado

    return correr


@pytest.fixture
def snapshot():
    """Compara un resultado contra el guardado. El corazón de la caracterización.

    Estos tests **no afirman que el pipeline esté bien**. Afirman que hoy hace
    exactamente lo mismo que ayer, que es la única defensa posible contra el
    patrón que se repitió todo julio: un cambio que parecía inocuo movía un
    número, nadie lo notaba, y el error aparecía semanas después mirando una
    pantalla.

    Para bendecir un cambio de comportamiento a propósito:

        ACTUALIZAR_SNAPSHOTS=1 pytest

    y **revisar el diff de `tests/snapshots/` antes de comitear** — ahí es donde
    se ve, en números, qué cambió de verdad. Un diff más grande de lo esperado
    es la señal.
    """
    def comparar(nombre: str, obtenido):
        ruta = SNAPSHOTS / f'{nombre}.json'
        serializado = json.loads(json.dumps(obtenido, default=str, ensure_ascii=False))

        if os.environ.get('ACTUALIZAR_SNAPSHOTS') or not ruta.exists():
            ruta.parent.mkdir(parents=True, exist_ok=True)
            ruta.write_text(
                json.dumps(serializado, indent=1, ensure_ascii=False, sort_keys=False),
                encoding='utf-8')
            if not os.environ.get('ACTUALIZAR_SNAPSHOTS'):
                pytest.skip(f'snapshot {nombre} creado por primera vez; revisarlo y volver a correr')
            return

        esperado = json.loads(ruta.read_text(encoding='utf-8'))
        assert serializado == esperado, (
            f'"{nombre}" cambió respecto al snapshot.\n'
            f'Si el cambio es intencional: ACTUALIZAR_SNAPSHOTS=1 pytest, '
            f'y revisar el diff de tests/snapshots/{nombre}.json antes de comitear.'
        )

    return comparar


@pytest.fixture
def abrir_fixture():
    """`abrir_fixture('wompi_reporte.csv')` → BytesIO, que es lo que reciben
    los `parse_file()` cuando el archivo baja de Drive."""
    def abrir(nombre: str) -> io.BytesIO:
        ruta = FIXTURES / nombre
        if not ruta.exists():
            pytest.skip(f'falta el fixture {nombre} (correr scripts/generar_fixtures.py)')
        return io.BytesIO(ruta.read_bytes())

    return abrir
