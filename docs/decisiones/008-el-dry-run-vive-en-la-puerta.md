# 008 — El modo simulación vive en la puerta, no en cada llamada

`--dry-run` corre el pipeline **contra los datos de producción, en lectura**, y
registra lo que escribiría en vez de escribirlo.

El interruptor está implementado dentro de `utils/supabase.py` y `utils/drive.py`
—las dos puertas al exterior— y no en cada punto donde el código escribe.

## Por qué ahí

Entre `cruzar.py` y `cruzar_cartera_preventiva.py` hay **19 puntos de
escritura**. Envolverlos uno por uno funciona hasta que alguien agrega el número
20 y se olvida; a partir de ahí el modo simulación **miente en silencio**, que es
justo el desenlace que esta herramienta existe para evitar.

En la puerta no se escapa ninguna, ni las de mañana. Y hay una prueba que lo
verifica **por introspección**: falla nombrando cualquier función de escritura
que aparezca sin guarda.

## Para qué se usa de verdad

Para responder *"¿este cambio movió algún dato?"* sin tener que adivinar. El
archivo de salida trae **una línea por fila, ordenada**, así que dos corridas se
comparan con `diff`:

```bash
python cruzar.py --dry-run --dry-run-salida /tmp/antes.jsonl
# ...aplicar el cambio...
python cruzar.py --dry-run --dry-run-salida /tmp/despues.jsonl
diff /tmp/antes.jsonl /tmp/despues.jsonl
```

Todos los cambios de rendimiento del 2 de agosto de 2026 se validaron así, contra
producción: idénticos fila por fila.

**La primera versión guardaba solo 3 filas de muestra por llamada.** Con eso, el
`diff` comparaba los totales y no los datos — un cambio que alterara un valor de
la fila 40 habría pasado como "no cambió nada". Una herramienta de verificación
que no verifica es peor que ninguna, porque da permiso.

## Lo que no hace

- **No intercepta lecturas**, a propósito: la gracia es correr contra los datos de
  verdad. Un dry-run sobre datos inventados no dice nada sobre lo que pasaría hoy.
- **No garantiza que el resultado sea correcto**, solo lo hace visible. Sigue
  haciendo falta leerlo.
