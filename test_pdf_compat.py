"""Real PDF regressions for startup when a native reader is blocked by Windows."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from pypdf import PdfWriter
from pypdf.generic import (ArrayObject, DecodedStreamObject, DictionaryObject,
                           FloatObject, NameObject, TextStringObject)


def write_pdf(path, *, blank=False, password=None):
    """A synthetic two-page resume with text and a hidden URI annotation."""
    writer = PdfWriter()
    for text in ("Alice Resume alice@example.com", "Python engineer experience"):
        page = writer.add_blank_page(width=612, height=792)
        if not blank:
            font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                NameObject('/Subtype'): NameObject('/Type1'),
                NameObject('/BaseFont'): NameObject('/Helvetica')})
            page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):
                DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
            stream = DecodedStreamObject()
            stream.set_data(f'BT /F1 12 Tf 50 700 Td ({text}) Tj ET'.encode('ascii'))
            page[NameObject('/Contents')] = writer._add_object(stream)
    if not blank:
        link = DictionaryObject({NameObject('/Type'): NameObject('/Annot'),
            NameObject('/Subtype'): NameObject('/Link'),
            NameObject('/Rect'): ArrayObject([FloatObject(n) for n in (10, 10, 100, 30)]),
            NameObject('/A'): writer._add_object(DictionaryObject({
                NameObject('/S'): NameObject('/URI'),
                NameObject('/URI'): TextStringObject('https://www.linkedin.com/in/pdf-user-in')}))})
        note = DictionaryObject({NameObject('/Subtype'): NameObject('/Text'),
            NameObject('/Contents'): TextStringObject('Not a link')})
        writer.pages[1][NameObject('/Annots')] = writer._add_object(ArrayObject([
            writer._add_object(note), writer._add_object(link)]))
    if password:
        writer.encrypt(password)
    with open(path, 'wb') as output:
        writer.write(output)


BLOCK_NATIVE = '''
import importlib.abc, sys
class BlockNative(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'fitz', 'pymupdf', 'mupdf'}:
            raise ImportError('DLL load failed: An Application Control policy has blocked this file.')
sys.meta_path.insert(0, BlockNative())
import extractor
'''


class TestPdfCompatibility(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.pdf = Path(self.temp.name) / 'synthetic.pdf'
        write_pdf(self.pdf)

    def run_blocked(self, code, bootstrap=BLOCK_NATIVE):
        result = subprocess.run([sys.executable, '-c', bootstrap + '\n' + code, str(self.pdf)],
            cwd=Path(__file__).resolve().parent, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_blocked_native_library_still_starts_and_reads_every_page_and_link(self):
        self.run_blocked('''
text = extractor.extract_text_from_pdf(sys.argv[1])
assert 'Alice Resume alice@example.com' in text, repr(text)
assert 'Python engineer experience' in text, repr(text)
assert extractor.extract_pdf_hyperlinks(sys.argv[1]) == ['https://www.linkedin.com/in/pdf-user-in']
candidate, _ = extractor.normalize_and_validate_candidate(
    dict(Name='Alice', Email='alice@example.com', Phone='2125551234', Country='US', YearsOfExperience='5'),
    text, sys.argv[1])
assert candidate['linkedin_url'] == 'https://www.linkedin.com/in/pdf-user-in', candidate
assert candidate['linkedin_source'] == 'PDF Hyperlink', candidate
''')

    def test_windows_never_attempts_to_load_native_pdf_binary(self):
        self.run_blocked('''
sys.platform = 'win32'
assert 'Alice Resume' in extractor.extract_text_from_pdf(sys.argv[1])
assert extractor.extract_pdf_hyperlinks(sys.argv[1]) == ['https://www.linkedin.com/in/pdf-user-in']
''', bootstrap=BLOCK_NATIVE.replace('raise ImportError(', 'raise AssertionError('))

    def test_available_default_reader_preserves_text_and_links(self):
        self.run_blocked('''
assert 'Alice Resume' in extractor.extract_text_from_pdf(sys.argv[1])
assert 'Python engineer experience' in extractor.extract_text_from_pdf(sys.argv[1])
assert extractor.extract_pdf_hyperlinks(sys.argv[1]) == ['https://www.linkedin.com/in/pdf-user-in']
''', bootstrap='import extractor, sys')

    def test_native_document_failure_recovers_with_real_pypdf(self):
        self.run_blocked('''
from unittest.mock import patch
class BrokenNative:
    def open(self, path):
        raise RuntimeError('Native parser cannot read this document')
with patch.object(extractor.pdf_reader, '_native_reader', return_value=BrokenNative()):
    assert 'Alice Resume' in extractor.extract_text_from_pdf(sys.argv[1])
    assert extractor.extract_pdf_hyperlinks(sys.argv[1]) == ['https://www.linkedin.com/in/pdf-user-in']
''')

    def test_malformed_annotation_does_not_hide_a_later_valid_link(self):
        from pypdf import PdfReader
        reader = PdfReader(self.pdf)
        writer = PdfWriter()
        writer.append(reader)
        annotations = writer.pages[1]['/Annots']
        annotations.insert(0, TextStringObject('invalid annotation'))
        reader.close()
        with open(self.pdf, 'wb') as output:
            writer.write(output)
        self.run_blocked('''
assert extractor.extract_pdf_hyperlinks(sys.argv[1]) == ['https://www.linkedin.com/in/pdf-user-in']
''')

    def test_corrupt_pdf_is_a_read_failure(self):
        self.pdf.write_bytes(b'Not a PDF')
        self.run_blocked('''
assert extractor.extract_text_from_pdf(sys.argv[1]) is None
assert extractor.extract_pdf_hyperlinks(sys.argv[1]) == []
''')

    def test_blank_pdf_has_no_invented_text_or_links(self):
        write_pdf(self.pdf, blank=True)
        self.run_blocked('''
assert not extractor.extract_text_from_pdf(sys.argv[1]).strip()
assert extractor.extract_pdf_hyperlinks(sys.argv[1]) == []
''')

    def test_password_protected_pdf_is_a_read_failure(self):
        write_pdf(self.pdf, password='private-test-password')
        self.run_blocked('''
assert extractor.extract_text_from_pdf(sys.argv[1]) is None
assert extractor.extract_pdf_hyperlinks(sys.argv[1]) == []
''')


if __name__ == '__main__':
    unittest.main()
