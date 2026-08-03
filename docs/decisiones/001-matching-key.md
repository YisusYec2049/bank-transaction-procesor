# 001 — `matching_key` es la identidad de un pago

Cada pago tiene una llave que lo identifica en todo el sistema. Todas las
escrituras son **por esa llave**, nunca por posición ni por inserción ciega. De
ahí sale la garantía más importante del pipeline: **correr dos veces no cobra dos
veces**.

## De dónde sale

- **Pasarelas** (WOMPI, Stripe, PlaceToPay, PayU): el id de transacción que ellas
  dan. Es único por definición.
- **Bancos**: no dan ninguno, así que se compone —
  `{DD/MM/YYYY}_{referencia}_{valor}`.

## El problema de la llave compuesta

Dos pagos reales distintos pueden generar la misma llave: la misma persona
pagando el mismo monto el mismo día, dos veces. Pasa, y no es un error del banco.

Durante un mes el sistema los trató como duplicados y **se comía el segundo**.
Cuando se corrigió, la primera versión del arreglo quedó en la capa equivocada:
había un descarte por `(fecha, descripción, valor)` dentro del parser, dos
archivos más arriba, que anulaba la corrección. El pago volvía a perderse.

## La regla

Las colisiones **se numeran, no se descartan**: el primer pago va sin sufijo, el
segundo ` (pago 2)`, el tercero ` (pago 3)`. El nombre importa — antes decía
`(duplicado)` y el equipo entendía que el registro estaba repetido, cuando lo que
dice es cuántas veces pagó la persona.

**El sufijo se asigna por POSICIÓN dentro del archivo**, no por lo que ya exista
en la base. Es lo que hace que reprocesar el mismo archivo dé siempre las mismas
llaves. Cualquier cambio en la deduplicación previa puede alterar esas posiciones
y hacer que el mismo pago entre con otra identidad — ver
[003](003-un-pago-se-reparte-una-vez.md) y
[006](006-recortar-una-lectura-no-es-neutral.md).

## Lo que se acepta a cambio

Con la información del extracto, "pagó dos veces" y "el banco reportó el pago dos
veces" se ven **idénticos**. El sistema asume lo primero. Se eligió así porque
**perder plata en silencio es peor que verla duplicada**: lo segundo alguien lo
nota, lo primero no.
