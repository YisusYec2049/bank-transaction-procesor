# Decisiones

Las reglas que sostienen este sistema y **por qué** son así. Cada una nació de
algo que pasó en producción: casi todas se aprendieron rompiéndolas.

No es documentación de la API ni un manual de uso — para eso está el
[README](../../README.md). Esto responde la pregunta que uno se hace al leer
código ajeno: *"¿por qué está hecho de esta forma tan rara?"*.

| # | Decisión |
|---|---|
| [001](001-matching-key.md) | `matching_key` es la identidad de un pago |
| [002](002-nunca-upsert-parcial.md) | Nunca upsert parcial sobre tablas con `NOT NULL` |
| [003](003-un-pago-se-reparte-una-vez.md) | Un pago se reparte una sola vez en su vida |
| [004](004-el-pipeline-no-cierra-solo.md) | El pipeline nunca cierra un pago como "no identificable" |
| [005](005-escrituras-en-lote.md) | Las escrituras en lote van por función de base |
| [006](006-recortar-una-lectura-no-es-neutral.md) | Recortar una lectura no es neutral |
| [007](007-pruebas-de-caracterizacion.md) | Las pruebas son de caracterización, con datos anonimizados |
| [008](008-el-dry-run-vive-en-la-puerta.md) | El modo simulación vive en la puerta, no en cada llamada |

## Cómo agregar una

Cuando tomes una decisión que a alguien le va a parecer arbitraria dentro de seis
meses, escribila acá. Formato: **qué se decidió**, **qué pasaba antes**, **por
qué esta salida y no la obvia**. Corta — si necesita más de una pantalla,
probablemente son dos decisiones.
