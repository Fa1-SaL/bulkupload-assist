# Bulk Upload UI Simplification Implementation Plan

> **For agentic workers:** Execute inline using superpowers:executing-plans. The user has authorized implementation of the reviewed UI recommendations.

**Goal:** Make the existing dark desktop app simple and predictable while retaining parsing, matching, uploads, retry/cancel, candidate details, and CSV export.

**Architecture:** Retain CustomTkinter, ttk.Treeview, the existing worker queues, and incremental row updates. Keep all nine data columns internally, show a compact default set, and put full information in reusable candidate details. Folder preview is read-only; only the primary button starts processing. Derive terminal status from both pipeline completion and outstanding uploads.

**Tech Stack:** Python, CustomTkinter, tkinter/ttk, unittest.

**Spec:** User-approved recommendations in this chat, implementation request of 2026-10-27.

## Global Constraints
- Keep the dark theme and existing functionality.
- Preserve existing uncommitted changes; do not commit unrelated work.
- No API calls or uploads during folder preview or UI verification.
- Preserve selection, scroll position, sorting, filtering, and complete CSV data.

## Review Focus
- Folder/key edits must never process or upload files, including stale folder previews.
- Parsing completion with queued uploads must remain nonterminal.
- Failure, cancellation, retry, and no exportable results must have accurate state.
- Compact table must preserve hidden data, accessible details, and sorting.
- New runs must not discard unfinished uploads; stale events must remain guarded.

### Task 1: Explicit processing
- [x] Replace automatic-run tests with preview-only behavior and explicit-button integration coverage; run to observe failures.
- [x] Debounce background folder preview only; start pipeline only through Process resumes.
- [x] Run loading-flow tests.

### Task 2: Accurate completion
- [x] Add regression tests for pending uploads, final success, partial failures, cancellation/retry, and pipeline failure.
- [x] Track pipeline outcome and render completion only when uploads settle; prevent new runs during uploads. Export only eligible completed candidates.
- [x] Retain detailed processing statistics in on-demand activity instead of a completion modal. Update existing presentation tests without reducing statistics coverage.
- [x] Run outcome and existing pipeline/upload regression tests.

### Task 3: Quiet dark UI and usable results
- [x] Add real-window coverage for compact columns, full details, and source-to-export interaction.
- [x] Compact table with optional all-column view and both scrollbars. Add View details and Enter support; reuse one details window with full contact/resume information.
- [x] Move API/model into Settings, make JD collapsible, use compact progress and consistent neutral secondary controls; keep full activity details on demand.
- [x] Run full unittest discovery, compile checks, and visual verification with synthetic data; review diff.

## Execution notes
- Baseline: 152 tests pass with .venv314/bin/python -m unittest discover -q.
- Working in the supplied checkout preserves current uncommitted fixes and the user's runnable app. No commits or branch changes are required.

## Verification results
- Final suite: 160 tests passed with `.venv314/bin/python -m unittest discover -q`.
- Python compilation and `git diff --check` passed.
- New workflow tests were observed failing before implementation; the activity clipping regression was separately reproduced RED then verified GREEN.
- Independent review identified Activity expansion clipping primary controls at 1200×700. Fixed with a reusable nonmodal activity window; actual-Tk test confirms table height and primary-action visibility remain stable.
- Visually checked the final dark layout using synthetic candidates and no live API or Drive workers.
- Retained retry while other uploads are in flight, matching existing behavior; new runs are blocked only while processing or uploads remain pending.
- Existing uncommitted source/uploader changes preserved; no commits or remote changes made.

## Follow-up UI polish
- Larger default window: 1440×900, capped to the current screen.
- Slim 8 px scrollbars replace the thick native arrowed controls; hidden when content fits.
- Removed Candidates label; search uses an italic ghost hint with normal typed text.
- JD matching controls minimum-score filter visibility and application; preserves the value when toggled.
- Regression coverage added for real search/filter behavior and scrollbar overflow visibility.
- Follow-up verification: 163 tests passed; compilation and whitespace checks passed. Final screenshot refreshed with JD matching off and no unnecessary scrollbars.
