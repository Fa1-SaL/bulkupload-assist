# UX Settings and Batch Workflow Implementation Plan

> **For agentic workers:** Use test-driven development. Independent Settings and interaction modules are delegated under dispatching-parallel-agents; the main executor integrates the pipeline and export workflow.

**Goal:** Implement the approved UX improvements, export modes, and predictable batch lifecycle.
**Architecture:** Keep existing workers and main-thread events; split new settings, interaction, and export policy into focused modules.
**Tech Stack:** Python, CustomTkinter, ttk, unittest, optional OS credential-store integration via keyring.
**Spec:** docs/superpowers/specs/2026-10-28-ux-settings-design.md

## Global Constraints
- Keep dark theme, simple layout, and existing functionality.
- Export Missing too and Regular export default off.
- Regular export retains Match % only for batches using JD matching.
- Clear never deletes source/Drive files or persisted history/corrections.
- No live AI/Drive requests in verification; never store API keys in preferences.

## Review Focus
- Incomplete/unevaluated candidates must not become bogus upload failures or exports with invented values.
- Settings Save/Cancel and key storage failures must not partially apply new credentials.
- Active batches retain their source, JD/model, and Drive destination; stale events ignored after Clear.
- Export mode, scopes, counts, actual filenames, repeat warnings, and persisted history must agree.
- Scroll/animation callbacks must remain responsive and stop safely on destroyed windows.

### Task 1: Correctness and policy
- [x] Regression tests for low similarity preserving fields, no upload/retry failure, missing-data exports and batch-dependent regular columns.
- [x] Implement low-similarity state plus export policy/history helpers; run targeted tests.

### Task 2: Settings module (delegated)
- [x] Preference store, opt-in OS key storage, transactional categorized Settings UI, tests.
- [x] Integrate preferences, connection actions, recursive scanning, workspace restoration and active-run safeguards.

### Task 3: Attention and duplicate workflow
- [x] Attention reasons/filter, persistent edits, merged-source visibility and Evaluate anyway; test data survival across reruns.

### Task 4: Export modes and safeguards
- [x] Standard/regular/missing options, scope counts, repeat warnings, job selection, history and clearing controls; retain existing format by default.
- [x] Tests for all mode combinations, missing values, no invented records, repeated exports and matching on/off.

### Task 5: Interaction module (delegated)
- [x] Smooth 2-axis scrolling, reduced-motion opening animation, cancellation and focused tests.
- [x] Integrate on table/dialogs, keyboard focus, and consistent controls; verify minimum sizing visually.

### Task 6: Batch lifecycle
- [x] Process/processing/upload/retry/done, Clear batch, safe close, immutable active source and event generation; actual Tk regression coverage.

### Task 7: Verify and document
- [x] Full existing/new unittest discovery, compile/whitespace checks, synthetic visual walkthrough, independent review and fixes.
- [x] Update README for settings, export semantics, persistence, clear and motion behavior.

## Verification outcome

- Full suite: 228 tests passed (Python 3.14; 66.235 seconds).
- Compilation: extractor, Drive uploader, preferences, workflow policy and UI interaction modules passed.
- Whitespace: git diff --check passed.
- Actual Tk and synthetic native preview: dark table, Settings at 100%/130%, Save/Cancel, hidden-control restoration and settled-batch actions checked.
- Independent review: corrections before validation, key-storage rollback, folder validation, stale dialogs, connection locks, retry destination and export editing safeguards addressed.
- External AI/Drive requests were mocked; live credentials and source resumes were not used for validation. VoiceOver/NVDA compliance is not claimed.

## User follow-up: simplified Settings and scrolling

- Removed display-size, smooth-scrolling and reduce-motion controls. Normalization forces 100% sizing and built-in motion defaults, including for older saved preferences.
- Removed Drive controls from Connections while preserving existing upload configuration and functionality.
- Settings reuses its window and handles wheel input on its active page, including native window-focused input and scrollbar canvases.
- Table horizontal movement uses pixel-sized eased steps. Window-focused and scrollbar wheel input is routed once; scrollbar click/drag behavior remains native.
- Regression cases reproduced before the fixes. Final full suite: 235 tests passed in 54.163 seconds; portable wheel-input regression also passed. Python compilation and git diff --check passed.
