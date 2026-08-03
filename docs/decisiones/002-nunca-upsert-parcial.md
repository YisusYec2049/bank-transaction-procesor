# 002 — Nunca upsert parcial sobre tablas con `NOT NULL`

Para actualizar algunas columnas de `cruce_cartera` o
`consolidated_transactions` se usa **UPDATE**, nunca un upsert con payload
parcial. Suena a detalle y costó un día entero de producción.

## Qué pasa si se hace

Las dos tablas tienen columnas `NOT NULL` sin valor por defecto
(`registration_date`, por ejemplo). Un `POST ... on_conflict=matching_key` con
solo dos campos hace que Postgres construya la tupla del INSERT para poder
evaluar el conflicto — y **valida el `NOT NULL` antes** de que el `ON CONFLICT`
alcance a redirigir al UPDATE.

Resultado: error `23502`, aunque la fila exista y solo se quisiera tocar una
columna.

## Cómo se descubrió

El 24 de julio de 2026 se agregó una escritura así. El crash ocurría **antes** de
escribir el cruce, y los scripts van encadenados con `&&`, así que **desde ese
deploy ninguna corrida escribió nada** — ni el cron ni los botones de la
plataforma. Se notó porque alguien reportó que "las modificaciones no se
reflejan", no porque algo avisara.

Al lado hubo un segundo aprendizaje: el error real (`23502`) estuvo invisible en
el log porque solo se veía `HTTPError: 400`. Por eso todas las llamadas pasan hoy
por `_raise_for_status`, que loguea el cuerpo de la respuesta **antes** de
lanzar — ahí es donde PostgREST pone el detalle.

## La regla

Actualizaciones parciales por `matching_key` → UPDATE (un PATCH por fila, o la
función de base de [005](005-escrituras-en-lote.md)). El upsert queda para
cuando se escribe la fila completa.
