# 004 — El pipeline nunca cierra un pago como "no identificable"

El sistema produce dos estados: `cruzado` (encontró a quién pertenece el pago) o
`pendiente` (no lo encontró, va a Excepciones para que alguien lo mire).

**`no_identificable` lo pone únicamente una persona**, desde la plataforma.

## Por qué

Ese estado es **terminal**: el pipeline no vuelve a mirar esas filas nunca más.

El 22 de julio de 2026 se automatizó por un rato: los pagos de WOMPI sin cruce se
cerraban solos, con el argumento de que nadie podía resolverlos a mano y solo
hacían ruido en Excepciones. Eran **109 pagos por $97 millones**.

El argumento era falso en un punto que cambia todo: "hoy nadie puede resolverlo"
no es lo mismo que "nunca se va a poder". Esos documentos podían aparecer en
cartera la semana siguiente. Al cerrarlos solo, el sistema garantizaba que
**nunca se reintentaría**.

## La regla complementaria

Una fila terminal **se reabre** si aparece señal nueva: si su documento o su
correo empiezan a cruzar, vuelve al proceso. No desautoriza a la persona que la
cerró — solo aplica a filas que no tenían ninguna señal cuando se marcaron.

Lo mismo con las correcciones de documento: corregir una cédula reabre la fila
para volver a cruzarla. Antes no lo hacía, y en la tabla de correcciones quedó el
rastro de alguien peleando con eso — el mismo documento corregido en un sentido
y en el otro el mismo día, hasta que se rindió y lo marcó "no identificable".
