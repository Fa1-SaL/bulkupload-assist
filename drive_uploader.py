import os
import json
import base64
import concurrent.futures
import hashlib
import hmac
import mimetypes
import time
import random
import re
import secrets
import traceback
import threading
from dataclasses import dataclass

import requests

DRIVE_FOLDER_ID = '1EhV9pbEZjZg2_D7Tg2Z-zlsFK8DY06Ne'  # fixed owner folder
# The owner distributes upload_endpoint.txt with the app; the public repo omits it.
# Each person also needs a separate signed access code in native keyring.
UPLOAD_SERVICE_URL = ''
UPLOAD_ENDPOINT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'upload_endpoint.txt')
DRIVE_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "drive_cache.json")
HTTP_TIMEOUT_S = 300
UPLOAD_CHUNK_SIZE = 1024 * 1024
MAX_UPLOAD_BYTES = 24 * 1024 * 1024

def normalize_folder_id(value):
    value = str(value or '').strip()
    match = re.search(r'/folders/([\w-]+)',value)
    if match: return match.group(1)
    if value and not re.fullmatch(r'[\w-]+',value):
        raise ValueError('Enter a Drive folder ID or a folder link.')
    return value


class UploadCancelled(Exception):
    """Raised when an upload is stopped by its should_cancel callback."""


class UploadTransientError(Exception):
    """The owner script asked the client to retry this request."""

def hosted_cache_key(file_hash: str) -> str:
    """Old directly uploaded, user-owned Drive links cannot count as service uploads."""
    return f'owner-script-v1:{DRIVE_FOLDER_ID}:{file_hash}'


def configured_upload_url() -> str:
    if UPLOAD_SERVICE_URL:
        return UPLOAD_SERVICE_URL.strip()
    try:
        with open(UPLOAD_ENDPOINT_FILE, 'r', encoding='utf-8') as file:
            return file.read().strip()
    except FileNotFoundError:
        return ''


def get_drive_credentials(force_reauth: bool = False) -> str:
    """Read this user's upload access code from native keyring; never use Google OAuth."""
    if not configured_upload_url():
        raise RuntimeError('Owner upload script is not configured. Contact the app owner.')
    from app_preferences import SecureApiKeyStore
    code = SecureApiKeyStore(service_name='BulkUploadAssist', username='upload_access_code').get()
    if not code:
        raise RuntimeError('Add your upload access code in Settings before processing resumes.')
    return code


@dataclass
class OwnerScriptService:
    url: str
    access_code: str
    session: requests.Session


def parse_access_code(value: str) -> tuple[str, str]:
    user, separator, key = str(value or '').strip().partition(':')
    if not separator or not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', user) or not re.fullmatch(r'[0-9a-f]{64}', key):
        raise ValueError('Enter a valid upload access code from the app owner.')
    return user, key


def get_drive_service(force_reauth: bool = False, creds=None):
    """Build a separate HTTP session per worker for the fixed owner script."""
    url = configured_upload_url()
    if not re.fullmatch(r'https://script\.google\.com/macros/s/[\w-]+/exec', url):
        raise RuntimeError('Owner upload script is not configured. Contact the app owner.')
    access_code = creds or get_drive_credentials(force_reauth)
    parse_access_code(access_code)
    return OwnerScriptService(url, access_code, requests.Session())


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
    valid_key = re.compile(r'^owner-script-v1:' + re.escape(DRIVE_FOLDER_ID) + r':[a-fA-F0-9]{32}$')

    for key, value in cache.items():
        if not isinstance(value, str) or not value.startswith("http"):
            continue  # Discard invalid values

        if valid_key.fullmatch(key):
            validated_cache[key] = value

    return validated_cache


def _signed_payload(service: OwnerScriptService, action: str, **fields) -> dict:
    user, key = parse_access_code(service.access_code)
    payload = {
        'action': action, 'timestamp': str(int(time.time())),
        'nonce': secrets.token_urlsafe(18), 'user': user, **fields,
    }
    names = ('action', 'timestamp', 'nonce', 'user') + (
        ('name', 'mime', 'data') if action == 'upload' else ())
    canonical = '\n'.join(str(payload[name]) for name in names)
    payload['signature'] = hmac.new(key.encode('ascii'), canonical.encode('utf-8'), hashlib.sha256).hexdigest()
    return payload


def _script_response(response) -> dict:
    if response.status_code != 200:
        raise requests.HTTPError(f'Owner upload script returned HTTP {response.status_code}', response=response)
    try:
        result = response.json()
    except ValueError as exc:
        raise UploadTransientError('Owner upload script returned an unreadable response.') from exc
    if not isinstance(result, dict):
        raise UploadTransientError('Owner upload script returned an invalid response.')
    if result.get('status') == 'error':
        code = result.get('code')
        if code == 'unauthorized':
            raise RuntimeError('Upload access code was rejected. Enter a valid code in Settings.')
        if code == 'invalid_size':
            raise ValueError('Resume exceeds the owner upload size limit (24 MiB).')
        if code in ('busy', 'upload_failed'):
            raise UploadTransientError('Owner upload is temporarily unavailable.')
        raise RuntimeError('Owner upload script rejected the request.')
    return result


def check_upload_access(service: OwnerScriptService) -> bool:
    response = service.session.post(service.url,
        json=_signed_payload(service, 'check'), timeout=(10, 20))
    return _script_response(response).get('status') == 'ready'


def _upload_single_file_internal(service, file_path: str, upload_name: str = None, parent_id: str = DRIVE_FOLDER_ID,
                                 should_cancel=None) -> str:
    """Send one bounded, signed resume to the owner-run Apps Script."""
    filename = upload_name or os.path.basename(file_path)
    if parent_id != DRIVE_FOLDER_ID:
        raise ValueError('The upload destination is fixed by the app owner.')
    if not re.search(r'\.(pdf|docx|doc|txt)$', filename, re.IGNORECASE):
        raise ValueError('Unsupported resume format.')
    chunks = []
    size = 0
    with open(file_path, 'rb') as source:
        while True:
            if should_cancel is not None and should_cancel():
                raise UploadCancelled(filename)
            chunk = source.read(UPLOAD_CHUNK_SIZE)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                raise ValueError('Resume exceeds the owner upload size limit (24 MiB).')
            chunks.append(chunk)
    if not chunks:
        raise ValueError('Resume file is empty.')
    if should_cancel is not None and should_cancel():
        raise UploadCancelled(filename)
    mime = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
    payload = _signed_payload(service, 'upload', name=filename, mime=mime,
        data=base64.b64encode(b''.join(chunks)).decode('ascii'))
    response = service.session.post(service.url, json=payload, timeout=(10, HTTP_TIMEOUT_S))
    result = _script_response(response)
    link = result.get('url') if result.get('status') == 'ok' else None
    if not isinstance(link, str) or not re.fullmatch(r'https://drive\.google\.com/file/d/[A-Za-z0-9_-]+/view', link):
        raise UploadTransientError('Owner upload script returned an invalid Drive link.')
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
            # 4. Owner script transient responses
            elif isinstance(e, UploadTransientError):
                is_transient = True
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
