# Owner Drive upload service

## Goal

Every desktop user uploads resumes into the fixed folder owned by
`faisalalam73@gmail.com` through Cloud Run and an owner-run Apps Script bridge.
Users authenticate with individual access codes; they never receive the
owner's Google credentials and never choose another Drive destination.

## Work

1. Add a Cloud Run service with bounded streaming, per-user bearer-code
   validation, and signed forwarding to an owner-run Apps Script bridge.
   The script verifies its effective owner account and writes to the fixed folder.
2. Add a desktop HTTPS upload client, an upload access-code field backed by the
   native OS keyring, and clear missing-configuration errors. Remove legacy
   per-user Drive OAuth and saved folder overrides from the active path.
3. Namespace upload cache entries so old user-owned links cannot be reused.
4. Add tests for authentication, fixed destination, retries, errors, settings,
   and cache behavior. Run headless tests; avoid Tk tests that open the app over
   the user's work.
5. Document provisioning and deploy to `turingxtractor` only with the owner's
   Apps Script deployment and per-user code secrets available. Publish code only
   after the client points at a verified service URL.

## Constraints

- Never commit bridge credentials, Google tokens, or access codes; show newly
  issued access codes only to the owner in a private terminal.
- No live resume uploads during automated tests.
- Preserve cancellation, retry, progress, and export behavior.
