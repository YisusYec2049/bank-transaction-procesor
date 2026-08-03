# Cómo trabajar en este repo

Este pipeline **mueve plata**: decide qué pago cubre qué cuota de qué persona.
Un error acá no tira una página, deja una deuda mal cobrada que alguien descubre
semanas después cuadrando a mano. Casi todo lo que sigue existe porque eso ya
pasó.

## Antes de escribir código

Leer [`docs/decisiones/`](docs/decisiones/README.md). Son ocho páginas cortas con
las reglas que sostienen el sistema y por qué son así. Varias parecen arbitrarias
hasta que se sabe qué las causó.

## Preparar el entorno

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pytest        # tiene que pasar sin .env y sin red
```

Hacen falta un `.env` y un `service_account.json` para correr el pipeline contra
datos reales; **las pruebas no los necesitan**, y si alguna empezara a
necesitarlos, dejó de ser una prueba.

## El ciclo

1. **`pytest` en verde antes de empezar.** Si ya estaba rojo, eso es lo primero.
2. Escribir el cambio.
3. **`pytest` otra vez.** Si un snapshot se movió, mirar el diff de
   `tests/snapshots/` — ahí se ve en números qué cambió. No bendecirlo con
   `ACTUALIZAR_SNAPSHOTS=1` sin entender cada línea.
4. **Verificar contra producción sin escribir nada:**

   ```bash
   python cruzar.py --dry-run --dry-run-salida /tmp/antes.jsonl
   # aplicar el cambio
   python cruzar.py --dry-run --dry-run-salida /tmp/despues.jsonl
   diff /tmp/antes.jsonl /tmp/despues.jsonl
   ```

   Para un cambio de rendimiento o una limpieza, ese diff **tiene que salir
   vacío**. Si no, no era una limpieza.
5. `ruff check .`
6. Commit. El CI corre todo de nuevo en tres versiones de Python.

## Escribir pruebas

Van en `tests/`, corren sin red y sin credenciales. El patrón es correr el
`main()` **real** con las puertas al exterior sustituidas — está montado en
`tests/conftest.py` (fixture `mundo`), no hace falta inventarlo.

**Rompé lo que la prueba debería proteger y confirmá que falla.** Una prueba que
nunca se vio fallar puede estar mirando a otro lado; ya pasó tres veces acá (ver
[decisión 007](docs/decisiones/007-pruebas-de-caracterizacion.md)).

Si tocás datos de prueba, regeneralos con
`python scripts/generar_fixtures.py --origen <carpeta>`. **Los archivos reales no
entran al repo** — el repo es público y esos archivos traen cédulas, nombres y
correos.

## Migraciones SQL

Se corren a mano en el editor de Supabase, no hay herramienta de migraciones.
**Escribí el código para que funcione con y sin la migración corrida**: si algo
nuevo en la base todavía no existe, el pipeline debe seguir por el camino
anterior y avisarlo en el log.

No es un capricho — desplegar código que exigía SQL ya corrido dejó el sistema
sin escribir durante un día entero.

## Cosas que parecen buena idea y no lo son

- **Traer menos filas para ir más rápido.** Los índices de ambigüedad se
  construyen sobre lo que se leyó; un universo más chico puede hacer que el
  sistema decida donde antes se abstenía. Ver
  [decisión 006](docs/decisiones/006-recortar-una-lectura-no-es-neutral.md).
- **Cerrar solo lo que nadie puede resolver hoy.** "Hoy no se puede" no es "nunca
  se va a poder". Ver
  [decisión 004](docs/decisiones/004-el-pipeline-no-cierra-solo.md).
- **Usar upsert para actualizar dos columnas.** Ver
  [decisión 002](docs/decisiones/002-nunca-upsert-parcial.md).
- **Reintentar un POST que falló.** Si llegó y se perdió la respuesta,
  reintentarlo escribe dos veces. Los reintentos automáticos están acotados a
  métodos idempotentes a propósito.

## Desplegar

Ver [`deploy/README.md`](deploy/README.md). Lo que más se olvida:
`systemctl restart cruce-trigger` — sin eso, los reprocesos que dispara la
plataforma siguen usando el código anterior.
