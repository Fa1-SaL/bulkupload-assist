import os
import json
import concurrent.futures
import time
import random
import re
import traceback
import threading
from dataclasses import dataclass
from urllib.parse import quote

import requests

DRIVE_FOLDER_ID = '1EhV9pbEZjZg2_D7Tg2Z-zlsFK8DY06Ne'  # fixed owner folder
# Set to the verified Cloud Run URL after deployment. All distributed clients
# use this one endpoint; there is no per-user destination override.
UPLOAD_SERVICE_URL = ''
DRIVE_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "drive_cache.json")
HTTP_TIMEOUT_S = 300        # the hosted service must receive and upload the file before responding
UPLOAD_CHUNK_SIZE = 1024 * 1024

def normalize_folder_id(value):
    value = str(value or '').strip()
    match = re.search(r'/folders/([\w-]+)',value)
    if match: return match.group(1)
    if value and not re.fullmatch(r'[\w-]+',value):
        raise ValueError('Enter a Drive folder ID or a folder link.')
    return value


class UploadCancelled(Exception):
    """Raised when an upload is stopped by its should_cancel callback."""

def hosted_cache_key(file_hash: str) -> str:
    """Old directly uploaded, user-owned Drive links cannot count as service uploads."""
    return f'owner-service-v1:{DRIVE_FOLDER_ID}:{file_hash}'


def get_drive_credentials(force_reauth: bool = False) -> str:
    """Read this user's upload access code from native keyring; never use Google OAuth."""
    if not UPLOAD_SERVICE_URL:
        raise RuntimeError('Upload service is not configured. Contact the app owner.')
    from app_preferences import SecureApiKeyStore
    code = SecureApiKeyStore(service_name='BulkUploadAssist', username='upload_access_code').get()
    if not code:
        raise RuntimeError('Add your upload access code in Settings before processing resumes.')
    return code


@dataclass
class HostedDriveService:
    url: str
    access_code: str
    session: requests.Session


def get_drive_service(force_reauth: bool = False, creds=None):
    """Build a separate HTTP session per worker, sharing only the access-code string."""
    if not UPLOAD_SERVICE_URL.startswith('https://'):
        raise RuntimeError('Upload service must use HTTPS and be configured by the app owner.')
    return HostedDriveService(UPLOAD_SERVICE_URL, creds or get_drive_credentials(force_reauth), requests.Session())


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
    """Keep only owner-service links; old per-user uploads must be sent again."""
    if not isinstance(cache, dict):
        print("[-] Corrupted cache structure detected. Initializing empty cache.")
        return {}

    validated_cache = {}
    valid_key = re.compile(r'^owner-service-v1:' + re.escape(DRIVE_FOLDER_ID) + r':[a-fA-F0-9]{32}$')

    for key, value in cache.items():
        if not isinstance(value, str) or not value.startswith("http"):
            continue  # Discard invalid values

        if valid_key.fullmatch(key):
            validated_cache[key] = value

    return validated_cache


def _upload_single_file_internal(service, file_path: str, upload_name: str = None, parent_id: str = DRIVE_FOLDER_ID,
                                 should_cancel=None) -> str:
    """Stream a resume to the owner service, checking cancellation between chunks."""
    filename = upload_name or os.path.basename(file_path)
    if parent_id != DRIVE_FOLDER_ID:
        raise ValueError('The upload destination is fixed by the app owner.')

    def chunks():
        with open(file_path, 'rb') as source:
            while True:
                if should_cancel is not None and should_cancel():
                    raise UploadCancelled(filename)
                chunk = source.read(UPLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                yield chunk

    response = service.session.post(
        service.url + '/v1/uploads', data=chunks(),
        headers={'Authorization': 'Bearer ' + service.access_code,
                 'X-Upload-Filename': quote(filename, safe=''),
                 'Content-Type': 'application/octet-stream'},
        timeout=(10, HTTP_TIMEOUT_S))
    if response.status_code == 401:
        raise RuntimeError('Upload access code was rejected. Enter a valid code in Settings.')
    if response.status_code == 413:
        raise ValueError('Resume exceeds the upload service size limit (24 MiB).')
    if response.status_code != 200:
        raise requests.HTTPError(f'Upload service returned HTTP {response.status_code}', response=response)
    try:
        link = response.json()['url']
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError('Upload service returned an invalid response.') from exc
    if not isinstance(link, str) or not link.startswith('https://drive.google.com/file/d/'):
        raise RuntimeError('Upload service returned an invalid Drive link.')
    return link


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
    if parent_id and normalize_folder_id(parent_id) != DRIVE_FOLDER_ID:
        raise ValueError('The upload destination is fixed by the app owner.')

    for attempt in range(1, max_retries + 2):
        try:
            return _upload_single_file_internal(service, file_path, upload_name, DRIVE_FOLDER_ID, should_cancel)

        except UploadCancelled:
            raise

        except Exception as e:
            err_msg = str(e).lower()

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
            # 4. Hosted service transient responses
            elif isinstance(e, requests.HTTPError) and e.response is not None:
                status_code = e.response.status_code
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
            file_hash = hosted_cache_key(get_file_hash(full_path))
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
