"""
Parser para reportes CSV de Stripe.

Columnas esperadas:
  Created date (UTC), Card Brand, Amount, Fee, Customer Email,
  Checkout Custom Field 1/2/3 Value, Checkout Line Item Summary,
  Currency, Converted Amount, Converted Currency, Card Name

  [0] VAL                 ← ''
  [1] identification      ← '' (vacío)
  [2] payment_date        ← DD-MM-YYYY
  [3] transaction_code_1  ← Card Name
  [4] transaction_code_2  ← '{Converted Amount} {Converted Currency}'
  [5] email               ← Customer Email
  [6] payment_method      ← 'STRIPE_USA'
  [7] program             ← Card Name
  [8] phone               ← Checkout Custom Field 3 Value
  [9] payment_amount      ← Converted Amount (monto en COP)
  [10] matching_key       ← {Card Name}_{YYYY-MM-DD}_{valor_js}
"""

import csv
import io
import logging

from utils.parser import valor_str

log = logging.getLogger(__name__)

HEADERS = [
    'VAL',
    'identification', 'payment_date', 'transaction_code_1', 'transaction_code_2',
    'email', 'payment_method', 'program', 'phone', 'payment_amount', 'matching_key',
]


def parse_file(buf: io.BytesIO, filename: str = '') -> list[dict]:
    text   = buf.read().decode('utf-8', errors='replace')
    reader = csv.DictReader(io.StringIO(text))
    results = []

    for row in reader:
        r = {k.strip(): str(v).strip() for k, v in row.items()}

        fecha_raw = (r.get('Created date (UTC)') or r.get('created date (utc)') or '')[:10]
        if not fecha_raw:
            continue
        try:
            yyyy, mm, dd = fecha_raw.split('-')
            payment_date = f'{dd}-{mm}-{yyyy}'
        except ValueError:
            continue

        converted_raw = r.get('Converted Amount') or r.get('converted amount') or ''
        try:
            converted = float(converted_raw.replace(',', ''))
        except ValueError:
            continue
        if converted <= 0:
            continue

        stripe_id       = r.get('id') or r.get('Id') or ''
        card_name       = r.get('Card Name') or r.get('card name') or ''
        email           = r.get('Customer Email') or r.get('customer email') or ''
        currency        = r.get('Converted Currency') or r.get('converted currency') or ''
        custom_field3   = (r.get('Checkout Custom Field 3 Value')
                           or r.get('checkout custom field 3 value') or '')

        # matching_key idéntico a n8n: cardName_YYYY-MM-DD_valorJs
        matching_key = f'{card_name}_{fecha_raw}_{valor_str(converted)}'

        results.append({
            'stripe_id':     stripe_id,
            'payment_date':  payment_date,
            'card_name':     card_name,
            'email':         email,
            'converted':     converted,
            'currency':      currency,
            'custom_field3': custom_field3,
            'matching_key':  matching_key,
        })

    log.info('Stripe: %d filas parseadas', len(results))
    return results


def normalize(raw_rows: list[dict]) -> list[list]:
    return [
        [
            '',                                          # [0]  VAL
            '',                                          # [1]  identification (vacío)
            r['payment_date'],                           # [2]
            r['card_name'],                              # [3]  transaction_code_1
            f"{r['converted']} {r['currency']}".strip(), # [4]  transaction_code_2
            r['email'],                                  # [5]
            'STRIPE_USA',                                # [6]
            r['card_name'],                              # [7]  program
            r['custom_field3'],                          # [8]  phone
            r['converted'],                              # [9]
            r['matching_key'],                           # [10]
        ]
        for r in raw_rows
    ]


def cheque_logic(normalized_rows: list[list]) -> tuple[list, list]:
    """Stripe no maneja cheques."""
    return normalized_rows, []
