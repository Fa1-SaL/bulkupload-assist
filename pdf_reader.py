"""PDF text and links without requiring a native library on Windows."""
from functools import lru_cache
import importlib
import logging
import sys

from pypdf import PdfReader


_log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _load_pymupdf():
    try:
        return importlib.import_module('pymupdf')
    except (ImportError, OSError):
        _log.info('Native PDF reader unavailable; using pypdf.')
        return None


def _native_reader():
    # Avoid even attempting to load an unsigned .pyd on Windows. Catching the
    # import error alone would still trigger Application Control block events.
    return None if sys.platform == 'win32' else _load_pymupdf()


def extract_text(path):
    """Read every page. Unreadable/protected files raise to the application."""
    native = _native_reader()
    if native is not None:
        try:
            with native.open(path) as document:
                return ''.join(text + '\n' for page in document
                               if (text := page.get_text('text')))
        except Exception:
            _log.info('Native PDF text extraction failed; trying pypdf.')
    with open(path, 'rb') as stream:
        reader = PdfReader(stream)
        return ''.join(text + '\n' for page in reader.pages
                       if (text := page.extract_text()))


def _resolve(value):
    return value.get_object() if hasattr(value, 'get_object') else value


def extract_hyperlinks(path):
    """Return URI link annotations, including URLs absent from visible text."""
    native = _native_reader()
    if native is not None:
        try:
            with native.open(path) as document:
                return [link['uri'] for page in document for link in page.get_links()
                        if link.get('kind') == native.LINK_URI and link.get('uri')]
        except Exception:
            _log.info('Native PDF link extraction failed; trying pypdf.')
    links = []
    with open(path, 'rb') as stream:
        reader = PdfReader(stream)
        for page in reader.pages:
            for reference in _resolve(page.get('/Annots')) or []:
                try:
                    annotation = _resolve(reference)
                    if annotation.get('/Subtype') != '/Link':
                        continue
                    action = _resolve(annotation.get('/A'))
                    if not action or action.get('/S') != '/URI':
                        continue
                    uri = _resolve(action.get('/URI'))
                    if isinstance(uri, bytes):
                        uri = uri.decode('utf-8', errors='replace')
                    if isinstance(uri, str) and uri:
                        links.append(uri)
                except (AttributeError, TypeError, ValueError):
                    # One malformed annotation must not hide valid later links.
                    _log.debug('Skipping malformed PDF link annotation.')
    return links
