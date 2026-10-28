# Windows PDF Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Start and read PDFs on Windows with Smart App Control enabled.

**Architecture:** Put PDF reading behind a small adapter. Windows uses pure-Python pypdf without importing PyMuPDF. Other platforms prefer PyMuPDF and fall back to pypdf when the native library cannot load or read a document. Existing application functions retain their return values and error handling.

**Tech Stack:** Python, pypdf 6.x, optional PyMuPDF, unittest, existing CustomTkinter UI.

**Spec:** User request: “Implement the PDF fallback so it works on Windows with Smart App Control enabled, end goal is to make the app run on windows”. Preserve the dark theme and existing functionality.

## Global Constraints

- Keep Smart App Control enabled; change no security policies.
- Preserve PDF text extraction and URI annotation extraction for LinkedIn normalization.
- Do not add OCR, passwords, external conversion services or candidate data uploads.
- Keep native-PDF performance on platforms where it works.

## Review Focus

- Blocked or absent native library must not prevent startup.
- Multi-page documents must preserve text and hidden hyperlinks.
- Malformed or password-protected documents must remain read failures.
- Empty/image-only documents must not gain invented text.
- New installations and existing Windows checkouts must receive the fallback dependency.

## Task 1: PDF compatibility and Windows verification

**Files:** Create `pdf_reader.py`, `test_pdf_compat.py`, `test_windows_startup.py`; modify `extractor.py`, `requirements.txt`, `test_extractor.py`, `README.md`, `app_preferences.py`, `ui_interactions.py` and portable scrolling test fixtures.

**Interfaces:** Adapter exports `extract_text(path) -> str` and `extract_hyperlinks(path) -> list[str]`. Application wrappers continue returning `None` / `[]` on failures.

- [x] Add real generated PDF fixtures and subprocess regressions for blocked native imports, Windows avoiding native imports, text/annotations, blank, corrupt and encrypted PDFs.
- [x] Run `python -m unittest test_pdf_compat -v`; observe startup failures before implementation.
- [x] Implement the adapter and route existing wrappers through it; make PyMuPDF optional on Windows and require pypdf.
- [x] Replace the existing native-specific hyperlink mock with backend-neutral normalization coverage and real PDF parsing regressions.
- [x] Run targeted tests, full discovery, compilation and whitespace checks.
- [x] Publish the verified change, update the remote Windows checkout, and install the dependency from PyPI.
- [x] On Windows, run PDF tests and open the real app; verify Smart App Control remains On. Fix other issues required for startup.

## Verification record

- PDF regressions: all five initial cases failed on the original fatal native import, then passed. Expanded coverage: 8/8 passed locally and on Windows Python 3.13 / pypdf 6.21.3.
- Windows launch exposed a second existing blocker: Tk 8.6 rejects `<TouchpadScroll>`. A real app/Settings startup regression reproduced that crash, then passed after capability-guarded bindings were added to Settings, table, scrollbars and the main window.
- Final production change: local full suite 266/266 passed; compilation and whitespace checks passed. Independent review found no blocking PDF defect; its startup/native-failure/annotation coverage recommendations were added.
- Windows targeted desktop suite: 39/42 passed, including PDF parsing and the app/Settings startup regression. Three scrolling assertions failed: `test_horizontal_wheel_over_table_works_with_window_focus`, `test_wheel_over_horizontal_scrollbar_is_eased_once`, `test_scrollbar_thumbs_only_appear_when_content_overflows`.
- Those three tests now set a fixed window size to establish the overflow their assertions require on wider/high-DPI displays. All three passed locally after this fixture adjustment. The remote session ended before their Windows recheck; this remains a test-verification limitation.
- The updated app was visibly running on the Windows desktop and the user confirmed “alright, works.” Smart App Control was verified On. No security policies were changed; no AI or Drive requests were made during verification.
