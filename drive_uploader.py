from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError
import google_auth_httplib2
import httplib2
import os
import pickle
import json
import concurrent.futures
import time
import random
import re
import traceback
import threading
from typing import Optional

SCOPES = ['https://www.googleapis.com/auth/drive.file']
DRIVE_FOLDER_ID = '1EhV9pbEZjZg2_D7Tg2Z-zlsFK8DY06Ne'  # personal Drive folder ID
TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'token.pickle')
CREDENTIALS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'oauth_credentials.json')
DRIVE_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "drive_cache.json")
HTTP_TIMEOUT_S = 60         # per-request socket timeout: a stalled connection raises (and is retried) instead of hanging
OAUTH_TIMEOUT_S = 300       # give up on the browser sign-in after this long instead of waiting forever
UPLOAD_CHUNK_SIZE = 1024 * 1024  # resumable chunk size (multiple of 256 KiB); cancel is checked between chunks

# Retained for compatibility with older callers; uploads never silently redirect.
_use_root_parent = False

def normalize_folder_id(value):
    value = str(value or '').strip()
    match = re.search(r'/folders/([\w-]+)',value)
    if match: return match.group(1)
    if value and not re.fullmatch(r'[\w-]+',value):
        raise ValueError('Enter a Drive folder ID or a folder link.')
    return value


class UploadCancelled(Exception):
    """Raised when an upload is stopped by its should_cancel callback."""

def get_drive_credentials(force_reauth: bool = False) -> Credentials:
    """Load the saved OAuth token, refreshing it or running the browser flow as needed."""
    creds = None

    # Load saved token if exists and we are not forcing re-authentication
    if not force_reauth and os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, 'rb') as f:
                creds = pickle.load(f)
        except Exception:
            pass

    # If no valid token or forcing re-auth, do the OAuth login
    if force_reauth or not creds or not creds.valid:
        if not force_reauth and creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                print(f"Token refresh failed: {e}. Re-authenticating...")
                return get_drive_credentials(force_reauth=True)
        else:
            if os.path.exists(TOKEN_FILE):
                try:
                    os.remove(TOKEN_FILE)
                except Exception:
                    pass
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0, timeout_seconds=OAUTH_TIMEOUT_S)
        
        with open(TOKEN_FILE, 'wb') as f:
            pickle.dump(creds, f)

    return creds


def get_drive_service(force_reauth: bool = False, creds=None):
    """Build a Drive client. The underlying httplib2 transport is not thread-safe:
    build one service per thread, sharing a single Credentials object via `creds`.
    Requests time out after HTTP_TIMEOUT_S so a stalled connection cannot hang a worker."""
    http = google_auth_httplib2.AuthorizedHttp(
        creds or get_drive_credentials(force_reauth), http=httplib2.Http(timeout=HTTP_TIMEOUT_S))
    return build('drive', 'v3', http=http, cache_discovery=False)


def get_file_hash(filepath: str) -> str:
    """Compute MD5 hash of the file bytes."""
    import hashlib
    hasher = hashlib.md5()
    try:
        with open(filepath, 'rb') as f:
            for chunk in iter(lambda: f.read(65536), b''):
                hasher.update(chunk)
        return hasher.hexdigest()
    except Exception:
        # Fallback to hashing the path string if file cannot be read
        return hashlib.md5(filepath.encode('utf-8')).hexdigest()


def is_ignored_file(filename: str) -> bool:
    """Determine if a file should be ignored to prevent duplicate or temporary uploads."""
    fn_lower = filename.lower()
    # Ignore temporary Word files
    if fn_lower.startswith('~$'):
        return True
    # Ignore historical conversion temp artifacts
    if '__upload_temp.pdf' in fn_lower:
        return True
    if '_converted.pdf' in fn_lower:
        return True
    # Ignore cache files
    if fn_lower in ('drive_cache.json', 'resume_cache.json', 'jd_cache.json'):
        return True
    return False


def log_upload_error(filename: str, exception: Exception, attempt: int):
    """Print detailed structured logs for upload failures."""
    exc_type = type(exception).__name__
    print(f"[-] Error uploading {filename} (Attempt {attempt}):")
    print(f"    Exception Type: {exc_type}")
    print(f"    Full Error:     {exception}")
    traceback.print_exc()


def atomic_write_json(path: str, data) -> None:
    """Write compact JSON to path + '.tmp', fsync it, then atomically replace path.

    On any failure the tmp file is removed and the exception re-raised, leaving
    the original file untouched.
    """
    tmp_path = path + ".tmp"
    try:
        payload = json.dumps(data, separators=(",", ":"))  # serialise fully before touching disk
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())  # data on disk before the rename makes it visible
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def migrate_and_validate_cache(cache: dict, folder_path: str) -> dict:
    """Migrate legacy filename-keyed cache entries to hash-keyed entries and remove invalid schemas."""
    if not isinstance(cache, dict):
        print("[-] Corrupted cache structure detected. Initializing empty cache.")
        return {}

    validated_cache = {}
    is_md5 = re.compile(r'^[a-fA-F0-9]{32}$')

    for key, value in cache.items():
        if not isinstance(value, str) or not value.startswith("http"):
            continue  # Discard invalid values

        if is_md5.match(key):
            # Already in correct hash -> link format
            validated_cache[key] = value
        else:
            # Legacy format: filename -> link
            full_path = os.path.join(folder_path, key)
            if os.path.exists(full_path) and os.path.isfile(full_path):
                try:
                    file_hash = get_file_hash(full_path)
                    validated_cache[file_hash] = value
                    print(f"[Cache Migration] Migrated cache entry for '{key}' to hash '{file_hash}'")
                except Exception as e:
                    print(f"[-] Failed to migrate cache entry for '{key}': {e}")
            else:
                print(f"[Cache Migration] Discarded legacy cache entry for '{key}' (file not found on disk)")

    return validated_cache


def _upload_single_file_internal(service, file_path: str, upload_name: str = None, parent_id: str = DRIVE_FOLDER_ID,
                                 should_cancel=None) -> str:
    """Helper that performs the actual API upload directly to Google Drive.
    The file is sent in UPLOAD_CHUNK_SIZE chunks; should_cancel() is checked before
    each one and raises UploadCancelled (nothing is created on Drive until the last chunk)."""
    filename = upload_name or os.path.basename(file_path)
    
    # Determine MIME type based on extension
    if filename.lower().endswith('.docx'):
        mime = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    elif filename.lower().endswith('.pdf'):
        mime = 'application/pdf'
    else:
        mime = 'application/octet-stream'

    file_metadata = {'name': filename, 'parents': [parent_id]}
    media = MediaFileUpload(file_path, mimetype=mime, chunksize=UPLOAD_CHUNK_SIZE, resumable=True)

    request = service.files().create(
        body=file_metadata,
        media_body=media,
        fields='id'
    )
    uploaded = None
    while uploaded is None:
        if should_cancel is not None and should_cancel():
            raise UploadCancelled(filename)
        _status, uploaded = request.next_chunk()

    if isinstance(uploaded, dict):
        file_id = uploaded.get('id')
    else:
        file_id = str(uploaded)

    if not file_id:
        raise ValueError(f"Drive API did not return a valid file ID for {filename}")

    # Set reader permissions for link sharing
    service.permissions().create(
        fileId=file_id,
        body={'type': 'anyone', 'role': 'reader'}
    ).execute()

    return f"https://drive.google.com/file/d/{file_id}/view"


def _upload_single_file(service, file_path: str, upload_name: str = None, should_cancel=None, parent_id=None) -> str:
    """Upload a file with transient failure retries (exponential backoff).

    Callers must pass a service owned by the calling thread (see get_drive_service).
    should_cancel: optional callable; when it returns True the upload stops with
    UploadCancelled (checked between chunks and before each retry)."""
    import ssl
    import socket

    filename = upload_name or os.path.basename(file_path)
    max_retries = 3
    delay = 1.0

    for attempt in range(1, max_retries + 2):
        try:
            destination = normalize_folder_id(parent_id) if parent_id else DRIVE_FOLDER_ID
            return _upload_single_file_internal(service, file_path, upload_name, destination, should_cancel)

        except UploadCancelled:
            raise

        except Exception as e:
            err_msg = str(e).lower()

            if 'insufficientparentpermissions' in err_msg or 'insufficient permissions for the specified parent' in err_msg:
                raise RuntimeError('Drive folder does not allow uploads. Change the folder or account in Settings, then retry.') from e

            # Determine if exception is a retryable transient failure
            is_transient = False
            # 1. SSL failures
            if any(term in err_msg for term in ("ssl", "decryption", "bad_record_mac", "wrong_version_number")):
                is_transient = True
            # 2. Timeouts
            elif any(term in err_msg for term in ("timeout", "timed out")) or isinstance(e, socket.timeout):
                is_transient = True
            # 3. Connection Resets / Abrupt closures
            elif any(term in err_msg for term in ("connection reset", "connection closed", "remote end closed")):
                is_transient = True
            # 4. Google API Transient Codes (429, 5xx)
            elif isinstance(e, HttpError):
                status_code = e.resp.status
                if status_code in (429, 500, 502, 503, 504):
                    is_transient = True

            if is_transient and attempt <= max_retries:
                log_upload_error(filename, e, attempt)
                sleep_time = delay + random.uniform(0.0, 0.5)
                print(f"[*] Retrying upload for {filename} in {sleep_time:.2f}s...")
                time.sleep(sleep_time)
                if should_cancel is not None and should_cancel():
                    raise UploadCancelled(filename)
                delay *= 2.0
            else:
                log_upload_error(filename, e, attempt)
                raise e


def upload_resume(file_path: str, upload_name: str = None) -> str:
    """Backward-compatible wrapper for uploading a single file."""
    try:
        service = get_drive_service(force_reauth=False)
        return _upload_single_file(service, file_path, upload_name)
    except Exception as e:
        print(f"[-] Single resume upload failed: {e}")
        return ""


def upload_all_resumes(folder_path: str, filenames_to_upload: list = None, max_workers: int = 2) -> dict:
    """Scan directory, apply migration, and parallel upload resumes to Google Drive with performance metrics."""
    # 1. Gracefully read and validate cache
    cache = {}
    if os.path.exists(DRIVE_CACHE_FILE):
        try:
            with open(DRIVE_CACHE_FILE, "r", encoding="utf-8") as f:
                cache = json.load(f)
        except Exception as e:
            print(f"[-] Failed to read cache: {e}. Starting with empty cache.")
            cache = {}

    cache = migrate_and_validate_cache(cache, folder_path)

    # 2. Discover supported files, filtering out ignored formats
    supported = ('.pdf', '.docx')
    try:
        all_files = os.listdir(folder_path)
    except Exception as e:
        print(f"[-] Folder access error {folder_path}: {e}")
        return {}

    resume_files = [
        f for f in all_files 
        if f.lower().endswith(supported) and not is_ignored_file(f)
    ]

    files_to_upload = []
    file_hashes = {}
    links = {}

    metrics = {
        'total_files': len(resume_files),
        'uploaded_files': 0,
        'cache_hits': 0,
        'failed_uploads': 0,
        'active_uploads': 0,
        'total_active_duration': 0.0
    }

    start_time = time.time()

    for filename in resume_files:
        # If filtered by list, restrict processing
        if filenames_to_upload is not None and filename not in filenames_to_upload:
            continue

        full_path = os.path.join(folder_path, filename)
        try:
            file_hash = get_file_hash(full_path)
            file_hashes[filename] = file_hash
        except Exception as e:
            print(f"[-] Hashing failure for {filename}: {e}")
            metrics['failed_uploads'] += 1
            links[filename] = ''
            continue

        # Check Cache
        if file_hash in cache:
            print(f"[*] Cache hit for {filename} (hash: {file_hash}) — skipping upload.")
            links[filename] = cache[file_hash]
            metrics['cache_hits'] += 1
        else:
            files_to_upload.append(filename)

    if files_to_upload:
        # Fetch credentials once; each worker thread builds its own service from them
        try:
            creds = get_drive_credentials(force_reauth=False)
        except Exception as e:
            print(f"[-] Authentication failed: {e}")
            creds = None

        if creds:
            metrics['active_uploads'] = len(files_to_upload)
            thread_state = threading.local()

            def thread_service():
                service = getattr(thread_state, "service", None)
                if service is None:
                    service = get_drive_service(creds=creds)
                    thread_state.service = service
                return service

            def upload_worker(filename):
                full_path = os.path.join(folder_path, filename)
                t_start = time.time()
                try:
                    print(f"Uploading {filename}...")
                    link = _upload_single_file(thread_service(), full_path)
                    duration = time.time() - t_start
                    return filename, link, duration, None
                except Exception as e:
                    duration = time.time() - t_start
                    return filename, '', duration, e

            # Concurrency limit applied via thread pool configuration
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_file = {executor.submit(upload_worker, f): f for f in files_to_upload}
                for future in concurrent.futures.as_completed(future_to_file):
                    filename = future_to_file[future]
                    try:
                        fname, link, duration, exc = future.result()
                        metrics['total_active_duration'] += duration
                        if exc is None and link:
                            links[fname] = link
                            metrics['uploaded_files'] += 1
                            # Cache the result under file hash
                            file_hash = file_hashes.get(fname)
                            if file_hash:
                                cache[file_hash] = link
                        else:
                            links[fname] = ''
                            metrics['failed_uploads'] += 1
                    except Exception as e:
                        links[filename] = ''
                        metrics['failed_uploads'] += 1
        else:
            for filename in files_to_upload:
                links[filename] = ''
                metrics['failed_uploads'] += 1

        # Save Cache
        try:
            atomic_write_json(DRIVE_CACHE_FILE, cache)
        except Exception as e:
            print(f"[-] Cache saving failed: {e}")

    # Generate Performance metrics
    total_duration = time.time() - start_time
    avg_upload_duration = (
        metrics['total_active_duration'] / metrics['uploaded_files'] 
        if metrics['uploaded_files'] > 0 else 0.0
    )

    print("\n" + "="*50)
    print("GOOGLE DRIVE UPLOAD PERFORMANCE REPORT")
    print("="*50)
    print(f"Total Files Checked:       {metrics['total_files']}")
    print(f"Cache Hits:                {metrics['cache_hits']}")
    print(f"Uploaded Files:            {metrics['uploaded_files']}")
    print(f"Failed Uploads:            {metrics['failed_uploads']}")
    if metrics['uploaded_files'] > 0:
        print(f"Avg Active Upload Time:    {avg_upload_duration:.2f}s")
    print(f"Total Upload Duration:     {total_duration:.2f}s")
    print("="*50 + "\n")

    return links
