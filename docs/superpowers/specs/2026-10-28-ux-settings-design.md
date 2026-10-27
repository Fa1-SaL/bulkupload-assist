# UX workflow and expanded Settings

User-authorized implementation of the joint UX review, preserving the dark CustomTkinter desktop app and all existing processing/upload/export behavior unless explicitly changed here.

## Behavior
- Preserve extracted candidate data for low similarity. Show Not evaluated, never a zero score or upload failure. Provide Evaluate anyway; no automatic screening decisions.
- Expanded resizable Settings: General, Connections, Processing, Export; Save/Cancel with reversible text-size preview. Persist non-sensitive preferences separately. API-key persistence off by default and only through the OS credential store.
- Settings include scaling, workspace/folder restoration, smooth scrolling, reduced motion, default matching, recursive folder scan, model and connection checks, Drive account and destination, export folder/history warnings, Export Missing too (off), Regular export (off).
- Export Missing includes incomplete parsed records with blank missing fields/links; unreadable documents and pure extraction failures remain problems, not invented candidates.
- Regular CSV contains candidate data only, without Job ID, rate, availability, notes, full-time. Include Match % only when the batch actually used JD matching. Standard export retains its current schema and 99-row batches.
- Needs attention filter with reasons/actions and persistent manual corrections. Duplicate merges expose count and source filenames. Export scopes and previously generated CSV warnings are explicit; never silently exclude prior exports.
- Visible Drive destination, useful connection tests, operation snapshots so settings changes cannot redirect a running batch.
- Smooth vertical and horizontal scrolling; short opening animations with Reduce motion override. Uniform 36 px control heights and consistent action widths.
- Process action states: Process resumes, Processing/Uploading, Retry processing or Retry uploads on operational failures, Done on settled success. Clear batch appears only when idle after a batch and clears the current results/run state, never source files, Drive files, caches, corrections, or export history.
- Close guard only during active work; preserve existing cache reuse. No network requests in tests or previews.

## Approach
Retain the pipeline and main app. Add small focused modules for preferences/settings, interaction motion/scrolling, and candidate/export policy/history rather than extending the monolithic UI with unrelated persistence logic. Main app owns worker/event integration. Tests use actual Tk for interaction coverage and isolate all local state in temporary directories.

## Follow-up simplification (2026-10-28)

The user superseded the display and connection controls above: General now contains only remembered workspace and last-folder options. Text size is fixed at 100%, smooth scrolling and opening motion are built in with no toggles, and Connections contains only AI controls. Drive upload behavior and existing destination configuration remain. Settings wheel events are routed once by the active window; horizontal table scrolling animates in pixels rather than coarse Treeview units, including when native input targets window focus.
