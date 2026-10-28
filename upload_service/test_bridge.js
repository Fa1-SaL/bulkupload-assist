// Headless tests for the Apps Script owner boundary; run with `node upload_service/test_bridge.js`.
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync(__dirname + '/owner_bridge.gs', 'utf8');
const folderId = '1EhV9pbEZjZg2_D7Tg2Z-zlsFK8DY06Ne';
const secret = 'a-test-secret-that-is-long-enough-for-the-script';
const created = [];
const seen = new Map();
let account = 'faisalalam73@gmail.com';
let folderOwner = 'faisalalam73@gmail.com';

const globals = {
  ContentService: {
    MimeType: {JSON: 'json'},
    createTextOutput: text => ({text, setMimeType() { return this; }}),
  },
  Session: {getEffectiveUser: () => ({getEmail: () => account})},
  PropertiesService: {getScriptProperties: () => ({getProperty: key =>
    key === 'BRIDGE_SHARED_SECRET' ? secret : null})},
  Utilities: {
    Charset: {UTF_8: 'utf8'},
    computeHmacSha256Signature: (message, key) => [...crypto.createHmac('sha256', key).update(message).digest()],
    base64Decode: data => [...Buffer.from(data, 'base64')],
    newBlob: (bytes, mime, name) => ({bytes, mime, name}),
  },
  LockService: {getScriptLock: () => ({tryLock: () => true, releaseLock() {}})},
  CacheService: {getScriptCache: () => ({get: key => seen.get(key), put: (key, value) => seen.set(key, value)})},
  DriveApp: {
    Access: {ANYONE_WITH_LINK: 'anyone'}, Permission: {VIEW: 'view'},
    getFolderById: id => {
      assert.equal(id, folderId);
      return {getOwner: () => ({getEmail: () => folderOwner}), createFile: blob => {
        created.push(blob);
        return {setSharing: (access, permission) => {
          assert.equal(access, 'anyone'); assert.equal(permission, 'view');
        }, getId: () => 'file123'};
      }};
    },
  },
  Date,
  JSON,
  Math,
};
vm.runInNewContext(source, globals, {filename: 'owner_bridge.gs'});

function send(overrides = {}) {
  const payload = {
    timestamp: String(Math.floor(Date.now() / 1000)),
    nonce: 'nonce-for-a-single-test-request',
    name: 'résumé.pdf', mime: 'application/pdf',
    data: Buffer.from('synthetic resume').toString('base64'),
    ...overrides,
  };
  const canonical = ['timestamp', 'nonce', 'name', 'mime', 'data'].map(key => payload[key]).join('\n');
  payload.signature = overrides.signature || crypto.createHmac('sha256', secret).update(canonical).digest('hex');
  const content = JSON.stringify(payload);
  return JSON.parse(globals.doPost({postData: {contents: content, length: content.length}}).text);
}

assert.deepEqual(send(), {status: 'ok', url: 'https://drive.google.com/file/d/file123/view'});
assert.equal(created.length, 1);
assert.equal(created[0].name, 'résumé.pdf');
assert.equal(send().code, 'replayed');
assert.equal(created.length, 1);
assert.equal(send({nonce: 'another-nonce-for-test-case', signature: '0'.repeat(64)}).code, 'unauthorized');
assert.equal(created.length, 1);
account = 'someone@example.com';
assert.equal(send({nonce: 'third-nonce-for-test-case'}).code, 'owner_mismatch');
assert.equal(created.length, 1);
account = 'faisalalam73@gmail.com';
folderOwner = 'someone@example.com';
assert.equal(send({nonce: 'fourth-nonce-for-test-case'}).code, 'folder_owner_mismatch');
assert.equal(created.length, 1);
folderOwner = 'faisalalam73@gmail.com';
assert.equal(send({nonce: 'fifth-nonce-for-test-case', name: 'payload.exe'}).code, 'invalid_request');
assert.equal(created.length, 1);
console.log('Apps Script bridge: 6 checks passed');
