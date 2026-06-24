"""Utilidades numéricas compartidas entre todos los parsers."""


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
