# Despliegue

El pipeline vive en `/opt/matching-test` en un VPS. Dos cosas lo ejecutan: el
**cron** (la corrida diaria) y el **servicio HTTP** (`cruce-trigger`, los
reprocesos que dispara la plataforma).

---

## Sacar el pipeline de root

Hasta ahora, las dos cosas corrían como **root**. El servicio HTTP está expuesto
a internet por Tailscale Funnel y lanza subprocesos, así que un fallo suyo era
un fallo con permisos totales sobre la máquina. Nada de lo que hace necesita
root: lee el repo, escribe en `logs/` y habla con Supabase y Drive.

Los pasos de abajo se corren **una sola vez**, como root, en el VPS.

### 1. Crear el usuario

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin matching
```

`--system` (sin login) y `--shell nologin`: nadie puede entrar con esa cuenta.

### 2. Cederle el repo

```bash
sudo chown -R matching:matching /opt/matching-test
sudo chmod 750 /opt/matching-test

# Los secretos, solo para su dueño.
sudo chmod 600 /opt/matching-test/.env /opt/matching-test/service_account.json

# Sin esto, el próximo `git pull` como root falla con "detected dubious
# ownership in repository": git se niega a operar sobre un repo que es de
# otro usuario. Es la consecuencia directa del chown de arriba.
sudo git config --global --add safe.directory /opt/matching-test
```

### 3. Instalar la unidad nueva

```bash
sudo cp /opt/matching-test/deploy/cruce-trigger.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart cruce-trigger
sudo systemctl status cruce-trigger        # debe decir "active (running)"
```

Comprobar que de verdad ya no es root:

```bash
ps -o user= -p "$(systemctl show -p MainPID --value cruce-trigger)"
# tiene que imprimir: matching
```

### 4. Mover el cron al mismo usuario — **no es opcional**

Si el servicio corre como `matching` pero el cron sigue como `root`, los
archivos que crea el cron en `logs/` quedan con dueño root y **el servicio deja
de poder escribir ahí**. Los dos tienen que ser el mismo usuario.

```bash
# Respaldo primero: `crontab <archivo>` REEMPLAZA el crontab entero.
sudo crontab -l > ~/crontab-root-respaldo-$(date +%F).txt

# Pasar las líneas del pipeline al crontab de matching...
sudo crontab -l -u root | grep matching-test | sudo crontab -u matching -
# ...y quitarlas del de root.
sudo crontab -l -u root | grep -v matching-test | sudo crontab -u root -

# Verificar que quedaron donde deben
sudo crontab -l -u matching
sudo crontab -l -u root
```

**Ojo con `TZ=America/Bogota`**: si estaba en el crontab de root, hay que
volver a ponerlo arriba del de `matching`, porque el `grep` no lo trae. Sin él
los logs quedan en UTC. (El horario del cron va en UTC de todos modos — ver el
README principal.)

### 5. Arreglar los logs que quedaron de root

```bash
sudo chown -R matching:matching /opt/matching-test/logs
```

### Si algo sale mal

Volver atrás es cambiar `User=matching` por `User=root` en
`/etc/systemd/system/cruce-trigger.service`, `daemon-reload` y `restart`. Los
archivos con dueño `matching` los sigue pudiendo leer y escribir root.

---

## Qué hace el aislamiento de la unidad

Además del usuario, la unidad le pide a systemd que encierre al servicio.
Todo esto se aplica sin tocar el código:

| Directiva | Qué impide |
|---|---|
| `NoNewPrivileges` | que un subproceso gane permisos (setuid) |
| `ProtectSystem=strict` | escribir en cualquier parte del disco |
| `ReadWritePaths` | …salvo `logs/`, que es lo único que necesita |
| `ProtectHome` | leer `/home` |
| `PrivateTmp` | ver o pisar los `/tmp` de otros procesos |
| `ProtectKernel*`, `RestrictNamespaces` | tocar el kernel o crear contenedores |

**Detalle a tener presente**: `PrivateTmp=true` le da al servicio su propio
`/tmp`. Hoy no molesta —el candado `flock -n /tmp/matching.lock` lo usa solo el
cron, y el servicio se coordina con un lock propio en memoria— pero si alguna
vez se quiere que los dos compartan el mismo candado, hay que sacar esa
directiva o mover el candado fuera de `/tmp`.

---

## Actualizar el código

```bash
cd /opt/matching-test && sudo git pull
sudo systemctl restart cruce-trigger

# El pull deja los archivos nuevos con dueño root; devolvérselos al servicio.
sudo chown -R matching:matching /opt/matching-test
```

**El restart no es opcional**: `gunicorn` tiene el código cargado en memoria, así
que sin reiniciarlo los reprocesos que dispara la plataforma seguirían usando la
versión anterior. El cron sí toma el código nuevo solo, porque arranca un
proceso nuevo en cada corrida.

---

## Migraciones SQL

Se corren a mano en el editor de Supabase. El código está escrito para **no
depender del orden**: si una función de base todavía no existe, el pipeline sigue
por el camino anterior y lo avisa en el log. Desplegar antes de correr el SQL no
rompe nada.
