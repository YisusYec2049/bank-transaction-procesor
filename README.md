# Cruce de Cartera — Bancolombia 2576

Script Python que replica la lógica de negocio del flujo n8n de Bancolombia 2576:
lee PDFs desde Google Drive, los normaliza al esquema estándar, deduplica,
escribe al spreadsheet CONSOLIDADO y hace upsert a Supabase.

---

## Requisitos previos

- Python 3.11+
- Ubuntu 24.04 (o cualquier Linux moderno)
- Acceso a Google Cloud Console para crear la Service Account

---

## 1. Crear la Service Account en Google Cloud

1. Ir a **IAM & Admin → Service Accounts** en la consola de tu proyecto GCP.
2. Crear una cuenta nueva (p. ej. `matching-bot@mi-proyecto.iam.gserviceaccount.com`).
3. Sin roles de proyecto (los permisos se otorgan a nivel de archivo/carpeta).
4. En la pestaña **Keys**, crear una clave de tipo **JSON** y descargarla.
5. Guardarla en el servidor: `cp descargado.json /opt/matching/service_account.json`.
6. `chmod 600 /opt/matching/service_account.json`

---

## 2. Compartir los recursos de Google Drive / Sheets con la Service Account

El email de la SA tiene la forma `nombre@proyecto.iam.gserviceaccount.com`.

| Recurso | Permiso necesario |
|---|---|
| Carpeta INBOX (PDFs entrantes) | **Editor** |
| Carpeta HISTÓRICO (destino de PDFs procesados) | **Editor** |
| Spreadsheet CONSOLIDADO | **Editor** |
| Spreadsheet CHEQUES\_PENDIENTES | **Editor** |

En cada uno: clic derecho → Compartir → pegar el email de la SA → Editor.

---

## 3. Instalación en el servidor

```bash
# Clonar o copiar los archivos al servidor
cd /opt/matching

# Crear entorno virtual
python3 -m venv venv
source venv/bin/activate

# Instalar dependencias
pip install -r requirements.txt
```

---

## 4. Configuración del .env

```bash
cp .env.example .env
nano .env   # Completar todos los valores con IDs de PRUEBA
```

Los IDs de carpetas y hojas se obtienen de la URL de cada recurso en Google Drive/Sheets:
- Carpeta: `https://drive.google.com/drive/folders/<FOLDER_ID>`
- Hoja: `https://docs.google.com/spreadsheets/d/<SHEET_ID>`

> **IMPORTANTE:** este servidor es de PRUEBA. Los IDs en `.env` deben apuntar
> a recursos de prueba, nunca a las carpetas/hojas de producción.

---

## 5. Ejecución manual

### Crear la tab del día (ejecutar una vez al día antes de procesar)
```bash
source venv/bin/activate
python crear_hoja.py
```

### Procesar archivos nuevos
```bash
python procesar.py
```

### Simular sin escribir nada (útil para validar un PDF nuevo)
```bash
python procesar.py --dry-run
```

---

## 6. Configuración de cron

Editar el crontab del usuario que corre el script:

```bash
crontab -e
```

Añadir las siguientes líneas:

```cron
TZ=America/Bogota

# Crear tab del día a las 00:01
1 0 * * * cd /opt/matching && /opt/matching/venv/bin/python crear_hoja.py >> /var/log/matching/crear_hoja.log 2>&1

# Procesar PDFs cada 5 minutos
*/5 * * * * cd /opt/matching && /opt/matching/venv/bin/python procesar.py >> /var/log/matching/procesar.log 2>&1
```

Crear el directorio de logs:
```bash
sudo mkdir -p /var/log/matching
sudo chown $USER /var/log/matching
```

---

## 7. Estructura de archivos

```
/opt/matching/
├── service_account.json   # Credenciales SA (NO subir a git)
├── .env                   # Variables de entorno (NO subir a git)
├── .env.example           # Plantilla sin valores
├── requirements.txt
├── crear_hoja.py          # Entry point: crea tab del día
├── procesar.py            # Entry point: procesa PDFs
└── bancolombia_2576.py    # Lógica de negocio Bancolombia 2576
```

---

## 8. Notas técnicas

### Formato de matching_key (Bancolombia 2576)
```
{DD/MM/YYYY}_{referencia_1_sin_ceros}_{valor}
```
Ejemplo: `15/06/2025_1234567_500000`

El valor se formatea igual que JavaScript (`String(parseFloat(n.toFixed(2)))`):
`500000.00` → `"500000"`, `435.50` → `"435.5"`.

### Lógica de cheques
Los movimientos con `CHEQUE` en la descripción siguen un flujo separado:
- **Nuevo**: se agrega a CHEQUES\_PENDIENTES con estado `PENDIENTE`.
- **Aparece de nuevo**: si hay un `PENDIENTE` con mismo `identification|valor`,
  se marca `CONCILIADO` y el cheque pasa al consolidado.
- **Ya conciliado**: se ignora con un warning en el log.

### Deduplicación
Antes de escribir al CONSOLIDADO se consulta la tab del día anterior
y se excluyen las `matching_key` ya presentes. Adicionalmente, el upsert
a Supabase usa `on_conflict=matching_key` con `resolution=merge-duplicates`
como segunda capa de protección.
