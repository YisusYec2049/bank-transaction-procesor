# 006 — Recortar una lectura no es neutral

Traer menos filas de la base parece una optimización sin consecuencias. **No lo
es.** Los índices con los que el sistema decide se construyen sobre las filas que
se leyeron, así que un universo más chico puede hacer que **decida donde antes se
abstenía**.

## El caso concreto

Al construir el modo puntual (`cruzar.py --solo`), lo natural era traer de
`cartera_inscrip` solo las filas del documento de ese pago.

Pero la resolución del INCP de un pago por link de WOMPI busca por **número de
inscripción**, no por documento, y tiene una regla de seguridad: si encuentra
**dos candidatos** para la misma base (ej. `3300PN` y `3300PJ`, los dos
inscritos), **no resuelve nada** — es una ambigüedad real que el reporte no
confirma.

Con la lectura filtrada por documento, esa base habría aparecido con **un solo
candidato**. La regla de seguridad no se dispara, y el pago se asigna a una
inscripción que nadie confirmó. En silencio.

## La regla

Un filtro tiene que ser **suficiente para la decisión que alimenta**, no solo
pequeño. Antes de recortar una lectura, preguntarse *qué se decide con estas
filas y con qué se compara*.

Cuando el recorte no es suficiente, se consulta aparte y por la llave correcta
(`_IdInscripcionPorBaseLazy` hace exactamente eso).

## Cómo se verifica

No razonando: **corriendo las dos versiones y comparando**. El modo puntual tiene
una prueba que exige que produzca *exactamente la misma fila* que la corrida
completa, y eso se comprobó además contra producción con un pago real, campo por
campo.

La misma regla frenó otro cambio "obvio": reemplazar la deduplicación de "lo que
entró ayer" por "todo el histórico". Es más estricto, suena mejor, y habría
renumerado los pagos repetidos al reprocesar un archivo — ver
[001](001-matching-key.md).
