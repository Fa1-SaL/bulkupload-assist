# Owner-run uploads without Cloud Run

The desktop app sends signed HTTPS requests straight to an Apps Script web app
deployed by `faisalalam73@gmail.com`. The script checks that it is running as
that account and writes resumes only to Drive folder
`1EhV9pbEZjZg2_D7Tg2Z-zlsFK8DY06Ne`. Users enter individual upload access
codes in Settings → Connections. No user receives the owner's Google OAuth
token, and this route does not require a Cloud Run service or a linked Cloud
billing account. The Apps Script project uses its Google-managed default Cloud
project.

The app owner keeps `bridge_secret.txt` locally and the same value in the
Apps Script `BRIDGE_SHARED_SECRET` property. Neither value nor any user code
belongs in Git or in a distributed app. A user code is derived from the owner
secret and a short user label; the desktop client signs each request with that
code. The code itself is never sent in the request.

## Owner setup

1. Create the owner secret once, if it does not already exist:

   ```bash
   python -m owner_upload.provision bridge-secret --output bridge_secret.txt
   ```

   Keep this file private. It is excluded by `.gitignore`.
2. In the owner [Apps Script project](https://script.google.com/home), put
   `owner_upload/owner_bridge.gs` in `Code.gs` and
   `owner_upload/appsscript.json` in the manifest. In Project Settings, save
   `BRIDGE_SHARED_SECRET` as a Script Property with the exact contents of
   `bridge_secret.txt` (without its final newline). The current owner project
   already has that property.
3. Deploy as a Web app, **Execute as me** and **Anyone**. Authorize the Drive
   access as the owner. The endpoint is anonymously reachable, but an upload
   requires a short-lived HMAC signature made with a personal code. The script
   checks the fixed account and folder before creating a file.
4. Issue a private code for each person. Choose a unique label for each
   person; `owner` is the first one. The command writes a file with mode 0600
   and prints only its path:

   ```bash
   python -m owner_upload.provision issue-code owner \
     --secret bridge_secret.txt --output owner_upload_code.txt
   ```

   Give that file's single line only to the named person through a private
   channel. They paste it into the Upload access code field. Issue the next
   person's code with a different label and output path.
5. Test a signed `check` request and a synthetic resume upload. Confirm the
   created file belongs to the owner, is in the fixed folder, and its returned
   link opens as intended. Put the verified Apps Script `/exec` URL as the
   single line of `upload_endpoint.txt` beside `extractor.py` on each staff
   computer. This URL is not a credential, but it is omitted from the public
   repository to reduce unwanted traffic to the script.

To revoke one person's code, add their label to `REVOKED_USERS` in
`owner_upload/owner_bridge.gs` and deploy a new script version. To rotate all
codes, replace the owner secret in the Script Property and reissue codes.

## Limits and recovery

- Apps Script has [per-user quotas and a six-minute execution limit](https://developers.google.com/apps-script/guides/services/quotas).
  The code caps raw files at 24 MiB, but the complete path must be tested with
  representative resume sizes before relying on that upper bound.
- A failed upload stays failed in the table and can be retried. Successful
  links are cached by file hash; old direct-upload links are ignored.
- The web app uses an HMAC signature, five-minute timestamp window, and nonce
  cache to reject unsigned or replayed requests. It accepts only PDF, DOCX,
  DOC, and TXT filenames. Each user code is kept in the native OS credential
  store on their computer.
- Do not distribute `token.pickle` or `oauth_credentials.json`. They are not
  required by this design and are ignored by Git.
