/** Owner-only Apps Script web app. Deploy as "Execute as me" under the owner account. */
const OWNER_EMAIL = 'faisalalam73@gmail.com';
const FOLDER_ID = '1EhV9pbEZjZg2_D7Tg2Z-zlsFK8DY06Ne';
const MAX_RAW_BYTES = 24 * 1024 * 1024;
// Add a user label here and redeploy to revoke only that person's code.
const REVOKED_USERS = [];

function reply_(value) {
  return ContentService.createTextOutput(JSON.stringify(value))
    .setMimeType(ContentService.MimeType.JSON);
}

function equal_(left, right) {
  if (typeof left !== 'string' || typeof right !== 'string') return false;
  let difference = left.length ^ right.length;
  const length = Math.max(left.length, right.length);
  for (let i = 0; i < length; i++) {
    difference |= (left.charCodeAt(i) || 0) ^ (right.charCodeAt(i) || 0);
  }
  return difference === 0;
}

function signedHex_(message, secret) {
  return Utilities.computeHmacSha256Signature(message, secret, Utilities.Charset.UTF_8)
    .map(byte => ('0' + (byte & 255).toString(16)).slice(-2)).join('');
}

function doPost(e) {
  try {
    // The deployed script's effective user must be the owner, not the caller.
    if (Session.getEffectiveUser().getEmail().toLowerCase() !== OWNER_EMAIL) {
      return reply_({status: 'error', code: 'owner_mismatch'});
    }
    const secret = PropertiesService.getScriptProperties().getProperty('BRIDGE_SHARED_SECRET');
    if (!secret || secret.length < 32) return reply_({status: 'error', code: 'unconfigured'});
    if (!e || !e.postData || !e.postData.contents || e.postData.length > 34 * 1024 * 1024) {
      return reply_({status: 'error', code: 'invalid_request'});
    }
    const input = JSON.parse(e.postData.contents);
    const {action, timestamp, nonce, user, name, mime, data, signature} = input;
    if ((action !== 'check' && action !== 'upload') ||
        typeof user !== 'string' || !/^[A-Za-z0-9_-]{1,40}$/.test(user) ||
        REVOKED_USERS.includes(user) ||
        typeof timestamp !== 'string' || !/^\d{10}$/.test(timestamp) ||
        Math.abs(Date.now() / 1000 - Number(timestamp)) > 300 ||
        typeof nonce !== 'string' || !/^[A-Za-z0-9_-]{16,80}$/.test(nonce) ||
        typeof signature !== 'string' || !/^[a-f0-9]{64}$/.test(signature)) {
      return reply_({status: 'error', code: 'invalid_request'});
    }
    if (action === 'upload' &&
        (typeof name !== 'string' || !name || name.length > 255 ||
         name === '.' || name === '..' || /[\\/\x00-\x1f\x7f]/.test(name) ||
         !/\.(pdf|docx|doc|txt)$/i.test(name) ||
         typeof mime !== 'string' || mime.length > 120 ||
         typeof data !== 'string' || !data || data.length > Math.ceil(MAX_RAW_BYTES / 3) * 4)) {
      return reply_({status: 'error', code: 'invalid_request'});
    }
    const userCode = signedHex_('bulk-upload-code-v1:' + user, secret);
    const canonical = action === 'check'
      ? [action, timestamp, nonce, user].join('\n')
      : [action, timestamp, nonce, user, name, mime, data].join('\n');
    if (!equal_(signature, signedHex_(canonical, userCode))) {
      return reply_({status: 'error', code: 'unauthorized'});
    }

    // Mark the nonce while holding a short lock. A replay cannot create a second file.
    const lock = LockService.getScriptLock();
    if (!lock.tryLock(10000)) return reply_({status: 'error', code: 'busy'});
    try {
      const cache = CacheService.getScriptCache();
      const key = 'upload_nonce_' + nonce;
      if (cache.get(key)) return reply_({status: 'error', code: 'replayed'});
      cache.put(key, '1', 600);
    } finally {
      lock.releaseLock();
    }

    const folder = DriveApp.getFolderById(FOLDER_ID);
    if (!folder.getOwner() || folder.getOwner().getEmail().toLowerCase() !== OWNER_EMAIL) {
      return reply_({status: 'error', code: 'folder_owner_mismatch'});
    }
    if (action === 'check') return reply_({status: 'ready'});
    const bytes = Utilities.base64Decode(data);
    if (!bytes.length || bytes.length > MAX_RAW_BYTES) {
      return reply_({status: 'error', code: 'invalid_size'});
    }
    const blob = Utilities.newBlob(bytes, mime, name);
    const file = folder.createFile(blob);
    file.setSharing(DriveApp.Access.ANYONE_WITH_LINK, DriveApp.Permission.VIEW);
    return reply_({status: 'ok', url: 'https://drive.google.com/file/d/' + file.getId() + '/view'});
  } catch (_) {
    return reply_({status: 'error', code: 'upload_failed'});
  }
}
