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
5. Set `DRIVE_FOLDER_ID` in `drive_uploader.py` to your destination folder's ID. Replace the project-specific default when using your own Drive.

On the first upload, the app opens a browser for Google sign-in and saves the authorization locally as `token.pickle`. It requests the `drive.file` scope. The app uses OAuth desktop credentials; a service-account file is not required.

If the configured folder rejects an upload because of permissions, the uploader can fall back to the root of My Drive.

## Run the app

```bash
python extractor.py
```

1. Choose the folder containing your resumes. The folder preview does not call the AI API or upload files.
2. Enter your API key and select a model in **Settings**.
3. Enable **Match against a job description** if you want matching, then paste the description or choose a JD file. Leave it unchecked to parse and upload without matching.
4. Click **Process resumes**. Results appear as files are processed.
5. Search or sort results, switch to **All columns**, or open **View details** for a selected row. Details also open with Enter or a double-click.
6. Monitor processing and upload progress. Use **View activity**, **Retry uploads**, or **Cancel uploads** as needed. Cancelling uploads does not stop resume processing.
7. Click **Export CSV**, enter the required Job ID and any optional export fields, and choose a destination.

A new processing run is blocked while processing or uploads are still pending. **Complete with issues** means some files need attention; review the activity report and row statuses.

## CSV output

Export includes eligible candidates whose uploads are complete. The export is deduplicated independently of the table's current search and minimum-match filters; it is not an export of only the visible rows.

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
| `logs/` | Application logs |
| `processing_summary.csv` | Per-file processing report in the selected resume folder |

Credentials, tokens, caches, logs, virtual environments, and processing summaries are excluded by `.gitignore`. Keep resume folders and exported CSV files outside the source checkout; they can contain personal information.

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
