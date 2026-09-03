"""La ventana de dos días hábiles antes del sello (requerimiento 3 del área).

Lo que se prueba acá, en orden de importancia:

  1. Que el fin de semana y los festivos **no le descuenten margen** a un pago.
     Es el agujero que motivó el cambio: hasta el 3 de septiembre un pago que
     entraba el viernes se sellaba el domingo.
  2. Que la ventana y el sello sean **exactamente complementarios**, con el
     borde correcto: la corrida que completa los días hábiles aplica Y sella.
  3. Que los festivos calculados sean los de Colombia de verdad, incluidos los
     dos que el vault ya tenía anotados por otro camino (7 y 17 de agosto de
     2026).
"""

from datetime import date

import pytest

from utils.dias_habiles import (
    DIAS_HABILES_VENTANA,
    es_habil,
    festivos,
    habiles_transcurridos,
    ventana_abierta,
    ventana_vencida,
)

# --------------------------------------------------------------------------
# Los festivos
# --------------------------------------------------------------------------

def test_festivos_2026_son_los_dieciocho_reales():
    esperados = {
        date(2026, 1, 1),    # Año Nuevo — jueves
        date(2026, 1, 12),   # Reyes, movido al lunes
        date(2026, 3, 23),   # San José, movido al lunes
        date(2026, 4, 2),    # Jueves Santo
        date(2026, 4, 3),    # Viernes Santo
        date(2026, 5, 1),    # Trabajo — viernes, fijo
        date(2026, 5, 18),   # Ascensión
        date(2026, 6, 8),    # Corpus Christi
        date(2026, 6, 15),   # Sagrado Corazón
        date(2026, 6, 29),   # San Pedro y San Pablo
        date(2026, 7, 20),   # Independencia — fijo
        date(2026, 8, 7),    # Batalla de Boyacá — viernes, fijo
        date(2026, 8, 17),   # Asunción, movido al lunes
        date(2026, 10, 12),  # Día de la Raza
        date(2026, 11, 2),   # Todos los Santos
        date(2026, 11, 16),  # Independencia de Cartagena
        date(2026, 12, 8),   # Inmaculada — martes, fijo
        date(2026, 12, 25),  # Navidad — viernes, fijo
    }
    assert festivos(2026) == esperados


def test_los_dos_festivos_que_el_vault_detecto_por_otro_camino():
    """El 10 de agosto quedó anotado que los únicos días hábiles sin cruces
    fueron el 7 y el 17 de agosto. El cálculo tiene que dar los mismos."""
    assert date(2026, 8, 7) in festivos(2026)    # viernes
    assert date(2026, 8, 17) in festivos(2026)   # lunes


def test_ley_emiliani_mueve_al_lunes_y_deja_quietos_los_fijos():
    # Asunción cae sábado 15/08/2026 -> se corre al lunes 17.
    assert date(2026, 8, 15) not in festivos(2026)
    assert date(2026, 8, 17) in festivos(2026)
    # Boyacá es fijo: cae viernes 7 y ahí se queda.
    assert date(2026, 8, 7) in festivos(2026)


def test_no_esta_clavado_a_un_anio():
    """2027 tiene la Pascua en marzo, así que las fechas móviles se corren."""
    assert date(2027, 3, 25) in festivos(2027)   # Jueves Santo
    assert date(2027, 3, 26) in festivos(2027)   # Viernes Santo
    assert date(2027, 4, 2) not in festivos(2027)
    assert len(festivos(2027)) == 18


def test_es_habil_distingue_los_tres_casos():
    assert es_habil(date(2026, 9, 3))        # jueves normal
    assert not es_habil(date(2026, 9, 5))    # sábado
    assert not es_habil(date(2026, 9, 6))    # domingo
    assert not es_habil(date(2026, 8, 7))    # viernes festivo


# --------------------------------------------------------------------------
# El contador
# --------------------------------------------------------------------------

def test_el_dia_de_entrada_no_cuenta():
    """Llega después de la corrida de las 9:30: nadie pudo trabajarlo ese día."""
    assert habiles_transcurridos(date(2026, 9, 1), date(2026, 9, 1)) == 0


def test_fin_de_semana_no_descuenta_margen():
    viernes = date(2026, 9, 4)
    assert habiles_transcurridos(viernes, date(2026, 9, 5)) == 0   # sábado
    assert habiles_transcurridos(viernes, date(2026, 9, 6)) == 0   # domingo
    assert habiles_transcurridos(viernes, date(2026, 9, 7)) == 1   # lunes
    assert habiles_transcurridos(viernes, date(2026, 9, 8)) == 2   # martes


def test_un_festivo_tampoco_descuenta():
    """El caso real: entró el jueves 6 de agosto y el viernes 7 era Boyacá."""
    jueves = date(2026, 8, 6)
    assert habiles_transcurridos(jueves, date(2026, 8, 7)) == 0   # festivo
    assert habiles_transcurridos(jueves, date(2026, 8, 10)) == 1  # lunes
    assert habiles_transcurridos(jueves, date(2026, 8, 11)) == 2  # martes


def test_fechas_al_reves_no_corren_el_reloj():
    assert habiles_transcurridos(date(2026, 9, 3), date(2026, 9, 1)) == 0


# --------------------------------------------------------------------------
# La ventana y el sello
# --------------------------------------------------------------------------

@pytest.mark.parametrize('llega, sella, descripcion', [
    (date(2026, 9, 1), date(2026, 9, 3), 'martes -> jueves'),
    (date(2026, 9, 4), date(2026, 9, 8), 'viernes -> martes'),
    (date(2026, 9, 5), date(2026, 9, 8), 'sábado  -> martes'),
    (date(2026, 8, 6), date(2026, 8, 11), 'jueves antes de Boyacá -> martes'),
])
def test_la_tabla_que_se_le_prometio_al_usuario(llega, sella, descripcion):
    """Cada fila de la tabla del requerimiento, con su día de sello exacto.

    En el día del sello la ventana sigue ABIERTA (esa corrida todavía aplica) y
    además vence: se aplica y se sella en la misma corrida.
    """
    assert ventana_abierta(llega, sella), descripcion
    assert ventana_vencida(llega, sella), descripcion
    # El día hábil anterior todavía no vence.
    anterior = sella
    while True:
        anterior = date.fromordinal(anterior.toordinal() - 1)
        if es_habil(anterior):
            break
    assert not ventana_vencida(llega, anterior), descripcion


def test_ventana_y_sello_son_complementarios_salvo_en_el_borde():
    """Recorre 40 días seguidos: nunca puede haber un pago que ya no se aplique
    solo y tampoco se selle — eso es plata muerta sin que nadie se entere."""
    llega = date(2026, 8, 5)
    for n in range(40):
        hoy = date.fromordinal(llega.toordinal() + n)
        abierta = ventana_abierta(llega, hoy)
        vencida = ventana_vencida(llega, hoy)
        assert abierta or vencida, f'{hoy}: ni se aplica ni se sella'


def test_el_viernes_ya_no_se_sella_el_domingo():
    """La regresión que motivó todo: antes `entrada < hoy` sellaba el domingo."""
    viernes = date(2026, 9, 4)
    assert not ventana_vencida(viernes, date(2026, 9, 5))
    assert not ventana_vencida(viernes, date(2026, 9, 6))
    assert ventana_vencida(viernes, date(2026, 9, 8))


def test_sin_fecha_de_entrada_queda_vivo_y_no_se_sella():
    """Ante la duda, no se pierde plata: el sello es irreversible."""
    assert ventana_abierta(None, date(2026, 9, 3))
    assert not ventana_vencida(None, date(2026, 9, 3))


def test_acepta_texto_iso_con_hora():
    """`registration_date` llega de PostgREST como texto, a veces con hora."""
    assert ventana_abierta('2026-09-04', date(2026, 9, 7))
    assert ventana_abierta('2026-09-04T10:31:00+00:00', '2026-09-07')
    assert ventana_vencida('2026-09-04', '2026-09-08')


def test_la_ventana_son_dos_dias_habiles():
    """Si alguien cambia la constante, esta prueba dice qué se movió."""
    assert DIAS_HABILES_VENTANA == 2
