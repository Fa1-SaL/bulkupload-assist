"""Authenticated Cloud Run boundary; only the owner-run script writes to Drive."""
from dataclasses import dataclass
import base64
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import tempfile
import time
from urllib.parse import unquote

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
import requests


MAX_UPLOAD_BYTES = 24 * 1024 * 1024  # below Cloud Run's HTTP/1 request limit
ALLOWED_RESUME_EXTENSIONS = {"pdf", "docx", "doc", "txt"}


@dataclass(frozen=True)
class ServiceConfig:
    bridge_url: str
    bridge_secret: str
    code_hashes: dict[str, str]
    max_bytes: int = MAX_UPLOAD_BYTES

    @classmethod
    def from_env(cls):
        bridge_url = os.environ.get("APPS_SCRIPT_URL", "").rstrip("\r\n")
        if not re.fullmatch(r"https://script\.google\.com/macros/s/[\w-]+/exec", bridge_url):
            raise ValueError("APPS_SCRIPT_URL must be an Apps Script deployment URL")
        bridge_secret = os.environ.get("BRIDGE_SHARED_SECRET", "").rstrip("\r\n")
        if len(bridge_secret) < 32:
            raise ValueError("BRIDGE_SHARED_SECRET must be a high-entropy secret")
        raw_hashes = os.environ.get("UPLOAD_ACCESS_CODE_HASHES", "")
        try:
            hashes = json.loads(raw_hashes)
            if not isinstance(hashes, dict) or not hashes or any(
                not isinstance(user, str) or not user or not isinstance(digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
                for user, digest in hashes.items()
            ):
                raise ValueError("Invalid code hashes")
        except (ValueError, TypeError) as exc:
            raise ValueError("UPLOAD_ACCESS_CODE_HASHES must map users to SHA-256 hashes") from exc
        return cls(bridge_url, bridge_secret, hashes)


def _authorized(header: str, code_hashes: dict[str, str]) -> bool:
    scheme, _, code = header.partition(" ")
    if scheme.lower() != "bearer" or not code or len(code) > 512 or any(c.isspace() for c in code):
        return False
    digest = hashlib.sha256(code.encode("utf-8")).hexdigest()
    # Compare every hash, without exposing or logging the supplied code.
    return bool(sum(secrets.compare_digest(digest, known) for known in code_hashes.values()))


def _filename(header: str) -> str:
    try:
        name = unquote(header, encoding="utf-8", errors="strict")
    except (UnicodeError, ValueError) as exc:
        raise HTTPException(400, "Invalid upload filename") from exc
    if (not name or name in (".", "..") or len(name) > 255
            or "/" in name or "\\" in name or any(ord(c) < 32 or ord(c) == 127 for c in name)):
        raise HTTPException(400, "Invalid upload filename")
    if "." not in name or name.rsplit(".", 1)[1].lower() not in ALLOWED_RESUME_EXTENSIONS:
        raise HTTPException(400, "Unsupported resume format")
    return name


def forward_to_bridge(path: str, filename: str, config: ServiceConfig) -> str:
    """Sign the bounded file and send it to the owner-run Apps Script web app."""
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    with open(path, "rb") as source:
        encoded = base64.b64encode(source.read(config.max_bytes + 1)).decode("ascii")
    if len(encoded) > ((config.max_bytes + 2) // 3) * 4:
        raise ValueError("Resume exceeds upload size limit")
    timestamp = str(int(time.time()))
    nonce = secrets.token_urlsafe(18)
    canonical = "\n".join((timestamp, nonce, filename, mime, encoded))
    signature = hmac.new(config.bridge_secret.encode("utf-8"),
                         canonical.encode("utf-8"), hashlib.sha256).hexdigest()
    response = requests.post(config.bridge_url, json={
        "timestamp": timestamp, "nonce": nonce, "name": filename,
        "mime": mime, "data": encoded, "signature": signature,
    }, timeout=(10, 300))
    if response.status_code != 200:
        raise RuntimeError("Apps Script bridge rejected the upload")
    try:
        result = response.json()
        link = result["url"] if result.get("status") == "ok" else None
    except (ValueError, TypeError, KeyError) as exc:
        raise RuntimeError("Apps Script bridge returned an invalid response") from exc
    if not isinstance(link, str) or not link.startswith("https://drive.google.com/file/d/"):
        raise RuntimeError("Apps Script bridge did not confirm a Drive upload")
    return link


def create_app(config: ServiceConfig, upload_func=forward_to_bridge) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    async def health():
        return {"status": "ready"}

    @app.get("/v1/check")
    async def check(request: Request):
        if not _authorized(request.headers.get("Authorization", ""), config.code_hashes):
            raise HTTPException(401, "Upload access code is invalid")
        return {"status": "ready"}

    @app.post("/v1/uploads")
    async def upload(request: Request):
        if not _authorized(request.headers.get("Authorization", ""), config.code_hashes):
            raise HTTPException(401, "Upload access code is invalid")
        if request.headers.get("Content-Type", "").split(";", 1)[0].lower() != "application/octet-stream":
            raise HTTPException(415, "Use application/octet-stream")
        filename = _filename(request.headers.get("X-Upload-Filename", ""))
        declared = request.headers.get("Content-Length")
        if declared:
            try:
                if int(declared) > config.max_bytes:
                    raise HTTPException(413, "Resume exceeds upload size limit")
            except ValueError as exc:
                raise HTTPException(400, "Invalid content length") from exc

        path = None
        try:
            with tempfile.NamedTemporaryFile(prefix="resume-upload-", suffix=".tmp", delete=False) as target:
                path = target.name
                count = 0
                async for chunk in request.stream():
                    count += len(chunk)
                    if count > config.max_bytes:
                        raise HTTPException(413, "Resume exceeds upload size limit")
                    target.write(chunk)
            if count == 0:
                raise HTTPException(400, "Empty resume")
            try:
                url = await run_in_threadpool(upload_func, path, filename, config)
            except Exception as exc:
                # Google errors can include identifiers. Return a stable, generic message.
                raise HTTPException(502, "Owner Drive upload failed; retry this resume") from exc
            return {"url": url}
        finally:
            if path is not None:
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass

    return app


def build_app() -> FastAPI:
    """Fail startup if any owner bridge setting is missing or invalid."""
    return create_app(ServiceConfig.from_env())
