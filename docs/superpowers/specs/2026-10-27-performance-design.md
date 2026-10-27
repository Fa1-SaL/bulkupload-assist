# Performance Improvements — Design Spec

Date: 2026-10-27
Status: Approved in conversation (Sections 1–3); awaiting written-spec review
Scope: `extractor.py`, `drive_uploader.py`, `test_extractor.py`

## Goal

Make a run of **up to 500 resumes** complete as fast as possible while staying stable (no rate-limit storms, no UI freezes). Speed is the priority. The user has chosen **`gpt-6-luna` for all LLM calls**; score changes caused by the model switch are accepted. Scoring logic (prompts, Stage-1/Stage-2 formula and selection) is otherwise unchanged.

### Baseline

Last logged run (3 resumes): total 35.06s — match evaluation 28.09s, embeddings 1.38s, file read 0.10s.

### Success criteria

1. Re-running the same folder against the same JD and model makes **zero** match-evaluation API calls.
2. No more than `LLM_CONCURRENCY` (32) OpenAI requests are in flight at once; 429s are retried with jitter and do not compound with SDK retries.
3. The results table is rebuilt at most once per UI tick (100ms), regardless of event volume.
4. Up to 4 Drive uploads run concurrently, and uploads start before match evaluation finishes.
5. A failure on any single resume never aborts the run; the progress bar moves throughout the match phase; closing the app mid-run keeps cached work done so far.
6. All existing tests pass (updated where they assert the old model tiering); new tests cover each change below.

### Non-goals

- Splitting `extractor.py` into modules (approach B — rejected).
- OpenAI Batch API (rejected: hours of latency).
- Changing Drive folder sharing settings.
- Removing dead code (`convert_docx_to_pdf`).

---

## Section 1 — LLM pipeline

### 1.1 Model

- Add `"gpt-6-luna"` as the first entry of `OPENAI_MODELS` (becomes the UI default). Other models remain selectable.
- `parse_jd_with_ai`: remove length-based tiering (`JD_SIMPLE_THRESHOLD` / `JD_COMPLEX_THRESHOLD`); always use `LUNA_MODEL = "gpt-6-luna"`.
- `extract_data_with_ai_async` and `extract_data_with_ai`: default model becomes `LUNA_MODEL`.
- Match evaluation uses the model selected in the UI (default Luna).
- Delete `DOWNGRADE_MAP` and both similarity-based downgrade blocks (`run_match_task`, `process_single_file`).
- Update `test_extractor.py` tests that assert `gpt-4o-mini` / `gpt-4o` / `gpt-4.1` for JD parsing to assert `gpt-6-luna`.

**Risk — structured outputs.** It is unverified that `gpt-6-luna` supports `client.beta.chat.completions.parse` with a Pydantic `response_format`. The first implementation task is a one-call smoke test with the user's API key. If unsupported, fall back to `response_format={"type": "json_schema", "json_schema": {...}}` built from `Model.model_json_schema()` and parse with `Model.model_validate_json(content)`, wrapped in one helper used by all three call sites.

### 1.2 Concurrency and retries

- Module constant `LLM_CONCURRENCY = 32`.
- One `asyncio.Semaphore(LLM_CONCURRENCY)` created per pipeline run, passed to (or closed over by) every extraction, match and embedding call; each API call acquires it.
- `AsyncOpenAI(api_key=..., max_retries=0)` and `OpenAI(api_key=..., max_retries=0)` so SDK retries do not stack on ours.
- `call_openai_with_retry`: keep 5 attempts and exponential base; sleep `delay * random.uniform(0.5, 1.5)`; if the exception carries a `retry-after` header (e.g. `openai.RateLimitError.response.headers`), sleep `max(that, computed)`.
- Embedding chunks (size 50) run concurrently via `asyncio.gather` under the semaphore.
- On JD cache miss, JD parsing and JD embedding run concurrently (`asyncio.gather` over `asyncio.to_thread` of the sync calls, or async equivalents).

### 1.3 Match-result cache

- New top-level key `"match_cache"` inside `resume_cache.json` (existing per-hash entries unchanged; loader tolerates its absence).
- Key: `f"{file_hash}:{jd_hash}:{model}"`. Value: the dict returned by `evaluate_candidate_match_async`.
- In `run_match_task`, for Stage-2-selected candidates: on cache hit, use cached dict and skip the API call; on miss, call and store the result **unless** `Summary` starts with `"Evaluation Failed"`.
- Stage-1 fallback scores are not cached (cheap to compute).

### 1.4 Unchanged

Prompts, `MatchResult` schema, Stage-1 formula, `STAGE2_TOP_N = 15`, `STAGE2_THRESHOLD = 0.35`, similarity pre-filter `< 0.20`.

---

## Section 2 — UI and threading

### 2.1 UI event queue

- `self.ui_events = queue.Queue()` created in `__init__`.
- Helper `self._post(fn)` → `self.ui_events.put(fn)`. Background threads (pipeline thread, upload workers) only ever call `_post`; they never call `self.after` or touch Tk widgets / `extracted_data` / upload counters directly.
- `_drain_ui_events()` runs on the main thread, rescheduled via `self.after(100, ...)` from `__init__`: executes all queued callables, then if `self._table_dirty` calls `filter_table()` once, if `self._progress_dirty` calls the progress-label update once, and clears both flags.
- Routed through `_post`: `add_or_update_candidate`, `handle_upload_starting/success/failure`, pipeline mutations of `total_uploads` / `failed_uploads`, status-label and progress-bar updates from the pipeline.

### 2.2 Coalesced rendering

- `add_or_update_candidate` and upload handlers mutate data and set `_table_dirty` / `_progress_dirty`; they do not render.
- `update_upload_progress_ui` becomes a direct (main-thread) label update; its inner `self.after(0, update)` wrapper is removed. Callers set `_progress_dirty` instead.

### 2.3 Filename index

- `self.candidates_by_file: dict[str, dict]` maintained alongside `self.extracted_data` (the list remains the source for export, retry and tests).
- `add_or_update_candidate` and the three upload handlers use the dict for lookup.
- Reset both together at the start of each run wherever `extracted_data` is reset.

### 2.4 Table state preservation

- `add_to_tree` inserts with `iid=row["SourceFilename"]`.
- `sort_table` records `self._sort_state = (col, numeric, descending)`; `filter_table` reapplies it after rebuilding.
- `filter_table` saves and restores the selection (by iid) and `yview`.
- `filter_table` uses `safe_match_percentage(row)` instead of the inline `float(match_pct)` logic.
- `on_row_double_click` looks up via `self.candidates_by_file[iid]` instead of matching on email.

### 2.5 Logging

- `LogRedirector` opens the file with `buffering=1` (line-buffered), drops the per-write `flush()`, and guards `write` with a `threading.Lock`.

---

## Section 3 — Uploads, caches and file I/O

### 3.1 Upload worker pool

- `UPLOAD_WORKERS = 4`.
- `start_upload_worker` starts 4 threads over the shared `upload_queue`.
- Auth stays lazy (no browser prompt at app start): the first worker that needs Drive (cache miss) obtains credentials under a one-time lock, so the OAuth browser flow / token refresh happens exactly once; other workers block on that lock, then reuse the same `creds`. Each worker builds its own `build('drive', 'v3', credentials=creds, cache_discovery=False)`. Expose a `get_drive_credentials()` helper in `drive_uploader.py` that returns `creds` (refactor of the existing token logic; `get_drive_service` calls it).
- Remove `upload_lock` usage from `_upload_single_file` (keep retry/backoff/root fallback). `upload_all_resumes` gives each of its workers its own service as well.
- Shutdown: one `None` sentinel per worker if a stop path exists; workers are daemon threads.

### 3.2 Early upload queueing

- After the similarity pre-filter, for each surviving candidate: `add_or_update_candidate` with `ResumeLink="Pending"`, `MatchPercentage="Scoring…"`, increment `total_uploads`, and enqueue the upload job. Remove the enqueue from `run_match_task`.
- `run_match_task` then only updates the existing row with match data (existing "keep non-Pending link" logic preserves upload status).
- `safe_match_percentage` treats `"Scoring…"` as 0 (it already returns 0 for non-numeric strings via `ValueError`).
- Set of uploaded files is unchanged.

### 3.3 Hash once

- `read_worker` computes `get_file_hash(filepath)` and returns it with the text; the separate sequential hashing loop is removed.
- Upload jobs become `(filename, filepath, file_hash)`; the worker uses the provided hash. `retry_failed_uploads` passes `None` and the worker computes it only then.

### 3.4 Drive cache writes

- In-memory cache shared by workers, guarded by `threading.Lock`.
- Saved every 10 successful uploads and when the queue becomes empty (`upload_queue.empty()` after `task_done`).
- Atomic save helper `atomic_write_json(path, data)` (write `path + ".tmp"`, `os.replace`) shared by all cache writers.

### 3.5 Cache file format

- `resume_cache.json` and `jd_cache.json`: compact `json.dump(..., separators=(",", ":"))`, written via `atomic_write_json`.
- `resume_cache.json` written only if new entries were added to it or to `match_cache`.
- Existing indented caches load unchanged.

### 3.6 Legacy `.doc` extraction

- In the read phase, `.doc` files are routed to a single dedicated thread that calls `pythoncom.CoInitialize()`, creates one `Word.Application`, extracts all `.doc` files sequentially, then `Quit()`s and `CoUninitialize()`s.
- `extract_text_from_doc` gains an optional `word` parameter; when supplied it reuses it and does not quit. Per-file LibreOffice fallback unchanged.
- `.pdf`, `.docx`, `.txt` stay in the 8-thread pool. Both phases run concurrently.
- On non-Windows (no `comtypes`/`pythoncom`), the dedicated thread just calls the existing fallback path.

---

## Section 4 — Stability and smoothness (added at user request, 2026-10-27)

### 4.1 One bad resume must not abort the run

`run_match_task` has no `try/except`; any exception (e.g. an unexpected `None` section, a malformed cached entry) propagates out of `asyncio.gather` and aborts the whole batch after uploads were already queued. Wrap the body: on exception, log it and update the row with `MatchPercentage=0`, `Summary=f"Evaluation Failed: {e}"` (same shape as `evaluate_candidate_match_async`'s failure dict) so the run always reaches `extraction_finished`.

### 4.2 Progress that reflects the long phase

Today the bar reaches 100% after file reading and then sits still through match evaluation (the longest phase), which looks frozen. Use phase-weighted progress: read 0–0.20, embeddings 0.20–0.30, extraction 0.30–0.40, match evaluation 0.40–1.00 advanced per completed candidate, with status text `"Evaluating matches (k/n)..."`. Constants `PROGRESS_PHASES = {"read": (0.0, 0.20), "embed": (0.20, 0.30), "extract": (0.30, 0.40), "match": (0.40, 1.00)}`.

### 4.3 Checkpoint caches during the run

`resume_cache.json` (including `match_cache`) is written only at the very end; a crash or close after 400 evaluations loses all of it. Checkpoint via `atomic_write_json` every `CACHE_CHECKPOINT_EVERY = 25` completed match evaluations (from the pipeline thread, guarded by a lock so checkpoint and final write never overlap), plus the existing final write.

### 4.4 Clean shutdown

Register `self.protocol("WM_DELETE_WINDOW", self.on_close)`. `on_close` flushes the Drive cache (`_save_drive_cache(force=True)`), sets `self._closing = True` (the drain loop stops rescheduling), then `destroy()`. In-flight daemon threads are abandoned as today; already-checkpointed caches survive.

### 4.5 Bounded per-call wait

Match-evaluation calls use `timeout=60` (was 120) — Luna is a fast tier, and with 5 jittered attempts a single stuck call can otherwise hold its slot for 10+ minutes.

### 4.6 Readable completion popup

The completion `messagebox` lists every skipped file; with hundreds of files it overflows the screen. Show at most 20 skipped filenames followed by `"…and N more (see processing_summary.csv)"`.

---

## Testing

All with mocks; no live API calls except the 1.1 smoke test.

| Area | Test |
|---|---|
| Model | JD parse, extraction, match all request `gpt-6-luna`; `DOWNGRADE_MAP` gone |
| Concurrency | With a fake slow API, peak in-flight calls ≤ `LLM_CONCURRENCY` |
| Retry | Retry sleeps are jittered; SDK clients created with `max_retries=0` |
| Match cache | Second run with same hashes/JD/model makes 0 match calls; failed evaluations not cached |
| UI queue | N posted updates in one tick → exactly one `filter_table` call |
| Sort/selection | Sort and selection survive a `filter_table` rebuild |
| Index | `candidates_by_file` stays consistent with `extracted_data` |
| Uploads | 4 workers run concurrently, each with its own service; no global lock |
| Early upload | Upload completing before match → row ends as `Uploaded` with match data |
| Drive cache | Saved atomically; not saved on every upload |
| .doc | Batch of .doc files creates Word once |
| Stability | An exception in one match task leaves other candidates scored and the run finishes |
| Progress | Progress values are monotonic and the match phase spans 0.40→1.00 |
| Checkpoint | 60 evaluations → ≥2 mid-run cache writes; killed run's cache still loads |
| Shutdown | `on_close` flushes Drive cache and stops drain rescheduling |
| Popup | 300 skipped files → popup lists 20 + "…and 280 more" |

Existing tests helper: after calling pipeline/UI methods directly, drain `app.ui_events` before asserting.

## Risks

- **Luna structured output support** — mitigated by smoke test + JSON-schema fallback (1.1).
- **Rate limits at 32 concurrency** — single constant; lower if 429s persist.
- **Drive credentials across threads** — creds refreshed once before workers start; google-auth refresh is internally safe for read-mostly use, and each worker has its own HTTP transport.
- **No git repo** — changes cannot be reverted via VCS; implementation should begin by copying the project folder (or `git init` with an initial commit, if the user agrees).
