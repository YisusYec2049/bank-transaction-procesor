"""
Pseudonimizador determinista para construir fixtures de prueba.

Por qué existe: los tests de caracterización necesitan archivos con la forma
EXACTA de los que manda cada banco, pero esos archivos traen cédulas, nombres,
correos y teléfonos de personas reales, y este repo va a GitHub.

Dos propiedades que lo hacen usable:

1. **Determinista y estable.** El mismo documento real produce siempre el mismo
   documento falso, en todos los archivos. Sin eso, un pago del CSV de WOMPI
   dejaría de cruzar contra su inscripción en el Excel de cartera, y el fixture
   no serviría para probar el cruce.
2. **Preserva el formato.** Un documento de 10 dígitos se reemplaza por otro de
   10 dígitos; un correo sigue siendo un correo. Los parsers deciden por
   longitud y por expresiones regulares, así que cambiar la forma cambiaría lo
   que se está probando.

Lo que NO se toca, a propósito: montos, fechas, ids de transacción de las
pasarelas y nombres de sucursal. Los montos y las fechas son la mitad de las
llaves de cruce; los ids de transacción son opacos (no dicen nada de nadie); y
una sucursal es una oficina del banco, no una persona.

La semilla es fija: regenerar los fixtures dos veces da el mismo resultado, así
que un diff en `tests/fixtures/` significa que cambió el archivo de origen, no
el generador.
"""

import hashlib
import re

_SEMILLA = b'matching-test/fixtures/v1'

# Nombres y apellidos inventados. La lista es corta a propósito: los fixtures
# tienen decenas de personas, no miles, y repetir un apellido es realista.
_NOMBRES = [
    'ANDREA', 'BRUNO', 'CAMILA', 'DIEGO', 'ELENA', 'FABIAN', 'GABRIELA',
    'HECTOR', 'IRENE', 'JULIAN', 'KAREN', 'LORENA', 'MARCOS', 'NATALIA',
    'OSCAR', 'PAULA', 'QUINTIN', 'ROSA', 'SAMUEL', 'TATIANA', 'ULISES',
    'VALERIA', 'WILMER', 'XIMENA', 'YOLANDA', 'ZULEMA',
]
_APELLIDOS = [
    'ACOSTA', 'BENITEZ', 'CARDENAS', 'DUARTE', 'ESCOBAR', 'FUENTES', 'GALINDO',
    'HERRERA', 'IBARRA', 'JARAMILLO', 'LOZANO', 'MEJIA', 'NARANJO', 'OSPINA',
    'PALACIOS', 'QUIROGA', 'RIVAS', 'SALAZAR', 'TORRES', 'URIBE', 'VARGAS',
    'ZAMBRANO',
]

_RE_EMAIL = re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+')
_RE_DIGITOS = re.compile(r'\d+')


def _hash(valor: str) -> int:
    """Entero estable a partir de un texto. blake2b con semilla fija: el mismo
    texto da el mismo número en cualquier máquina y en cualquier corrida (a
    diferencia de hash(), que Python aleatoriza entre procesos)."""
    h = hashlib.blake2b(valor.encode('utf-8'), key=_SEMILLA, digest_size=8)
    return int.from_bytes(h.digest(), 'big')


def documento(valor: str) -> str:
    """Reemplaza los dígitos conservando longitud y cualquier separador.

    '1.234.567.890' → '4.087.512.336'   ·   '860004922-4' → '733915608-2'

    El primer dígito nunca queda en cero: un documento que empiece por 0 se
    lee distinto (y Excel se lo come al abrirlo), así que el fixture no debe
    introducir ese caso por accidente.
    """
    if not valor:
        return valor

    def _sub(m: re.Match) -> str:
        bloque = m.group(0)
        semilla = _hash(f'doc:{valor}:{m.start()}')
        digitos = str(semilla).ljust(len(bloque), '0')[:len(bloque)]
        if len(digitos) > 1 and digitos[0] == '0':
            digitos = '1' + digitos[1:]
        return digitos

    return _RE_DIGITOS.sub(_sub, valor)


def nombre(valor: str) -> str:
    """Nombre falso, con la misma cantidad de palabras que el real.

    Conservar el número de palabras importa: `_extraer_refs()` de Bancolombia
    descarta la primera palabra en mayúsculas para no confundir un nombre con
    una referencia, así que un nombre de 3 palabras y uno de 1 recorren caminos
    distintos del parser.
    """
    if not valor or not valor.strip():
        return valor

    partes = valor.split()
    salida = []
    for i, parte in enumerate(partes):
        h = _hash(f'nom:{valor}:{i}')
        fuente = _NOMBRES if i == 0 else _APELLIDOS
        falso = fuente[h % len(fuente)]
        salida.append(falso if parte.isupper() else falso.title())
    return ' '.join(salida)


def email(valor: str) -> str:
    """Correo falso en un dominio reservado por la RFC 2606 (`example.com`),
    que por definición no le pertenece a nadie ni resuelve a ningún servidor."""
    if not valor or '@' not in valor:
        return valor
    h = _hash(f'mail:{valor}')
    usuario = f'{_NOMBRES[h % len(_NOMBRES)].lower()}.{_APELLIDOS[(h // 7) % len(_APELLIDOS)].lower()}'
    return f'{usuario}{h % 100}@example.com'


def telefono(valor: str) -> str:
    """Celular colombiano falso: 10 dígitos empezando por 3, como los reales."""
    if not valor:
        return valor
    solo_digitos = re.sub(r'\D', '', valor)
    if len(solo_digitos) < 7:
        return valor
    h = _hash(f'tel:{valor}')
    return '3' + str(h).ljust(9, '0')[:9]


def texto_libre(valor: str) -> str:
    """Para campos donde quien paga escribe lo que quiere (el `ref. 2` de WOMPI,
    la descripción de Stripe). Se limpia lo que sí es identificable —correos y
    números largos— y se deja el resto: ahí es donde vive el nombre del programa,
    que es justo lo que el pipeline necesita leer."""
    if not valor:
        return valor
    salida = _RE_EMAIL.sub(lambda m: email(m.group(0)), valor)
    return re.sub(r'\b\d{6,}\b', lambda m: documento(m.group(0)), salida)
