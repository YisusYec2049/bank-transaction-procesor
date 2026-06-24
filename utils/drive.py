"""Cliente Google Drive: listar, descargar y mover archivos."""

import io
import logging

from googleapiclient.http import MediaIoBaseDownload

log = logging.getLogger(__name__)


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
