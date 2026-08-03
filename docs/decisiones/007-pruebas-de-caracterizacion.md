# 007 — Las pruebas son de caracterización, con datos anonimizados

Las pruebas de este repo **no afirman que el resultado sea correcto**. Afirman
que sigue siendo **el mismo**.

## Por qué esa forma y no otra

El historial de bugs de este sistema tiene todos la misma silueta: no fallaban,
simplemente hacían algo distinto **en silencio**, y los encontraba el equipo
mirando pantallas días o semanas después. Una función de cheques rota un mes. Una
fase completa que nunca aplicó un pago. Un descarte que se comía pagos reales.

Ninguno lo habría explicado un test. Pero cualquiera de ellos habría **gritado el
día que el número cambió**, que es todo lo que se le pide a una prueba acá.

Por eso el grueso son *snapshots*: se guarda lo que produce cada parser y se
compara. Si cambia, el test falla y dice dónde.

```bash
ACTUALIZAR_SNAPSHOTS=1 pytest    # bendecir un cambio a propósito
```

**Revisar el diff de `tests/snapshots/` antes de comitear.** Ahí se ve, en
números, qué cambió de verdad. Un diff más grande de lo esperado es la señal.

## Los datos

`tests/fixtures/` tiene extractos reales **anonimizados**: mismo formato exacto,
cero datos de personas. Se regeneran con `scripts/generar_fixtures.py`, que
**verifica su propia salida y se niega a escribir si detecta fugas**.

Esa verificación no es adorno. La primera versión del generador dejó pasar **80
correos reales** escondidos en columnas de payload JSON que ninguna regla por
nombre de columna iba a atrapar. Y una segunda tanda porque **la gente teclea su
propio nombre** en el campo libre de "detalle del pago", fuera de la columna que
le corresponde.

Los archivos de origen nunca entran al repo. El repo es público.

## Un test que pasa no es un test que sirve

Tres de las pruebas escritas el 2 de agosto de 2026 no probaban lo que decían, y
solo se supo **rompiendo el código a propósito** y viendo que no fallaban:

- La del token buscaba la cadena `compare_digest` en el código de la función… y
  **el docstring de arriba la menciona**, así que seguía pasando con la
  comparación revertida.
- La del modo puntual no ejercitaba su propia trampa, porque esa rama solo corre
  cuando hay reporte de WOMPI y en los tests no lo hay.
- Una de la ingesta usaba un nombre de variable de entorno equivocado y **pasaba
  gracias al `.env` de la máquina**. Falló recién en CI, donde no hay `.env`.

Al escribir un test, romper lo que debería proteger y confirmar que se entera.
