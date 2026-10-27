# Bulk Upload Assistant

A desktop app for extracting candidate information from resume folders, optionally matching resumes against a job description, uploading eligible resumes to Google Drive, and exporting candidate records as CSV files.

Built with Python, CustomTkinter, the OpenAI API, and the Google Drive API.

## Features

- **Deliberate processing:** choosing a folder previews its contents; processing starts only when you click **Process resumes**.
- **Resume extraction:** reads PDF, DOCX, legacy DOC, and TXT files and extracts names, contact information, country, experience, and LinkedIn URLs when available.
- **Optional job matching:** compares resumes with a pasted or imported job description and shows match scores, strengths, missing requirements, and supporting evidence.
- **Searchable results:** compact results table, optional full columns, sorting, search, and candidate details. Minimum-match filtering is available when job matching is enabled.
- **Google Drive uploads:** background uploads with separate progress, retry, cancellation, and cached links to avoid unnecessary repeat uploads.
- **Accurate completion states:** processing and uploads are tracked separately, including failures and cancelled uploads.
- **CSV export:** exports valid, uploaded candidates, deduplicated by email, in batches of up to 99 records.
- **Dark desktop interface:** larger screen-aware window, collapsible setup sections, slim scrollbars, and an on-demand activity report.

## Requirements

- Python 3.10 or later with Tkinter available.
- An OpenAI API key and access to the models selected by the app.
- A Google account and OAuth desktop-client credentials for Google Drive uploads.
- An internet connection for AI processing and uploads.

PDF, DOCX, and TXT reading does not require Microsoft Word. Legacy DOC files require either Microsoft Word automation on Windows or LibreOffice with `soffice` available on your PATH. Image-only PDFs may need OCR before processing; the app does not include an OCR pipeline.

## Installation

Clone the repository and create a virtual environment:

```bash
git clone https://github.com/Fa1-SaL/bulkupload-assist.git
cd bulkupload-assist
python3 -m venv .venv
```

Activate it on macOS or Linux:

```bash
source .venv/bin/activate
```

Or on Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

Install dependencies:

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

On Windows, install `pywin32` as well if you want to use Microsoft Word automation for legacy DOC files:

```powershell
python -m pip install pywin32
```

Check that Tkinter is available:

```bash
python -m tkinter
```

If this does not open a test window, install a Python distribution with Tk support or your operating system's Tkinter package.

## Configuration

### OpenAI

Open **Settings** in the app, enter your API key, and choose a model available to your API account. A configured key is not a guarantee of a valid API connection; requests are validated when processing begins.

The model menu is defined by `OPENAI_MODELS` in `extractor.py`. Extraction and evaluation use the selected model; job-description parsing uses the separate `LUNA_MODEL` constant, and embeddings use `text-embedding-3-small`. Update those source settings if your account does not have access to the configured models. AI processing can incur API charges.

### Google Drive

1. Create or select a project in Google Cloud and enable the Google Drive API.
2. Configure the OAuth consent screen. If the app is in testing, add the Google account you will use as a test user.
3. Create an OAuth client with the **Desktop app** application type.
4. Download its JSON credentials and save them beside `drive_uploader.py` as `oauth_credentials.json`.
5. Configure the destination using `DRIVE_FOLDER_ID` in `drive_uploader.py`. Drive uploads and sign-in continue to work normally; Google Drive controls are not exposed in Settings. Existing saved destination configuration is preserved.

On the first upload, the app opens a browser for Google sign-in and saves the authorization locally as `token.pickle`. It requests the `drive.file` scope. The app uses OAuth desktop credentials; a service-account file is not required.

Uploads stay in the configured folder. A permission failure is shown as a failure with a retry action; uploads never silently move to My Drive root.

## Run the app

```bash
python extractor.py
```

1. Choose the folder containing your resumes. The folder preview does not call the AI API or upload files.
2. Enter your API key and select a model in **Settings**.
3. Enable **Match against a job description** if you want matching, then paste the description or choose a JD file. Leave it unchecked to parse and upload without matching.
4. Click **Process resumes**. Results appear as files are processed.
5. Search or sort results, switch to **All columns**, or open **View details** for a selected row. Details also open with Enter or a double-click.
6. Monitor the processing and upload stages. The primary button shows **Processing…**, **Uploading…**, **Retry processing / uploads**, or **Done ✓**. Use **Activity**, **Retry uploads**, or **Cancel uploads** as needed. Cancelling uploads does not stop resume processing.
7. Review **Needs attention**, open the source file, edit candidate fields, or select **Evaluate anyway** for a resume whose JD similarity was too low to score. Edits are marked and saved by source content hash for future reruns. Duplicate email records are merged; details show their source filenames.
8. Click **Export CSV**, choose all eligible or filtered records, enter the Job ID for a standard export, and choose a destination.
9. After the batch settles, **Clear batch** resets the results and progress for the next batch. Source files, uploaded files, caches, saved corrections and CSV history remain.

A new processing run is blocked while processing, uploads, or a Drive connection operation are still pending. Closing during active processing asks for confirmation. **Complete with issues** means some files need attention; review the activity report and row statuses.

## CSV output

By default, export includes complete candidates with uploaded resumes. The export dialog explicitly offers **All eligible** and **Filtered** scopes with counts. Records are deduplicated by email. Previously generated candidates for the same Job ID trigger a warning; recent Job IDs require an explicit selection. Job-specific rate, availability, notes and full-time fields start fresh in each dialog. Generating a CSV does not submit candidates to an ATS.

Files are written to the selected directory as:

```text
<job_id>_candidate_data_batch_1.csv
<job_id>_candidate_data_batch_2.csv
...
```

Each file contains up to 99 candidate records, plus a header, with these columns:

```text
name,email,phone,country,job_id,expected_rate,availability_weeks,
resume_link,years_of_experience,linkedin_url,notes,is_fulltime,match_percentage
```

### Export options

Both optional modes are **off by default** in **Settings → Export**:

- **Export Missing too** includes settled, parsed candidates with incomplete data. Missing fields, failed/missing resume links and unavailable scores are blank. Unreadable files, extraction failures, parsing placeholders and uploads still in progress are excluded.
- **Regular export** writes only `name,email,phone,country,resume_link,years_of_experience,linkedin_url`. It adds `match_percentage` only when JD matching was enabled for the processed batch. Changing the live JD checkbox afterwards does not change that batch's export columns. Job ID is optional and the added job/rate/availability/notes/full-time columns are omitted.

The two options can be combined. Files still contain at most 99 records. Existing output names require confirmation before replacement.

## Settings and interaction

**Settings** has four categories: **General**, **Connections**, **Processing**, and **Export**. Changes apply on **Save**; **Cancel**, Escape, or closing the window discards them. Text size is fixed at 100% and has no user-adjustable control.

General contains remembered window/table layout and reopening the last folder. Connections contains the AI API key, optional secure key storage, model selection, and the AI connection test. Processing includes default JD matching, optional subfolder discovery, and a separate clear-analysis action. Export includes a remembered output folder, repeat-generation warnings, both export modes, and a separate clear-history action.

The table supports vertical wheel/trackpad scrolling and horizontal scrolling with **Shift + wheel**. Smooth scrolling and short opening fades are built in, without Settings switches. Horizontal table movement uses pixel-sized animation steps. Settings pages accept wheel input even when the window itself has focus. Scrollbars remain slim and hide their thumb when no overflow exists. Match filters are hidden when JD matching is unchecked. Existing keyboard shortcuts and table selection remain available.

API key persistence is opt-in and off by default. When enabled, `keyring` must provide a native secure OS credential store (macOS Keychain, Windows Credential Manager, or supported Linux desktop credential storage). There is no plaintext fallback. Without it, the key can still be used for the current session. JSON preferences never contain the API key.

## Local data and privacy

Resume text is sent to the OpenAI API for extraction and, when enabled, job matching. Eligible resume files are uploaded to Google Drive. Use data you are authorized to process and review extracted information and match assessments before relying on them.

The app stores runtime data locally:

| File or folder | Purpose |
| --- | --- |
| `oauth_credentials.json` | Google OAuth desktop-client configuration |
| `token.pickle` | Saved Google authorization |
| `resume_cache.json` | Cached extraction and matching data |
| `jd_cache.json` | Cached job-description data |
| `drive_cache.json` | Cached upload links and file hashes |
| `preferences.json` | Non-secret workspace and export preferences |
| `review_state.json` | Hash-scoped manual corrections and per-job CSV history |
| `logs/` | Application logs |
| `processing_summary.csv` | Per-file processing report in the selected resume folder |

Credentials, tokens, preferences, review state, caches, logs, virtual environments, and processing summaries are excluded by `.gitignore`. Keep resume folders and exported CSV files outside the source checkout; they can contain personal information.

## Tests

With the virtual environment activated:

```bash
python -m unittest discover -q
```

Tests cover extraction, matching, caching, upload handling, and UI workflows. They replace external API and upload operations with test doubles. Tests requiring Tk need a graphical desktop session and are skipped when Tk is unavailable.

## Project layout

```text
extractor.py          Desktop UI, file reading, extraction, matching, and export
drive_uploader.py    Google Drive authorization, uploads, and upload caching
requirements.txt     Python dependencies
test_extractor.py    Core regression tests
test_loading_flow.py Folder-discovery and loading-flow tests
test_ui_flow.py      Desktop workflow and UI regression tests
docs/superpowers/    Design and implementation notes
```

The regression suite mocks AI and Drive requests and isolates local state. It includes live Tk widget checks for settings transactions and reopening, window-focused Settings/table scrolling, pixel-sized horizontal animation, batch states, Clear, motion cancellation, and export modes. These checks do not replace a VoiceOver/NVDA accessibility audit or validation against your live service accounts.
