# 003 — Un pago se reparte una sola vez en su vida

Cuando un pago se aplica sobre las cuotas que alguien debe, queda registrado que
ya se usó. **Nunca se vuelve a repartir**, aunque reaparezca en un archivo,
aunque se reprocese, aunque cambie la versión de cartera.

Sin esto, cada corrida volvería a cobrar los mismos pagos sobre las cuotas — y
como el pipeline corre solo, nadie lo vería hasta que la cartera estuviera
destruida.

## Por qué es más difícil de lo que parece

La memoria de "esto ya se cobró" vive en **dos lugares distintos**, y uno de
ellos no lo controla este sistema:

1. **Lo que repartió el pipeline**: en `pago_asociaciones`.
2. **Lo que cobró el equipo a mano**: anotado en la columna
   `codigo_transaccion_1` del Excel de cartera.

El 30 de julio de 2026 el Excel del mes llegó **sin un solo código** (el del mes
anterior traía 1.581). Esa memoria se perdía entera al cambiar de versión: iban a
reaplicarse **581 pagos**, 113 de ellos ya cobrados a mano por casi $100 M. Desde
entonces la lista se arma con la cartera viva **más las archivadas**.

## La trampa de la columna con dos escritores

En `codigo_transaccion_1` escriben **dos manos**: el Excel anota lo que cobró el
proceso manual, y el propio pipeline la llena al aplicar un pago. Leerlas como si
fueran lo mismo hace que un pago repartido esta mañana quede bloqueado **por su
propio rastro** al archivarse la cartera saliente.

`fecha_cruce` es lo único que las distingue: si está, la fila la tocó el
pipeline. Ignorarlo bloquea los pagos del día; darlo por sentado rompe cualquier
búsqueda por código.

## La excepción, y por qué existe

Al cambiar de versión de cartera se archiva todo y las tablas vivas quedan
vacías, así que para el pipeline **todos los pagos vuelven a ser nuevos**. Un
pago archivado se re-aplica solo si **el pipeline lo repartió el mismo día en que
se cargó la cartera**.

El criterio es *cuándo lo repartió el sistema*, no *cuándo pagó la persona*: el
34% de los pagos llegan con dos o más días de desfase entre el pago y el archivo
del banco. Con la fecha del banco, un pago del viernes repartido el lunes por la
mañana desaparecía si esa tarde se cargaba cartera nueva.
