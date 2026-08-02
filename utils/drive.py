"""Cliente Google Drive: listar, descargar y mover archivos."""

import io
import logging

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from utils import dry_run

log = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/drive']


def build_drive_service(sa_json_path: str):
    creds = service_account.Credentials.from_service_account_file(sa_json_path, scopes=SCOPES)
    return build('drive', 'v3', credentials=creds, cache_discovery=False)


def find_file_id(drive, folder_id: str, name: str) -> str | None:
    """Busca en la carpeta un archivo con ese nombre exacto (case-insensitive)."""
    wanted = name.strip().lower()
    for f in list_files(drive, folder_id):
        if f['name'].strip().lower() == wanted:
            return f['id']
    return None


def find_latest_any_file(drive, folder_id: str) -> str | None:
    """Devuelve el id del archivo más reciente (por createdTime) en la carpeta,
    SIN importar el nombre. Cada carpeta de cruce es dedicada a un solo tipo de
    archivo (Payu UC, Ingresos, Cartera Preventiva), así que CUALQUIER archivo
    que entre ahí ES ese tipo — no se exige un nombre concreto (decisión del
    usuario, 2026-07-21: "todo archivo que entre a la carpeta se lee como el
    tipo de esa carpeta"). list_files ya excluye subcarpetas y archivos nativos
    de Google, y ordena por createdTime, así que [-1] es el más reciente."""
    archivos = list_files(drive, folder_id)
    return archivos[-1]['id'] if archivos else None


def list_files(drive, folder_id: str) -> list[dict]:
    """Lista todos los archivos (no carpetas, no nativos de Google) en la
    carpeta, ordenados por createdTime ascendente (el último es el más
    reciente).

    Pagina hasta agotar la carpeta: la API devuelve como máximo 100 archivos
    por página, y sin paginar los que se perdían eran justamente los ÚLTIMOS
    de la lista — o sea los más nuevos, porque el orden es ascendente. El
    Inbox se vacía en cada corrida y no llega a ese tope, pero Histórico y la
    carpeta del ReportePagosWompi sí acumulan."""
    archivos, token = [], None
    while True:
        result = drive.files().list(
            q=(f"'{folder_id}' in parents"
               " and trashed=false"
               " and mimeType!='application/vnd.google-apps.folder'"
               " and not mimeType contains 'vnd.google-apps'"),
            fields='nextPageToken, files(id, name, mimeType)',
            orderBy='createdTime',
            pageSize=100,
            pageToken=token,
        ).execute()
        archivos += result.get('files', [])
        token = result.get('nextPageToken')
        if not token:
            return archivos


def find_all_files(drive, folder_id: str, contains: str) -> list[dict]:
    """Todos los archivos de la carpeta cuyo nombre contiene `contains`
    (case-insensitive), del más viejo al más reciente — el último es el más
    nuevo.

    Existe para el ReportePagosWompi: leer un solo archivo (el más reciente)
    hace que, cuando se suben varias entregas juntas —el lunes con el fin de
    semana, o el martes si el lunes fue festivo—, las demás no se lean NUNCA
    y sus pagos por link queden clasificados como manuales para siempre."""
    wanted = contains.strip().lower()
    return [f for f in list_files(drive, folder_id)
            if wanted in f['name'].strip().lower()]


def download_pdf(drive, file_id: str) -> io.BytesIO:
    request    = drive.files().get_media(fileId=file_id)
    buf        = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    buf.seek(0)
    return buf


def move_file(drive, file_id: str, dest_folder_id: str) -> None:
    # Mover un archivo a Histórico es una escritura, y de las que más duelen:
    # si la corrida no lo procesó bien, moverlo lo esconde. En simulación se
    # registra y el archivo se queda donde está.
    if dry_run.registrar(f'drive:{dest_folder_id}', 'move', [file_id]):
        return

    f            = drive.files().get(fileId=file_id, fields='parents').execute()
    prev_parents = ','.join(f.get('parents', []))
    drive.files().update(
        fileId=file_id,
        addParents=dest_folder_id,
        removeParents=prev_parents,
        fields='id,parents',
    ).execute()
    log.info('Archivo movido: %s → carpeta %s', file_id, dest_folder_id)
