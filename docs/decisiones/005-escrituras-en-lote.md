# 005 — Las escrituras en lote van por una función de base

Actualizar muchas filas con **valores distintos** se hace llamando a una función
de Postgres (`actualizar_cruce_valores`, `actualizar_consolidated_campos`) con
todo el lote en un solo pedido.

## Qué había antes

Una petición HTTP **por fila**. Medido contra producción el 2 de agosto de 2026:

| | Filas | Costo |
|---|---|---|
| Cruce inverso | 1.080 | ~4,5 min |
| Consolidado WOMPI | 825 | ~3,4 min |

Casi 8 minutos de una corrida de 11, gastados en pedir mil veces el mismo favor.
El cálculo nunca fue lo lento.

## Por qué una función y no el upsert de PostgREST

Porque sería un insert parcial sobre tablas con `NOT NULL` — ver
[002](002-nunca-upsert-parcial.md). La función es la única forma de mandar
valores distintos para filas distintas en una sola petición sin pasar por el
camino del INSERT.

## El detalle delicado: los lotes son heterogéneos

Cada objeto del lote trae **solo las columnas que se quieren cambiar**, y no
siempre las mismas: la re-evaluación de WOMPI manda `incp` únicamente cuando
logró resolverlo.

Por eso la función pregunta si la clave existe (`? 'columna'`) en vez de usar
`COALESCE`. Rellenar con NULL las ausentes **borraría el INCP** de las filas
donde no venía.

## Y el despliegue no depende del orden

Si la función no existe en la base, el código lo detecta, sigue fila por fila y
lo avisa en el log. Esa independencia es deliberada: el acoplamiento inverso
—código nuevo que exige SQL ya corrido— es lo que dejó el motor caído un día
entero (ver [002](002-nunca-upsert-parcial.md)).
