# Owner-operated resume uploads

Every desktop user sends resumes to one Cloud Run service in Google Cloud
project `turingxtractor`. Cloud Run validates a personal access code and
forwards the file to a private bridge implemented as an owner-run Google Apps
Script web app. The script checks a signed request, verifies that its effective
account is `faisalalam73@gmail.com`, and writes only to folder
`1EhV9pbEZjZg2_D7Tg2Z-zlsFK8DY06Ne`.

The owner has no domain. The existing Google OAuth app in `turingxtractor` is
in **Testing**, and [Google says its refresh tokens expire after seven days](https://developers.google.com/identity/protocols/oauth2#expiration)
for this kind of access. The Apps Script bridge executes as its owner and
doesn't distribute or store the owner's refresh token in Cloud Run or the
desktop app. It does add Apps Script execution limits, so validate the expected
resume volume before full rollout.

## Owner setup

1. In a private local terminal, generate a high-entropy bridge secret:

   ```bash
   python -m upload_service.provision bridge-secret --output bridge_secret.txt
   ```

   Keep the file private and out of Git. Do not send its contents in chat.

2. In [Apps Script](https://script.google.com/home) while signed in as
   `faisalalam73@gmail.com`, create a standalone project. Paste
   `upload_service/owner_bridge.gs` into `Code.gs`. Set its manifest from
   `upload_service/appsscript.json`.
3. In the project's **Script properties**, add `BRIDGE_SHARED_SECRET` with the
   contents of `bridge_secret.txt`. A credential must be entered by the owner
   in the Google UI; do not paste it into source code.
4. Deploy as a **Web app**, **Execute as me**, access **Anyone** (including
   callers without Google sign-in). The manifest specifies the corresponding
   `USER_DEPLOYING` and `ANYONE_ANONYMOUS` values. Authorize the
   requested Drive access as the owner. The public endpoint is protected by the
   signed, short-lived requests that only Cloud Run can make. Copy the `/exec`
   deployment URL privately.
5. In Google Cloud project `turingxtractor`, enable Cloud Run, Cloud Build, and
   Secret Manager APIs, and confirm billing. Create a dedicated Cloud Run
   runtime service account. Give it Secret Manager access to only the three
   secrets below.

Create Secret Manager secrets:

| Secret | Value | Cloud Run environment name |
| --- | --- | --- |
| `bulk-upload-bridge-url` | Apps Script `/exec` URL | `APPS_SCRIPT_URL` |
| `bulk-upload-bridge-secret` | Contents of `bridge_secret.txt` | `BRIDGE_SHARED_SECRET` |
| `bulk-upload-codes` | Contents of `upload_codes.json` | `UPLOAD_ACCESS_CODE_HASHES` |

Pin Cloud Run to numbered secret versions. Deploy **only** the `upload_service/`
directory as source; its Dockerfile builds the service without desktop files,
resumes, caches, or local secrets. Set memory to at least 1 GiB, concurrency
to 4, and request timeout to at least 600 seconds. Cloud Run must allow
unauthenticated *infrastructure* invocation so desktop clients can reach it;
the application itself requires a personal bearer code on `/v1/check` and
`/v1/uploads`. `/health` exposes only readiness.

For a first deployment, run these commands from the repository root in a
terminal with the Google Cloud CLI signed in as the project owner. Replace the
placeholder URL in the first command with the actual Apps Script `/exec` URL.
The private files are ignored by Git. The generated bridge secret may contain
a final newline; the service removes only trailing line endings before signing.
Create the first personal code before these commands so `upload_codes.json`
exists (see **User access codes** below).

```bash
printf '%s' 'PASTE_APPS_SCRIPT_EXEC_URL' > bridge_url.txt
gcloud config set project turingxtractor
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com iam.googleapis.com
gcloud iam service-accounts create bulk-upload-run \
  --display-name='Bulk upload service runtime'
gcloud secrets create bulk-upload-bridge-url --replication-policy=automatic \
  --data-file=bridge_url.txt
gcloud secrets create bulk-upload-bridge-secret --replication-policy=automatic \
  --data-file=bridge_secret.txt
gcloud secrets create bulk-upload-codes --replication-policy=automatic \
  --data-file=upload_codes.json
for secret in bulk-upload-bridge-url bulk-upload-bridge-secret bulk-upload-codes; do
  gcloud secrets add-iam-policy-binding "$secret" \
    --member='serviceAccount:bulk-upload-run@turingxtractor.iam.gserviceaccount.com' \
    --role='roles/secretmanager.secretAccessor'
done
gcloud run deploy bulk-upload-assist --source upload_service \
  --region asia-south1 \
  --service-account bulk-upload-run@turingxtractor.iam.gserviceaccount.com \
  --memory 1Gi --concurrency 4 --timeout 600 \
  --set-secrets 'APPS_SCRIPT_URL=bulk-upload-bridge-url:1,BRIDGE_SHARED_SECRET=bulk-upload-bridge-secret:1,UPLOAD_ACCESS_CODE_HASHES=bulk-upload-codes:1' \
  --allow-unauthenticated
```

Google's [source deployment guide](https://docs.cloud.google.com/run/docs/deploying-source-code)
confirms that a Dockerfile in the source directory is used. The
[secrets guide](https://docs.cloud.google.com/run/docs/configuring/services/secrets)
recommends numbered versions for environment variables. Confirm billing and
the selected region in Cloud Run before deploying.

## User access codes

Generate one high-entropy code for each person. This creates a private JSON
mapping from user labels to code hashes; the command displays the new code
once. Give only that person's code to them privately. They enter it in
Settings → Connections; the desktop app stores it in the native OS credential
store.

```bash
python -m upload_service.provision new-code alice --output upload_codes.json
```

To add a user, create a new file and replace the prior hash list after review:

```bash
python -m upload_service.provision new-code bob \
  --current upload_codes.json --output upload_codes.next.json
mv upload_codes.next.json upload_codes.json
```

To revoke a user, remove their entry from the hash list, upload a new secret
version, and redeploy Cloud Run pinned to that version. Code plaintext cannot
be recovered from the hash.

## Verify before switching clients

1. Confirm the Apps Script deployment runs as the owner's account and can
   create a **synthetic** test file in the fixed folder.
2. Confirm Cloud Run `/v1/check` rejects a missing/invalid code and accepts
   an issued code; `/v1/uploads` rejects oversized requests.
3. Upload one synthetic resume end to end. Verify its owner, destination,
   returned link, and sharing permission in Drive. Remove the test file after
   verifying it, using normal Drive trash/recovery.
4. Set `UPLOAD_SERVICE_URL` in `drive_uploader.py` to the verified HTTPS Cloud
   Run URL and publish the client update. The URL is public configuration, not
   a credential. Users must never configure their own Google Drive account.

## Limits and security

- The desktop client ignores old `token.pickle` and saved `drive_folder_id`
  values. Old cached links from direct, per-user uploads are not reused.
- Cloud Run accepts up to 24 MiB per resume, below its [32 MiB HTTP/1 request
  limit](https://docs.cloud.google.com/run/quotas#networking_limits). It signs
  the file with an HMAC, timestamp, and nonce before forwarding to Apps Script.
- Apps Script verifies owner identity, the signature and timestamp, and uses a
  nonce cache to reject a replay. It gives the uploaded file the same
  anyone-with-link reader permission as the previous desktop uploader.
- The owner must keep the Apps Script deployment and its Script property
  private. Rotate the bridge secret in both Apps Script and Secret Manager if
  it is exposed.
- Apps Script has a [six-minute execution limit and per-account quotas](https://developers.google.com/apps-script/guides/services/quotas).
  The full-size 24 MiB path needs a live synthetic test before relying on it.
