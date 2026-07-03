"""Cliente Google Drive: listar, descargar y mover archivos."""

import io
import logging

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

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


def list_files(drive, folder_id: str) -> list[dict]:
    """Lista todos los archivos (no carpetas, no nativos de Google) en la carpeta."""
    result = drive.files().list(
        q=(f"'{folder_id}' in parents"
           " and trashed=false"
           " and mimeType!='application/vnd.google-apps.folder'"
           " and not mimeType contains 'vnd.google-apps'"),
        fields='files(id, name, mimeType)',
        orderBy='createdTime',
    ).execute()
    return result.get('files', [])


def list_pdfs(drive, folder_id: str) -> list[dict]:
    result = drive.files().list(
        q=(f"'{folder_id}' in parents"
           " and mimeType='application/pdf'"
           " and trashed=false"),
        fields='files(id, name)',
        orderBy='createdTime',
    ).execute()
    return result.get('files', [])


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
    f            = drive.files().get(fileId=file_id, fields='parents').execute()
    prev_parents = ','.join(f.get('parents', []))
    drive.files().update(
        fileId=file_id,
        addParents=dest_folder_id,
        removeParents=prev_parents,
        fields='id,parents',
    ).execute()
    log.info('Archivo movido: %s → carpeta %s', file_id, dest_folder_id)
