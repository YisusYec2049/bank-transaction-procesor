# matching-test — conciliación de pagos

[![tests](https://github.com/YisusYec2049/bank-transaction-procesor/actions/workflows/tests.yml/badge.svg)](https://github.com/YisusYec2049/bank-transaction-procesor/actions/workflows/tests.yml)

Pipeline en Python que toma los extractos que mandan los bancos y las pasarelas
de pago, los normaliza a un formato común, y decide **a qué cuota de qué
inscripción corresponde cada pago**.

Reemplaza un proceso que se hacía a mano en Excel. Corre solo, una vez al día,
en un VPS. La gente del área no lo ve: consume su resultado desde una
aplicación web aparte (`financial-platform`), que lee las mismas tablas.

```
   Google Drive                  este repo                     Supabase
┌────────────────┐        ┌─────────────────────┐        ┌───────────────────┐
│ extractos del  │───────>│ 2. normalizar       │───────>│ consolidated_     │
│ día (8 fuentes)│        │    procesar_todos   │        │ transactions      │
├────────────────┤        ├─────────────────────┤        ├───────────────────┤
│ cartera y      │───────>│ 1. sincronizar      │───────>│ tablas espejo     │
│ referencias    │        │    sync_cartera     │        │ de referencia     │
└────────────────┘        ├─────────────────────┤        ├───────────────────┤
                          │ 3. identificar      │───────>│ cruce_cartera     │
                          │    cruzar           │        │ pagos_apartados   │
                          ├─────────────────────┤        ├───────────────────┤
                          │ 4. aplicar a las    │───────>│ cartera_preventiva│
                          │    cuotas           │        │ pago_asociaciones │
                          │ cruzar_cartera_     │        │ cartera_saldos_   │
                          │ preventiva          │        │ favor             │
                          └─────────────────────┘        └─────────┬─────────┘
                                                                   v
                                                          financial-platform
                                                           (app web, otro repo)
```

## Los cuatro pasos

Corren **en ese orden**, encadenados con `&&`: cada uno solo arranca si el
anterior terminó bien.

| | Script | Qué hace |
|---|---|---|
| 1 | `sync_cartera.py` | Baja de Drive los archivos de referencia (inscripciones, ingresos, cartera del mes) y refresca las tablas espejo. La cartera nueva queda **en espera**: entra en vivo solo cuando una persona aprieta "Cargar Cartera". |
| 2 | `procesar_todos.py` | Lee la bandeja de cada banco, parsea, normaliza a 11 columnas, deduplica y guarda. Mueve a Histórico lo ya procesado. |
| 3 | `cruzar.py` | Le pone identidad a cada pago: a qué inscripción corresponde. Lo que no es de cartera (matrículas, cesantías, cheques, pagos por llave) lo aparta del proceso. |
| 4 | `cruzar_cartera_preventiva.py` | Reparte cada pago sobre las cuotas que esa persona debe, de la más vieja a la más nueva. Es el paso que mueve plata. |

Fuera del cron:

- **`activar_cartera.py`** — cambia la cartera del mes por la nueva, archivando
  la saliente. Lo dispara una persona desde la app, nunca solo.
- **`trigger_server.py`** — servicio HTTP para que la app pida un reproceso sin
  esperar a la corrida del día siguiente.
- **`vigilante.py`** — mira si llegó algo a Drive fuera de hora. Devuelve código
  de salida 1 cuando no hay nada, para que la cadena ni arranque.

## Fuentes que entiende

| Fuente | Formato | Módulo |
|---|---|---|
| Bancolombia 2576 | PDF | `fuentes/bancolombia_2576.py` |
| Bancolombia 2833 | PDF | `fuentes/bancolombia_2833.py` |
| PayU | TSV + CSV (dos archivos que se emparejan) | `fuentes/payu.py` |
| Stripe | CSV | `fuentes/stripe.py` |
| WOMPI | CSV | `fuentes/wompi.py` |
| PlaceToPay | XLSX | `fuentes/placetopay.py` |
| Colpatria | CSV (punto y coma) | `fuentes/colpatria.py` |
| Davivienda | CSV o XLSX | `fuentes/davivienda.py` |

Cada módulo expone lo mismo: `parse_file()` (o `parse_pdf()` para los PDF),
`normalize()` y `cheque_logic()`. Agregar un banco es escribir un módulo más con
esa forma.

**Esquema normalizado** (las 11 columnas que produce `normalize()`):

```
VAL, identification, payment_date, transaction_code_1, transaction_code_2,
email, payment_method, program, phone, payment_amount, matching_key
```

`matching_key` es la llave de todo el sistema. Donde la pasarela da un id de
transacción único, se usa ese; donde no (los bancos), se compone con
`{DD/MM/YYYY}_{referencia}_{valor}`.

## Garantías del sistema

Vale la pena tenerlas presentes antes de tocar nada, porque varias se
aprendieron rompiéndolas:

- **Correr dos veces no cobra dos veces.** El pipeline es idempotente: escribe
  por llave, no acumula. Una corrida interrumpida se arregla volviéndola a
  correr.
- **Un pago se reparte una sola vez en su vida.** Aunque se cambie de versión de
  cartera, aunque el archivo del banco se vuelva a subir.
- **El pipeline nunca cierra un pago como "no identificable".** Ese estado es
  terminal, y solo lo pone una persona desde la app.
- **Lo que corrigió una persona no se pisa.** Las filas ya resueltas no se
  vuelven a calcular.
- **Falla antes de tocar nada.** Los pasos que archivan y reemplazan datos
  archivan primero: si algo revienta, se cae sin haber cambiado el estado.

## Correr en local sin tocar producción

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
```

Hacen falta un `.env` y el `service_account.json` de Google. **Ninguno de los
dos va en git** — se piden a quien administre el VPS.

```bash
pytest                                # no toca la red: corre contra fixtures
ruff check .                          # linter
```

### Simulación (`--dry-run`)

Los cuatro scripts se pueden correr **contra los datos de producción, en
lectura**, registrando lo que escribirían en vez de escribirlo:

```bash
python cruzar.py --dry-run
python cruzar_cartera_preventiva.py --dry-run --dry-run-salida /tmp/hoy.jsonl
```

Al terminar imprime un resumen (qué tabla, cuántas filas) y deja el detalle en
`logs/dry-run-*.jsonl`, **una línea por fila con todo su contenido**, ordenado.
Está pensado para comparar dos corridas con `diff`: si el archivo sale idéntico,
el cambio no movió ningún dato. Es la verificación que se usó para la escritura
en lote — 107 filas, idénticas antes y después.

El interruptor vive en las tres puertas al exterior (`utils/supabase.py`,
`utils/drive.py`, `utils/sheets.py`), no en cada llamada. Así ninguna escritura
se escapa, ni las que se agreguen después: hay una prueba que lo verifica por
introspección y falla si aparece una función de escritura sin guarda.

También se puede encender por entorno, útil desde el cron: `MATCHING_DRY_RUN=1`.

### Chequeo de cuadre

Al final de cada corrida de `cruzar_cartera_preventiva.py`, el pipeline
comprueba pago por pago que **lo aplicado a cuotas más lo que queda disponible
sea igual a lo que entró**, y grita en el log si algo no cierra.

Ese mismo chequeo, hecho a mano, fue el que destapó el bug del ledger del 28 de
julio: de 278 asociaciones, una sola no cuadraba. Los totales daban bien — solo
fallaba el detalle.

### Las pruebas

`tests/` corre contra archivos guardados en `tests/fixtures/`, que son extractos
reales **anonimizados**: mismo formato exacto, cero datos de personas. Se
regeneran con:

```bash
python scripts/generar_fixtures.py --origen ~/carpeta/con/archivos/reales
```

Ese script verifica su propia salida y **se niega a escribir fixtures que
contengan datos reales**. Los archivos de origen nunca entran al repo.

Son **pruebas de caracterización**: no afirman que el resultado sea correcto,
afirman que sigue siendo el mismo. Es la defensa contra el patrón que más daño
ha hecho acá — un cambio que parecía inocuo movía un número y nadie se enteraba
hasta semanas después, mirando una pantalla. Si un resultado cambia a propósito:

```bash
ACTUALIZAR_SNAPSHOTS=1 pytest
```

y **revisar el diff de `tests/snapshots/` antes de comitear**: ahí se ve, en
números, qué cambió de verdad. Un diff más grande de lo esperado es la señal.

## Modo puntual

Cuando alguien corrige una cédula en la plataforma, no hace falta recalcular
todo: cambió un pago.

```bash
python cruzar.py --solo 190093-1784244218-79528
```

Trae de cada tabla **solo lo que ese pago necesita** en vez de leerlas enteras,
y omite los pases globales (la re-evaluación de WOMPI, el archivado de reportes
en Drive). El servicio HTTP lo expone pasando `matching_key`:

```bash
curl -X POST .../trigger/reproceso -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' -d '{"matching_key":"190093-..."}'
```

Sin ese campo, se recalcula todo, como antes.

**La regla que gobierna esto: recortar una lectura no es neutral.** Los índices
de ambigüedad se construyen sobre las filas leídas, así que un universo más
chico puede hacer que el sistema decida donde antes se abstenía. Ya pasó una vez
al diseñarlo: filtrar `cartera_inscrip` por documento hacía que una inscripción
con dos candidatos reales apareciera con uno solo, y el pago se habría asignado
a la equivocada en silencio (por eso existe `_IdInscripcionPorBaseLazy`).

Por eso la prueba que respalda el modo puntual no mide velocidad: verifica que
**produce exactamente la misma fila que la corrida completa**
(`tests/test_modo_puntual.py`), y eso mismo se comprobó contra producción con un
pago real, comparando las dos salidas de `--dry-run`.

## Rendimiento

Dos cosas explican casi todo el tiempo de una corrida, y las dos están
resueltas:

| | Antes | Ahora |
|---|---|---|
| Leer las 8 tablas de referencia | 24,1 s | **14,7 s** (una sola conexión reusada) |
| Escribir el cruce inverso (1.080 filas) | ~4,5 min | **una petición** |
| Escribir el consolidado (825 filas WOMPI) | ~3,4 min | **una petición** |
| Reprocesar un solo pago (`cruzar.py`) | 29 s | **10-12 s** |

El cálculo nunca fue lo lento: se abría una conexión TLS nueva por petición y se
hacía una petición por fila.

Las dos escrituras en lote usan funciones de base
(`actualizar_cruce_valores`, `actualizar_consolidated_campos`). **Si no están
creadas, el pipeline sigue funcionando** por el camino fila por fila y lo avisa
en el log — desplegar el código antes de correr el SQL no rompe nada.

## Configuración de Google

La cuenta de servicio (`service_account.json`) necesita permiso de **Editor**
sobre cada carpeta de Drive que el pipeline lee o escribe: las bandejas de
entrada de cada banco, sus carpetas de Histórico, y las carpetas de archivos de
cruce. Se comparten como cualquier carpeta, poniendo el correo de la cuenta
(`nombre@proyecto.iam.gserviceaccount.com`).

Los ids de cada carpeta van en el `.env`. Una carpeta sin compartir no da error
de permisos: se ve **vacía**, que es bastante peor.

## Despliegue

Corre en un VPS, en `/opt/matching-test`, disparado por cron. El reloj del
sistema está en UTC, así que las horas del crontab van en UTC (Colombia es
UTC−5 y no tiene horario de verano):

```cron
TZ=America/Bogota
# La corrida del día: 15:30 UTC = 10:30 Colombia.
30 15 * * * flock -n /tmp/matching.lock -c '... sync_cartera && procesar_todos && cruzar && cruzar_cartera_preventiva'
# Excepción, por si algo llegó tarde: 11:00 a 19:00 de Colombia.
5,20,35,50 16-23 * * * flock -n /tmp/matching.lock -c '... vigilante && <la misma cadena>'
```

Tres detalles que ya costaron un día de trabajo cada uno:

- **`TZ=` no cambia la hora a la que dispara el cron**, solo la de los logs. El
  horario se escribe en la zona del sistema.
- Los minutos del vigilante evitan el `:30` **a propósito**: si cayera ahí, se
  llevaría el candado de `flock` y la corrida garantizada del día se saltaría.
- `crontab <archivo>` **reemplaza** el crontab entero. Sacar respaldo primero.

El servicio del trigger vive en `deploy/cruce-trigger.service`.

## Estructura

```
procesar_todos.py             normaliza los extractos del día
sync_cartera.py               refresca las tablas de referencia
cruzar.py                     identifica a quién pertenece cada pago
cruzar_cartera_preventiva.py  aplica los pagos a las cuotas
activar_cartera.py            cambia de versión de cartera (manual)
trigger_server.py             HTTP para reprocesos bajo demanda
vigilante.py                  ¿llegó algo nuevo a Drive?
fuentes/                      un módulo por banco/pasarela
utils/                        las tres puertas al exterior: supabase, drive, sheets
scripts/                      generación de fixtures anonimizados
tests/                        pruebas de caracterización + snapshots
sql/                          migraciones (se corren a mano en Supabase)
deploy/                       unidad systemd del servicio de trigger
```
