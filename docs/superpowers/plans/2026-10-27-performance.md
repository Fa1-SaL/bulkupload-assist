# Performance Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make 500-resume runs fast, smooth and stable: `gpt-6-luna` everywhere, bounded LLM concurrency, a match-result cache, coalesced thread-safe UI updates, 4 parallel Drive uploads that start before scoring finishes, per-candidate failure isolation, live progress and crash-safe caches.

**Architecture:** Targeted changes inside the existing two modules (approach A — no module split). `extractor.py` keeps the async pipeline but gains a per-run semaphore, a match cache section in `resume_cache.json`, and a main-thread UI event queue. `drive_uploader.py` gains a shared-credentials helper, an atomic JSON writer, and loses the global upload lock.

**Tech Stack:** Python 3, customtkinter/ttk, `openai` (AsyncOpenAI, structured outputs), google-api-python-client, PyMuPDF, comtypes (Windows only), `unittest`.

**Spec:** `docs/superpowers/specs/2026-10-27-performance-design.md`

## Global Constraints

- Model id is exactly `"gpt-6-luna"`, exposed as `LUNA_MODEL` in `extractor.py`.
- `LLM_CONCURRENCY = 32`; `UPLOAD_WORKERS = 4`; `DRIVE_CACHE_SAVE_EVERY = 10`; UI drain interval 100ms.
- Prompts, `MatchResult` schema, Stage-1 formula, `STAGE2_TOP_N = 15`, `STAGE2_THRESHOLD = 0.35`, similarity pre-filter `< 0.20` are unchanged.
- All cache writes go through `atomic_write_json` and are compact (`separators=(",", ":")`); readers must still load the old indented files.
- Background threads never touch Tk widgets, `extracted_data`, `candidates_by_file` or upload counters directly — only via `self._post(fn)`.
- Never write an API key, OAuth token or credential to any file, test, plan or commit. `token.pickle`, `oauth_credentials.json`, `service_account.json`, `*_cache.json` and `logs/` are never committed.
- Tests must never read or delete the real cache files in the project folder (Task 0 redirects them to a temp dir).
- Test command: `.venv/bin/python -m unittest test_extractor -v` (Windows: `.venv\Scripts\python -m unittest test_extractor -v`).

## Review Focus

1. **"Start Matching" clicked again while the previous run's uploads are still in flight** — late upload events for filenames no longer in `candidates_by_file` are ignored without error. Test in Task 4.
2. **Model changed in the dropdown between two runs on the same folder/JD** — match cache misses and re-evaluates with the new model. Test in Task 3.
3. **App killed / exception while a cache file is being written** — the previous cache file stays loadable. Test in Task 3.
4. **First-ever run with no `token.pickle` and 4 upload workers starting together** — exactly one OAuth/credential fetch happens. Test in Task 7.
5. **User sorts the table (e.g. by Match %) mid-run** — the sort persists through subsequent live refreshes. Test in Task 5.

---

## File Map

| File | Changes |
|---|---|
| `extractor.py` | Model constants, retry/semaphore, match cache, UI queue, table state, logging, upload pool, early queueing, hash-once, .doc batching |
| `drive_uploader.py` | `atomic_write_json`, `get_drive_credentials`, `get_drive_service(creds=...)`, remove `upload_lock` |
| `test_extractor.py` | Temp-dir cache isolation, updated mocks, new tests per task |
| `.gitignore` | New (Task 0) |

---

### Task 0: Safety net and test isolation

**Files:**
- Create: `.gitignore`
- Modify: `extractor.py` (add `JD_CACHE_FILE` constant next to `RESUME_CACHE_FILE`; use it at the inline `cache_file = os.path.join(..., "jd_cache.json")` in `async_extraction_pipeline`)
- Modify: `test_extractor.py` (`TestExtractor.setUp/tearDown`, `TestDriveUploader.setUp/tearDown`)

**Interfaces:**
- Produces: `extractor.JD_CACHE_FILE: str`, `extractor.RESUME_CACHE_FILE: str`, `drive_uploader.DRIVE_CACHE_FILE: str` — all patchable module attributes; tests patch them to a `tempfile.mkdtemp()` dir.

- [ ] **Step 1: Back up the project** — `cp -R "bulk upload_v2" "bulk upload_v2.bak-2026-10-27"` from the parent folder. Confirm the copy contains the three `*_cache.json` files.
- [ ] **Step 2: Create `.gitignore`** with: `token.pickle`, `oauth_credentials.json`, `service_account.json`, `*_cache.json`, `*.tmp`, `logs/`, `.venv/`, `__pycache__/`, `processing_summary.csv`.
- [ ] **Step 3: Init git** — `git init && git add .gitignore *.py requirements.txt docs && git status` — verify none of the ignored secret/cache files are staged, then `git commit -m "chore: baseline before performance work"`.
- [ ] **Step 4: Create venv** — `python3 -m venv .venv && .venv/bin/pip install PyMuPDF customtkinter pydantic openai google-api-python-client google-auth google-auth-oauthlib docx2txt` (add `comtypes pywin32` on Windows).
- [ ] **Step 5: Add `JD_CACHE_FILE`** in `extractor.py` and use it in the pipeline.
- [ ] **Step 6: Isolate tests** — in `TestExtractor.setUp` and `TestDriveUploader.setUp`, create `self.tmp = tempfile.mkdtemp()` and start `patch.object` for `extractor.RESUME_CACHE_FILE`, `extractor.JD_CACHE_FILE`, `drive_uploader.DRIVE_CACHE_FILE` → files inside `self.tmp`; remove the existing `os.remove` cleanup of real files; `tearDown` stops patches and `shutil.rmtree(self.tmp)`.
- [ ] **Step 7: Run baseline** — run the test command. Record the pass/fail list in the commit message; pre-existing failures are not this plan's to fix but must not grow. Confirm real `*_cache.json` files in the project folder still exist and are unchanged.
- [ ] **Step 8: Commit** — `git commit -am "test: isolate cache files in tests"`.

---

### Task 1: Switch to gpt-6-luna

**Files:**
- Modify: `extractor.py` — `OPENAI_MODELS`, delete `DOWNGRADE_MAP`, `JD_SIMPLE_THRESHOLD`, `JD_COMPLEX_THRESHOLD`; `parse_jd_with_ai`, `extract_data_with_ai`, `extract_data_with_ai_async`, `run_match_task`, `process_single_file`
- Test: `test_extractor.py`

**Interfaces:**
- Produces: `LUNA_MODEL = "gpt-6-luna"`; `OPENAI_MODELS[0] == LUNA_MODEL`; `extract_data_with_ai_async(text, filename, client, model=LUNA_MODEL)`.

- [ ] **Step 1: Smoke-test Luna structured outputs (user runs it).** Ask the user to run, in their own terminal with `OPENAI_API_KEY` exported (never pasted into chat or files):

```bash
.venv/bin/python -c "from openai import OpenAI; from pydantic import BaseModel
class R(BaseModel):
    name: str
    years: float
c = OpenAI().beta.chat.completions.parse(model='gpt-6-luna', messages=[{'role':'user','content':'Jane Doe, 7 years of Python.'}], response_format=R)
print(c.choices[0].message.parsed)"
```

Expected: `name='Jane Doe' years=7.0`. **If it errors with an unsupported `response_format`/structured-output message, stop and insert Task 1b (below) before continuing.**

- [ ] **Step 2: Write failing tests** — replace `test_dynamic_jd_model_selection` with `test_jd_parse_always_uses_luna`: for JD lengths 8, 2000 and 5000 assert `parse.assert_called_with(model="gpt-6-luna", messages=ANY, response_format=ANY, timeout=60)`. Add `test_luna_is_default_model`: `assertEqual(extractor.OPENAI_MODELS[0], "gpt-6-luna")` and `assertFalse(hasattr(extractor, "DOWNGRADE_MAP"))`. Add `test_extract_async_defaults_to_luna`: call `extract_data_with_ai_async("text", "f.pdf", client)` with an `AsyncMock` client; assert `model="gpt-6-luna"` in the `parse` call kwargs.
- [ ] **Step 3: Run** — expect the three new tests to FAIL.
- [ ] **Step 4: Implement** — add `LUNA_MODEL`, put it first in `OPENAI_MODELS`, make JD parse use it unconditionally, change both extraction defaults, delete `DOWNGRADE_MAP` and both downgrade blocks (`eval_model` becomes `model`), delete the two JD threshold constants.
- [ ] **Step 5: Run full suite** — new tests PASS; no new failures vs. baseline.
- [ ] **Step 6: Commit** — `git commit -am "feat: use gpt-6-luna for all LLM calls"`.

#### Task 1b (only if Step 1 failed): JSON-schema fallback

- Add `async def parse_structured(client, model: str, messages: list, schema: type[BaseModel], timeout: int)` → `BaseModel` that calls `client.chat.completions.create(..., response_format={"type": "json_schema", "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema(), "strict": False}})` and returns `schema.model_validate_json(resp.choices[0].message.content)`; plus a sync twin `parse_structured_sync`. Route all `beta.chat.completions.parse` call sites through them. Test: with a mocked client returning `'{"full_name": "A", ...}'`, the helper returns a `ResumeData` with `full_name == "A"`. Commit `feat: json-schema fallback for structured outputs`.

---

### Task 2: Bounded concurrency and sane retries

**Files:**
- Modify: `extractor.py` — `call_openai_with_retry`, `get_embeddings_batch`, `extract_data_with_ai_async`, `evaluate_candidate_match_async`, client construction and embedding/JD steps in `async_extraction_pipeline`
- Test: `test_extractor.py` (new `TestConcurrency` class; update `side_effect_embeddings_batch` mocks to accept `**kwargs`)

**Interfaces:**
- Consumes: `LUNA_MODEL` (Task 1).
- Produces:
  - `LLM_CONCURRENCY = 32`
  - `def retry_delay(base_delay: float, exc: Exception) -> float` — jittered `base_delay * uniform(0.5, 1.5)`, raised to `retry-after` seconds when `exc.response.headers["retry-after"]` parses as a number.
  - `async def call_openai_with_retry(func, *args, semaphore: Optional[asyncio.Semaphore] = None, **kwargs)` — acquires `semaphore` around each attempt only (not during sleeps).
  - `get_embeddings_batch(texts, async_client, semaphore=None)`, `extract_data_with_ai_async(..., semaphore=None)`, `evaluate_candidate_match_async(..., semaphore=None)` — pass it through to `call_openai_with_retry`.

- [ ] **Step 1: Write failing tests:**
  - `test_semaphore_caps_inflight`: 100 tasks call `call_openai_with_retry(fake, semaphore=asyncio.Semaphore(32))` where `fake` increments a counter, `await asyncio.sleep(0.01)`, decrements; assert recorded peak `== 32`.
  - `test_retry_delay_is_jittered`: with `random.uniform` patched to return 0.5 then 1.5, `retry_delay(2.0, Exception())` returns `1.0` then `3.0`.
  - `test_retry_delay_honors_retry_after`: exception with `response.headers = {"retry-after": "7"}` → `retry_delay(1.0, exc) >= 7.0`.
  - `test_clients_disable_sdk_retries`: run the pipeline (existing `test_direct_mode_success` setup) and assert `mock_async_openai.assert_called_with(api_key="api-key", max_retries=0)` and same for `mock_openai`.
- [ ] **Step 2: Run** — expect FAIL.
- [ ] **Step 3: Implement** — `retry_delay`; semaphore param; in the pipeline create `llm_sem = asyncio.Semaphore(LLM_CONCURRENCY)` once and pass it to every extraction, match and embedding call; build both clients with `max_retries=0`; replace the sequential embedding-chunk loop with `asyncio.gather` over chunks; on JD cache miss run `parse_jd_with_ai` and `get_embedding` together via `asyncio.gather(asyncio.to_thread(...), asyncio.to_thread(...))`, then write the JD cache only if `parsed_jd` is truthy (unchanged rule).
- [ ] **Step 4: Run full suite** — PASS, no regressions.
- [ ] **Step 5: Commit** — `git commit -am "perf: cap LLM concurrency, jittered retries, parallel embeddings"`.

---

### Task 3: Match-result cache and atomic compact cache writes

**Files:**
- Modify: `drive_uploader.py` (add `atomic_write_json`)
- Modify: `extractor.py` — resume-cache load, `run_match_task`, cache write step, JD cache write
- Test: `test_extractor.py`

**Interfaces:**
- Consumes: semaphore-aware `evaluate_candidate_match_async` (Task 2); `JD_CACHE_FILE`, `RESUME_CACHE_FILE` (Task 0).
- Produces:
  - `drive_uploader.atomic_write_json(path: str, data) -> None` — compact dump to `path + ".tmp"`, then `os.replace`; on exception removes the tmp file and re-raises.
  - `extractor.match_cache_key(file_hash: str, jd_hash: str, model: str) -> str` → `f"{file_hash}:{jd_hash}:{model}"`.
  - `resume_cache["match_cache"]: dict[str, dict]` (loader uses `resume_cache.setdefault("match_cache", {})`; per-file loops skip the `"match_cache"` key).

- [ ] **Step 1: Write failing tests:**
  - `test_atomic_write_json_preserves_old_on_failure`: write `{"a": 1}`, then call with an unserializable object (`{"b": object()}`) → raises `TypeError`; file still loads as `{"a": 1}`; no `.tmp` left.
  - `test_atomic_write_json_is_compact`: output contains no `"\n"` and no `": "`.
  - `test_match_cache_skips_second_run`: run the pipeline twice with the `test_direct_mode_success` mocks; `mock_eval_match_async.call_count` is 1 after both runs.
  - `test_match_cache_misses_on_model_change`: second run with model `"gpt-4.1"` → call count 2.
  - `test_match_cache_misses_on_jd_change`: second run with JD `"JD2"` → call count 2.
  - `test_failed_evaluation_not_cached`: eval returns `Summary="Evaluation Failed: boom"`; second run → call count 2.
  - `test_old_indented_cache_loads`: pre-write `RESUME_CACHE_FILE` with `json.dump(..., indent=4)` (no `match_cache` key); pipeline runs without error and uses the cached embedding (`get_embeddings_batch` not called).
- [ ] **Step 2: Run** — expect FAIL.
- [ ] **Step 3: Implement** — `atomic_write_json`; import it in `extractor.py`; in `run_match_task` (Stage-2 branch only) check/store via `match_cache_key(resume["file_hash"], jd_hash, model)`; track a `cache_dirty` flag set on any new resume entry or match entry; write `resume_cache.json` via `atomic_write_json` only when dirty; write `jd_cache.json` via `atomic_write_json`. `jd_hash` must be defined even with no JD (use `""`; `parsed_jd` is `None` so the Stage-2 branch never runs).
- [ ] **Step 4: Run full suite** — PASS.
- [ ] **Step 5: Commit** — `git commit -am "perf: cache match results; atomic compact cache writes"`.

---

### Task 4: UI event queue, coalesced rendering, filename index, logging

**Files:**
- Modify: `extractor.py` — `LogRedirector`, `ExtractorApp.__init__`, `add_or_update_candidate`, `handle_upload_starting/success/failure`, `update_upload_progress_ui`, `retry_failed_uploads`, `start_extraction` (reset), every `self.after(0, ...)` / counter mutation inside `async_extraction_pipeline`
- Test: `test_extractor.py` (setUp helper + new `TestUiQueue` class)

**Interfaces:**
- Produces (all on `ExtractorApp`):
  - `self.ui_events: queue.Queue`, `self._table_dirty: bool`, `self._progress_dirty: bool`, `self.candidates_by_file: dict[str, dict]`
  - `_post(self, fn: Callable[[], None]) -> None` — `self.ui_events.put(fn)`; the only thing background threads call.
  - `_drain_ui_events(self) -> None` — main thread; runs all queued callables (each in try/except that prints the error), then at most one `filter_table()` and one `update_upload_progress_ui()` depending on the dirty flags, clears them, reschedules itself with `self.after(100, self._drain_ui_events)` in a `finally`.
  - `_record_upload_counts(self, total: int = 0, failed: int = 0) -> None` — main thread; bumps counters, sets `_progress_dirty`.
  - `add_or_update_candidate` and the three upload handlers: main-thread only, mutate data via `candidates_by_file`, set dirty flags, never call `after`/`filter_table`. Upload handlers silently return if the filename is not in `candidates_by_file`.
  - `update_upload_progress_ui`: updates labels directly (inner `after` wrapper removed).

- [ ] **Step 1: Update `TestExtractor.setUp`** — set `self.app._post = lambda fn: fn()`, `self.app.candidates_by_file = {}`, bind real `_record_upload_counts`; replace the `mock_add_or_update` stub with the real bound `add_or_update_candidate` (plus `_table_dirty`/`_progress_dirty = False`).
- [ ] **Step 2: Write failing tests (`TestUiQueue`, using a `MagicMock(spec=ExtractorApp)` with real `_post`, `_drain_ui_events`, `add_or_update_candidate`, handlers bound, `filter_table` and `update_upload_progress_ui` mocked, `after` mocked as no-op):**
  - `test_many_updates_one_render`: post 50 `add_or_update_candidate` calls, drain once → `filter_table.call_count == 1`, `len(extracted_data) == 50`, `len(candidates_by_file) == 50`.
  - `test_update_reuses_index`: add then update same filename → one row in `extracted_data`, updated values visible in both structures.
  - `test_late_upload_event_after_reset_ignored`: reset `extracted_data`/`candidates_by_file`, then post `handle_upload_success("old.pdf", "url")` and drain → no exception, `completed_uploads` unchanged.
  - `test_drain_reschedules`: after drain, `after.assert_called_with(100, app._drain_ui_events)`.
  - `test_callable_error_does_not_stop_drain`: first posted fn raises, second appends to a list → list has the item.
  - `test_log_redirector_line_buffered`: `LogRedirector(path)`; two threads each write 200 lines `f"t{i}-{n}\n"`; every line in the file matches `^t\d-\d+$`.
- [ ] **Step 3: Run** — expect FAIL.
- [ ] **Step 4: Implement** — as per Interfaces. In the pipeline: every `self.add_or_update_candidate(x)` → `self._post(lambda x=x: self.add_or_update_candidate(x))`; every `self.total_uploads += 1; self.failed_uploads += 1; self.update_upload_progress_ui()` → `self._post(lambda: self._record_upload_counts(total=1, failed=1))` (or `total=1` alone where only total was bumped); every `self.after(0, lambda...: self.lbl_status...)`/`set_matching_progress` → `self._post(...)`; `extraction_finished` and error-path calls likewise. In `upload_worker_loop`, calls to `handle_upload_starting/success/failure` become `self._post(lambda f=filename, ...: self.handle_upload_...(f, ...))`. In `__init__` initialise the new attributes and call `self.after(100, self._drain_ui_events)`. In `start_extraction` reset `candidates_by_file` alongside `extracted_data`. `LogRedirector`: `open(filepath, "a", encoding="utf-8", buffering=1)`, a `threading.Lock` around `write`, no per-write flush.
- [ ] **Step 5: Run full suite** — PASS (existing pipeline tests now exercise real `add_or_update_candidate` through the immediate `_post`).
- [ ] **Step 6: Manual check (Windows)** — launch `python extractor.py`, run a 3-file folder; table populates, progress labels update, no Tcl errors in `logs/app.log`.
- [ ] **Step 7: Commit** — `git commit -am "perf: main-thread UI queue with coalesced table refresh"`.

---

### Task 5: Table state preservation

**Files:**
- Modify: `extractor.py` — `add_to_tree`, `sort_table`, `filter_table`, `on_row_double_click`
- Test: `test_extractor.py` (new `TestTableState`)

**Interfaces:**
- Consumes: `candidates_by_file` (Task 4).
- Produces: `self._sort_state: Optional[tuple[str, bool, bool]]` = `(col, numeric, descending)`; `_apply_sort(self) -> None` reorders tree children per `_sort_state` without toggling; `sort_table(col, numeric)` toggles direction for the same column (first click on a new column = descending, matching current behaviour of `sort_descending` starting value), stores state, calls `_apply_sort`.

- [ ] **Step 1: Write failing tests** — use a real `ttk.Treeview` on a withdrawn `tk.Tk()` (skip the class with `unittest.skipUnless` if `tk.Tk()` raises `TclError`), app = `MagicMock(spec=ExtractorApp)` with real `add_to_tree`, `filter_table`, `sort_table`, `_apply_sort`, `on_row_double_click` bound; `sv_search.get` → `""`, `sv_min_score.get` → `""`; rows valid per `is_valid_candidate(allow_pending=True)`.
  - `test_sort_survives_refresh`: rows with MatchPercentage 10, 90, 50; `sort_table("MatchPct", True)`; append a row with 70; `filter_table()` → `MatchPct` column order `90, 70, 50, 10`.
  - `test_selection_survives_refresh`: select iid `"b.pdf"`, `filter_table()` → `tree.selection() == ("b.pdf",)`.
  - `test_scoring_placeholder_does_not_crash`: a row with `MatchPercentage="Scoring…"` and min score `"0"` → `filter_table()` runs, row present.
  - `test_double_click_uses_iid`: two rows sharing an email; select the second's iid; `on_row_double_click(None)` → `show_candidate_details` called with the second row's dict.
- [ ] **Step 2: Run** — expect FAIL.
- [ ] **Step 3: Implement** — `iid=row["SourceFilename"]` in `add_to_tree` (fall back to tree-generated iid when missing); `filter_table` saves `selection()` and `yview()[0]`, rebuilds, `_apply_sort()`, restores selection for iids that still exist and `yview_moveto`; replace the inline `float(match_pct)` with `safe_match_percentage(row)`; double-click resolves `self.candidates_by_file.get(iid)`.
- [ ] **Step 4: Run full suite** — PASS.
- [ ] **Step 5: Commit** — `git commit -am "fix: keep sort, selection and scroll across table refresh"`.

---

### Task 6: Hash once and queue uploads before scoring

**Files:**
- Modify: `extractor.py` — `read_worker` and hashing loop in `async_extraction_pipeline`, pre-filter loop, `run_match_task`, `retry_failed_uploads`
- Test: `test_extractor.py` (update `test_direct_mode_success`, `test_batch_mode_success` queue assertions)

**Interfaces:**
- Consumes: `_post`, `_record_upload_counts` (Task 4).
- Produces: `read_worker` returns `(filename, clean_text, file_hash)`; upload job tuple is `(filename: str, filepath: str, file_hash: Optional[str])`; placeholder `MatchPercentage = "Scoring…"`.

- [ ] **Step 1: Update/write failing tests:**
  - In `test_direct_mode_success`, expected queued item becomes `("resume1.pdf", os.path.join("dummy_folder", "resume1.pdf"), dummy_md5("resume1.pdf"))`.
  - `test_upload_queued_before_match`: make `mock_eval_match_async` side effect assert `self.app.upload_queue.qsize() == 1` at call time (record a flag), return the usual dict → flag is True.
  - `test_hash_computed_once_per_file`: with 3 files (one unreadable), `mock_get_file_hash.call_count == 3` (once per file, not once in reading and again for upload).
  - `test_retry_failed_uploads_passes_none_hash`: row with `ResumeLink="Failed"` → queued item's third element is `None`.
- [ ] **Step 2: Run** — expect FAIL.
- [ ] **Step 3: Implement** — hash inside `read_worker` (still before reading; unreadable files still get a hash); remove the separate `file_hashes` dict comprehension; after the similarity pre-filter, for each surviving resume post `add_or_update_candidate({...cand_data, "SourceFilename": filename, "ResumeLink": "Pending", "MatchPercentage": "Scoring…"})`, post `_record_upload_counts(total=1)`, and `upload_queue.put((filename, filepath, resume["file_hash"]))`; remove the counter bump and `put` from `run_match_task`; `retry_failed_uploads` puts `(filename, filepath, None)`.
- [ ] **Step 4: Run full suite** — PASS.
- [ ] **Step 5: Commit** — `git commit -am "perf: hash once and start uploads before match evaluation"`.

---

### Task 7: Parallel upload workers with per-thread Drive services

**Files:**
- Modify: `drive_uploader.py` — split credential logic out of `get_drive_service`, remove `upload_lock` from `_upload_single_file`, per-worker service in `upload_all_resumes`
- Modify: `extractor.py` — `start_upload_worker`, `upload_worker_loop`
- Test: `test_extractor.py` (`TestDriveUploader` + new `TestUploadPool`)

**Interfaces:**
- Consumes: job tuple `(filename, filepath, file_hash)` (Task 6); `atomic_write_json` (Task 3); `_post` (Task 4).
- Produces:
  - `drive_uploader.get_drive_credentials(force_reauth: bool = False) -> Credentials` (existing token/refresh/OAuth logic, unchanged behaviour).
  - `drive_uploader.get_drive_service(force_reauth: bool = False, creds=None)` → `build('drive', 'v3', credentials=creds or get_drive_credentials(force_reauth), cache_discovery=False)`.
  - `extractor.UPLOAD_WORKERS = 4`, `extractor.DRIVE_CACHE_SAVE_EVERY = 10`.
  - `ExtractorApp` attributes: `self._drive_creds`, `self._drive_creds_lock: threading.Lock`, `self._drive_cache: dict`, `self._drive_cache_lock: threading.Lock`, `self._uploads_since_save: int`.
  - `ExtractorApp._get_shared_drive_creds(self)` — under `_drive_creds_lock`, fetch once via `get_drive_credentials()` and memoise.
  - `ExtractorApp._save_drive_cache(self, force: bool = False)` — under `_drive_cache_lock`, `atomic_write_json(DRIVE_CACHE_FILE, ...)` when `force` or `_uploads_since_save >= DRIVE_CACHE_SAVE_EVERY`; resets the counter.

- [ ] **Step 1: Write failing tests:**
  - `test_upload_workers_run_concurrently`: `_upload_single_file` patched to sleep 0.2s; enqueue 8 jobs; start pool; `upload_queue.join()` finishes in < 0.8s (serial would be 1.6s).
  - `test_each_worker_has_own_service`: patch `get_drive_service`; after 8 jobs, it was called with `creds=` 4 times (once per worker), never more.
  - `test_credentials_fetched_once`: patch `get_drive_credentials` with a 0.1s sleep; start 4 workers on 8 cache-miss jobs → called exactly once.
  - `test_drive_cache_batched`: patch `atomic_write_json`; 25 successful uploads → called 2 times from the every-10 rule plus once when the queue drains (total 3); final cached dict has 25 entries.
  - `test_no_global_upload_lock`: `assertFalse(hasattr(drive_uploader, "upload_lock"))`.
  - `test_cache_hit_skips_upload`: preload `_drive_cache[hash] = "url"`; job with that hash → `_upload_single_file` not called, success handler posted with `"url"`.
- [ ] **Step 2: Run** — expect FAIL.
- [ ] **Step 3: Implement** — `drive_uploader`: extract `get_drive_credentials`, add `creds` param, delete `upload_lock` and the `with upload_lock:` wrappers (keep retry/backoff/root fallback), in `upload_all_resumes` give each worker thread its own service (`threading.local`) and replace its cache write with `atomic_write_json`. `extractor`: `start_upload_worker` loads `drive_cache.json` once into `_drive_cache`, starts `UPLOAD_WORKERS` daemon threads on `upload_worker_loop`; each worker builds its service lazily on first cache miss from `_get_shared_drive_creds()`; handlers go through `_post`; after `task_done`, if `upload_queue.unfinished_tasks == 0` call `_save_drive_cache(force=True)`. On credential failure the job is marked failed (as today) and the next job retries credentials.
- [ ] **Step 4: Run full suite** — PASS, including the updated `test_upload_all_resumes_caching_and_parallel`.
- [ ] **Step 5: Manual check (Windows)** — run a 10-file folder with an empty `drive_cache.json`; log shows overlapping `Upload started` lines; all rows end `Uploaded`; links open.
- [ ] **Step 6: Commit** — `git commit -am "perf: 4 parallel Drive upload workers, batched atomic cache"`.

---

### Task 8: One Word instance for .doc files

**Files:**
- Modify: `extractor.py` — `extract_text_from_doc`, read phase of `async_extraction_pipeline`
- Test: `test_extractor.py` (`TestLegacyDocSupport`)

**Interfaces:**
- Produces:
  - `extract_text_from_doc(filepath, word=None)` — if `word` is given, use it and do not `Quit()` or `CoInitialize`; otherwise current behaviour.
  - `extract_doc_batch(filepaths: List[str]) -> Dict[str, Optional[str]]` — `CoInitialize`, one `Word.Application`, extract each (per-file LibreOffice fallback inside `extract_text_from_doc`), `Quit` + `CoUninitialize` in `finally`; if COM import/creation fails, calls `extract_text_from_doc(p)` per file.
  - Read phase: `.doc` paths go to one extra future running `extract_doc_batch`, submitted to the same executor as the per-file reads; results are preprocessed and hashed like other files and merged into `results_dict`.

- [ ] **Step 1: Write failing tests:**
  - `test_doc_batch_creates_word_once`: patch `comtypes.client.CreateObject` (and `pythoncom`) — 5 paths → `CreateObject` called once, `Quit` once, result has 5 keys.
  - `test_doc_batch_falls_back_without_com`: `CreateObject` raises → `extract_text_from_doc` called once per path without `word`.
  - Existing three `TestLegacyDocSupport` tests still pass unchanged.
- [ ] **Step 2: Run** — expect FAIL.
- [ ] **Step 3: Implement** per Interfaces.
- [ ] **Step 4: Run full suite** — PASS.
- [ ] **Step 5: Commit** — `git commit -am "perf: reuse one Word instance for .doc extraction"`.

---

### Task 9: Stability and smoothness hardening

**Files:**
- Modify: `extractor.py` — `run_match_task`, progress calls in `async_extraction_pipeline`, resume-cache write step, `ExtractorApp.__init__`, `_drain_ui_events`, `extraction_finished`, `evaluate_candidate_match_async` timeout
- Test: `test_extractor.py` (new `TestStability`)

**Interfaces:**
- Consumes: `_post`, `_drain_ui_events` (Task 4); `match_cache_key`, `atomic_write_json` (Task 3); `_save_drive_cache` (Task 7).
- Produces:
  - `PROGRESS_PHASES = {"read": (0.0, 0.20), "embed": (0.20, 0.30), "extract": (0.30, 0.40), "match": (0.40, 1.00)}`
  - `def phase_progress(phase: str, done: int, total: int) -> float` — `lo + (hi - lo) * (done / total)`, `lo` when `total == 0`.
  - `CACHE_CHECKPOINT_EVERY = 25`; a `threading.Lock` (`cache_write_lock`, local to the pipeline run) around every `resume_cache.json` write.
  - `ExtractorApp.on_close(self) -> None` and `self._closing: bool` (drain loop does not reschedule when `True`).
  - `def format_skipped_files(skipped: list[tuple[str, str]], limit: int = 20) -> str` — module-level; used by `extraction_finished`.

- [ ] **Step 1: Write failing tests:**
  - `test_match_task_exception_isolated`: in the `test_direct_mode_success` setup with two valid resumes, make `mock_eval_match_async` raise `RuntimeError("boom")` for the first filename and return the normal dict for the second → run completes, `extraction_finished` called, first row `MatchPercentage == 0` and `Summary.startswith("Evaluation Failed")`, second row `MatchPercentage == 85`.
  - `test_phase_progress`: `phase_progress("match", 0, 10) == 0.40`, `phase_progress("match", 5, 10) == 0.70`, `phase_progress("match", 10, 10) == 1.00`, `phase_progress("embed", 0, 0) == 0.20`.
  - `test_progress_monotonic`: record every value passed to `set_matching_progress` during a pipeline run → non-decreasing, last value `1.0`, at least one value strictly between 0.40 and 1.0.
  - `test_cache_checkpointed_midrun`: 60 Stage-2 candidates (set `STAGE2_TOP_N` patch to 60), patch `atomic_write_json` with a recorder → ≥ 3 writes to `RESUME_CACHE_FILE` (2 checkpoints + final), and the 2nd-checkpoint payload has ≥ 50 `match_cache` entries.
  - `test_on_close_flushes_and_stops_drain`: `on_close()` → `_save_drive_cache` called with `force=True`, `destroy` called; subsequent `_drain_ui_events()` does not call `after`.
  - `test_match_timeout_60`: `evaluate_candidate_match_async` passes `timeout=60` to `parse`.
  - `test_skipped_files_truncated`: 300 entries → output contains exactly 20 filenames and `"…and 280 more (see processing_summary.csv)"`; 5 entries → no "more" line.
- [ ] **Step 2: Run** — expect FAIL.
- [ ] **Step 3: Implement** — wrap the whole `run_match_task` body in `try/except Exception as e` producing the failure dict (spec 4.1) and still posting the row update; replace every `set_matching_progress` / status post in the pipeline with `phase_progress(...)` values, incrementing a `matches_done` counter after each `run_match_task` and posting `"Evaluating matches (k/n)..."`; checkpoint `resume_cache` every `CACHE_CHECKPOINT_EVERY` completed evaluations under the lock (the final write takes the same lock); register `self.protocol("WM_DELETE_WINDOW", self.on_close)` in `__init__`; set match `timeout=60`; use `format_skipped_files` in `extraction_finished`.
- [ ] **Step 4: Run full suite** — PASS.
- [ ] **Step 5: Manual check (Windows)** — during a 50+ file run: the bar keeps moving during "Evaluating matches", resizing/scrolling the window stays smooth; close the window mid-run, relaunch, re-run → already-evaluated candidates are cache hits (fewer `Match Evaluation` calls in the log).
- [ ] **Step 6: Commit** — `git commit -am "fix: isolate per-candidate failures, phased progress, cache checkpoints, clean close"`.

---

### Task 10: End-to-end verification

**Files:** none (verification only)

- [ ] **Step 1: Full suite** — test command; all pass except recorded baseline failures.
- [ ] **Step 2: Real run, cold (Windows, user's key)** — clear `resume_cache.json` match section only by using a fresh JD edit, run a large folder (aim for 100–500 resumes); capture the `PERFORMANCE PROFILING REPORT` from `logs/app.log`; confirm no `429` storms (count `OpenAI call failed` lines) and the UI stays responsive (resize/scroll during run).
- [ ] **Step 3: Real run, warm** — re-run same folder + JD + model; `Match Evaluation Time` is near 0 and no `Upload started` lines (all Drive cache hits).
- [ ] **Step 4: Report** — compare against baseline (35.06s / 3 resumes, 28.09s match) and record numbers in the final summary to the user.
