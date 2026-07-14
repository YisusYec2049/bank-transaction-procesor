"""Utilidades numéricas compartidas entre todos los parsers."""

import re

_RE_DV_NIT = re.compile(r'-\d$')


def normalizar_nit(valor: str) -> str:
    """Quita el dígito de verificación de un NIT (ej. "860004922-4" ->
    "860004922"), si lo tiene. Los NIT de empresas (Persona Jurídica) en las
    hojas de referencia de cartera a veces vienen con DV y las transacciones
    que llegan de los bancos/pasarelas nunca lo traen. No toca formatos con
    guion que no sean exactamente "-<un dígito>" al final (ej. prefijos de
    documento de extranjería como "ID-", "CI-", "DNI-")."""
    return _RE_DV_NIT.sub('', valor)


SUFIJOS_IGNORABLES = ('PN', 'PJ', 'P')


def normalizar_sufijo(valor: str) -> str:
    """Quita el sufijo (PN, PJ, o un "P" truncado, con o sin espacio antes,
    ej. "411 PJ" o "4844P") para comparar el número base.

    Un "P" suelto es ambiguo por sí mismo (puede ser "PN" o "PJ" truncado) —
    esta función solo calcula el número base, no decide a cuál corresponde.
    Esa resolución depende del resto de valores de la misma llave y se hace
    en cada lookup, no aquí."""
    v = valor.strip()
    upper = v.upper()
    for suf in SUFIJOS_IGNORABLES:
        if upper.endswith(suf):
            return v[:-len(suf)].strip()
    return v


def parse_valor(s: str) -> float | None:
    """
    Detecta formato europeo (1.435.500,00) vs americano (300,000.00).
    Regla: si lastIndexOf(',') > lastIndexOf('.') → europeo; si no → americano.
    Replica parseValor() de n8n.
    """
    s = str(s or '').strip().replace('$', '').replace(' ', '')
    if not s:
        return None
    last_comma = s.rfind(',')
    last_dot   = s.rfind('.')
    try:
        if last_comma > last_dot:
            n = float(s.replace('.', '').replace(',', '.'))
        else:
            n = float(s.replace(',', ''))
    except ValueError:
        return None
    return round(n, 2)


def valor_str(v) -> str:
    """
    Replica String(parseFloat(n.toFixed(2))) de JavaScript.
    Garantiza que matching_key sea idéntico al generado por n8n.
      500000.0 → "500000"
      435.5    → "435.5"
      435.53   → "435.53"
    """
    rounded = round(float(v), 2)
    if rounded == int(rounded):
        return str(int(rounded))
    s = f'{rounded:.2f}'.rstrip('0')
    return s.rstrip('.')


def norm_valor_str(raw) -> str:
    """Normaliza un valor leído desde Sheets (str o número) usando valor_str."""
    try:
        return valor_str(float(str(raw).replace(',', '').strip()))
    except (ValueError, TypeError):
        return str(raw).strip()
