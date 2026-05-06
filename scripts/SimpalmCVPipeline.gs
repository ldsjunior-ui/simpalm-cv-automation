// ═══════════════════════════════════════════════════════════════════════════
// Simpalm Staffing — CV Pipeline (Google Drive Edition)
// Drop a CV in the "PALMDECK - CVs" folder → branded PDF appears automatically
// ═══════════════════════════════════════════════════════════════════════════
//
// ARCHITECTURE:
//   1. Drop PDF/DOCX in the "PALMDECK - CVs" Google Drive folder
//   2. This script detects it → pushes to GitHub inbox/
//   3. GitHub Actions runs Python + WeasyPrint → branded Simpalm PDF (~2–3 min)
//   4. This script polls GitHub → downloads the branded PDF → saves to Drive
//      "✅ Processed CVs" subfolder
//   5. Serves index.json via Web App → PalmDeck reads it live
//
// ─── ONE-TIME SETUP ────────────────────────────────────────────────────────
//   1. script.google.com → New project → paste this file → Save
//   2. Project Settings (⚙) → Script Properties → Add property:
//        Name:  GITHUB_TOKEN
//        Value: <GitHub Personal Access Token with "repo" write scope>
//        → github.com/settings/tokens/new → check "repo" → Generate token
//   3. Deploy → New deployment → Type: Web app
//        Execute as: Me
//        Who has access: Anyone
//      → Copy the Web App URL
//      → Paste it into PalmDeck as CV_DRIVE_INDEX_URL
//   4. Run installTriggers() ONCE from the editor (Run menu → installTriggers)
//      This installs TWO triggers:
//        a) onChange  → fires the INSTANT a file is added to Drive
//        b) Every 5 min → backup poller to sync branded PDFs back from GitHub
// ═══════════════════════════════════════════════════════════════════════════

// ── Config ──────────────────────────────────────────────────────────────────

const DRIVE_FOLDER_ID  = '1Lg2C2Ij8GYz0In8GdRHHXGVlry4DQC5T';   // "PALMDECK - CVs"
const GH_OWNER         = 'ldsjunior-ui';
const GH_REPO          = 'simpalm-cv-automation';
const GH_BRANCH        = 'main';
const PROC_FOLDER_NAME = '✅ Processed CVs';
const INDEX_FILE_NAME  = 'palmdeck-index.json';
const PUSHED_KEY       = 'pushed_files_v2';
const SYNCED_KEY       = 'synced_files_v2';
const EXPIRY_DAYS      = 21;   // CVs older than this are automatically purged

const VALID_MIME_TYPES = [
  'application/pdf',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  'application/msword',
];

function getToken() {
  const t = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
  if (!t) throw new Error('GITHUB_TOKEN not set in Script Properties.');
  return t;
}

// ── Reliable GitHub fetch (replaces raw.githubusercontent.com) ───────────────
// Uses api.github.com with the auth token — different routing from GAS IPs,
// avoids the "Address unavailable" error that hits raw.githubusercontent.com.
// Retries 3× with exponential backoff on network-level exceptions.
function _ghApiFetch(path, options, retries) {
  retries = (retries === undefined) ? 3 : retries;
  const url = `https://api.github.com/repos/${GH_OWNER}/${GH_REPO}/contents/${path}?ref=${GH_BRANCH}`;
  const opts = Object.assign({
    headers: {
      Authorization: `token ${getToken()}`,
      Accept: 'application/vnd.github.v3+json',
    },
    muteHttpExceptions: true,
  }, options || {});

  for (let attempt = 0; attempt < retries; attempt++) {
    try {
      const res = UrlFetchApp.fetch(url, opts);
      return res;
    } catch (e) {
      if (attempt === retries - 1) throw e;
      Logger.log(`⚠️ GitHub fetch attempt ${attempt + 1} failed: ${e.message} — retrying…`);
      Utilities.sleep(2000 * Math.pow(2, attempt)); // 2s, 4s, 8s
    }
  }
}

// Fetch a file's raw content from GitHub (text or binary) via the Contents API.
// For JSON/text files: returns parsed string.
// For binary files: returns Blob.
function _ghGetFileContent(path, asBinary) {
  let res;
  try {
    res = _ghApiFetch(path, {
      headers: {
        Authorization: `token ${getToken()}`,
        Accept: asBinary ? 'application/vnd.github.v3.raw' : 'application/vnd.github.v3+json',
      },
      muteHttpExceptions: true,
    });
  } catch (e) {
    Logger.log(`⚠️ Could not reach api.github.com for "${path}": ${e.message}`);
    return null;
  }
  if (!res || res.getResponseCode() !== 200) return null;
  if (asBinary) return res.getBlob();
  // JSON response: { content: "<base64-with-newlines>", encoding: "base64", ... }
  const data = JSON.parse(res.getContentText());
  const raw  = Utilities.newBlob(
    Utilities.base64Decode(data.content.replace(/\n/g, ''))
  ).getDataAsString();
  return raw;
}

// ── Web App endpoint ─────────────────────────────────────────────────────────
// Supports three actions via ?action=<value>:
//   (default / 'index') → palmdeck-index.json  (used by PalmDeck CV picker)
//   'status'            → { uploaded[], processed[], ts }  (pipeline monitor)
//   'trigger'           → runs the full pipeline instantly, returns { ok, message }

function _jsonOut(data) {
  return ContentService
    .createTextOutput(JSON.stringify(data))
    .setMimeType(ContentService.MimeType.JSON);
}

function doGet(e) {
  const action = (e && e.parameter && e.parameter.action) || 'index';

  // ── TRIGGER ──────────────────────────────────────────────────────────────
  // Two modes:
  //   a) Selective — ?action=trigger&files=inbox/file1.pdf|inbox/file2.pdf
  //      Re-pushes specific inbox files → GitHub diff shows only those files
  //      → Actions workflow processes ONLY those CVs.
  //   b) Full     — ?action=trigger (no files param)
  //      Pushes .trigger file → workflow falls back to processing all inbox files.
  if (action === 'trigger') {
    try {
      const token   = getToken();
      const headers = { Authorization: `token ${token}`,
                        Accept: 'application/vnd.github.v3+json',
                        'Content-Type': 'application/json' };

      // ── Selective mode ────────────────────────────────────────────────────
      const rawFiles = (e && e.parameter && e.parameter.files) || '';
      if (rawFiles) {
        const files   = rawFiles.split('|').map(f => f.trim()).filter(Boolean);
        const pushed  = [];
        const failed  = [];

        for (const filePath of files) {
          const apiUrl = `https://api.github.com/repos/${GH_OWNER}/${GH_REPO}/contents/${encodeURIComponent(filePath).replace(/%2F/g, '/')}`;

          // GET current file (need SHA + content to re-push unchanged)
          const getRes = UrlFetchApp.fetch(apiUrl, { headers, muteHttpExceptions: true });
          if (getRes.getResponseCode() !== 200) {
            Logger.log(`⚠️ File not found on GitHub: ${filePath}`);
            failed.push(filePath);
            continue;
          }
          const fileData = JSON.parse(getRes.getContentText());
          const content  = (fileData.content || '').replace(/\n/g, '');  // base64 without newlines

          // Re-PUT the same content — creates a new commit that changes this inbox file
          // → GitHub Actions diff will include ONLY this file → processed selectively
          const putPayload = {
            message: `🔄 Reprocess: ${filePath.split('/').pop()} (${new Date().toISOString()})`,
            content,
            sha:     fileData.sha,
            branch:  GH_BRANCH,
          };
          const putRes = UrlFetchApp.fetch(apiUrl, {
            method: 'PUT', headers, payload: JSON.stringify(putPayload), muteHttpExceptions: true,
          });
          const code = putRes.getResponseCode();
          if (code === 200 || code === 201) {
            Logger.log(`⚡ Re-pushed to trigger reprocess: ${filePath}`);
            pushed.push(filePath);
          } else {
            Logger.log(`❌ Re-push failed (${code}) for ${filePath}: ${putRes.getContentText().slice(0,100)}`);
            failed.push(filePath);
          }
        }

        return _jsonOut({
          ok: pushed.length > 0,
          triggered: pushed.length > 0,
          pushed: pushed.length,
          failed: failed.length,
          message: pushed.length > 0
            ? `Reprocessing ${pushed.length} CV(s) via GitHub Actions. Ready in ~2–3 min.`
            : 'No files were pushed. Check that source_file is correct in index.json.',
        });
      }

      // ── Full mode (no specific files) ─────────────────────────────────────
      const apiUrl = `https://api.github.com/repos/${GH_OWNER}/${GH_REPO}/contents/inbox/.trigger`;

      let sha;
      const getRes = UrlFetchApp.fetch(apiUrl, { headers, muteHttpExceptions: true });
      if (getRes.getResponseCode() === 200) {
        sha = JSON.parse(getRes.getContentText()).sha;
      }

      const payload = {
        message: `⚡ Manual trigger from PalmDeck — process all inbox CVs (${new Date().toISOString()})`,
        content: Utilities.base64Encode(`trigger ${Date.now()}\n`),
        branch:  GH_BRANCH,
      };
      if (sha) payload.sha = sha;

      const putRes = UrlFetchApp.fetch(apiUrl, {
        method: 'PUT', headers, payload: JSON.stringify(payload), muteHttpExceptions: true,
      });
      const code = putRes.getResponseCode();
      if (code === 200 || code === 201) {
        Logger.log('⚡ Full trigger pushed to GitHub inbox/');
        return _jsonOut({ ok: true, triggered: true,
          message: 'Full pipeline triggered. All inbox CVs will be processed in ~2–3 min.' });
      } else {
        const err = putRes.getContentText().slice(0, 200);
        Logger.log(`❌ Trigger push failed (${code}): ${err}`);
        return _jsonOut({ ok: false, error: `GitHub push failed (${code}): ${err}` });
      }
    } catch (err) {
      return _jsonOut({ ok: false, error: err.message });
    }
  }

  // ── REMOVE_TRIGGERS ──────────────────────────────────────────────────────
  // One-shot: deletes all project triggers (stops the "Error code INTERNAL"
  // failure emails from time-based triggers that were never removed).
  // Call once: <webapp_url>?action=remove_triggers
  // After calling, this action can be left in place — it's harmless.
  if (action === 'remove_triggers') {
    try {
      const triggers = ScriptApp.getProjectTriggers();
      triggers.forEach(t => ScriptApp.deleteTrigger(t));
      Logger.log(`✅ Removed ${triggers.length} trigger(s). Pipeline is now manual-only.`);
      return _jsonOut({ ok: true, removed: triggers.length,
        message: `${triggers.length} trigger(s) deleted. No more automatic runs.` });
    } catch (err) {
      return _jsonOut({ ok: false, error: err.message });
    }
  }

  // ── LIST_INBOX ────────────────────────────────────────────────────────────
  // Returns all raw CV files currently in the Drive folder (processed or not).
  // Used by PalmDeck to populate the "Select CV to process" dropdown immediately
  // after a file is dropped in the folder — before any processing happens.
  if (action === 'list_inbox') {
    try {
      const folder = DriveApp.getFolderById(DRIVE_FOLDER_ID);
      const files  = folder.getFiles();
      const inbox  = [];
      while (files.hasNext()) {
        const f = files.next();
        if (!VALID_MIME_TYPES.includes(f.getMimeType())) continue;
        inbox.push({
          name:    f.getName(),
          id:      f.getId(),
          created: f.getDateCreated().toISOString(),
          size:    f.getSize(),
        });
      }
      // Newest first
      inbox.sort((a, b) => new Date(b.created) - new Date(a.created));
      return _jsonOut(inbox);
    } catch (err) {
      return _jsonOut({ error: err.message });
    }
  }

  // ── PUSH_AND_TRIGGER ──────────────────────────────────────────────────────
  // Reads ONE specific Drive file (by ID) and pushes it to GitHub inbox/.
  // Because the GH Actions workflow fires on push to inbox/**, this is enough
  // to trigger processing of ONLY that file — no other files are touched.
  if (action === 'push_and_trigger') {
    const fileId = e && e.parameter && e.parameter.file_id;
    if (!fileId) return _jsonOut({ ok: false, error: 'file_id required' });
    try {
      const driveFile = DriveApp.getFileById(fileId);
      const fileName  = driveFile.getName();
      const base64    = Utilities.base64Encode(driveFile.getBlob().getBytes());
      const token     = getToken();
      const apiUrl    = `https://api.github.com/repos/${GH_OWNER}/${GH_REPO}/contents/inbox/${encodeURIComponent(fileName)}`;
      const headers   = {
        Authorization:  `token ${token}`,
        Accept:         'application/vnd.github.v3+json',
        'Content-Type': 'application/json',
      };

      // If file already exists in GitHub, we need its SHA to update it
      let sha;
      const getRes = UrlFetchApp.fetch(apiUrl, { headers, muteHttpExceptions: true });
      if (getRes.getResponseCode() === 200) {
        sha = JSON.parse(getRes.getContentText()).sha;
      }

      const putPayload = {
        message: `📥 Process CV: ${fileName} (${new Date().toISOString()})`,
        content: base64,
        branch:  GH_BRANCH,
      };
      if (sha) putPayload.sha = sha;

      const putRes = UrlFetchApp.fetch(apiUrl, {
        method: 'PUT', headers, payload: JSON.stringify(putPayload), muteHttpExceptions: true,
      });
      const code = putRes.getResponseCode();
      if (code === 200 || code === 201) {
        Logger.log(`✅ Pushed to GitHub inbox for processing: ${fileName}`);
        return _jsonOut({ ok: true, filename: fileName,
          message: `${fileName} queued. Ready in ~2–3 min.` });
      } else {
        const err = putRes.getContentText().slice(0, 200);
        Logger.log(`❌ GitHub push failed (${code}): ${err}`);
        return _jsonOut({ ok: false, error: `GitHub push failed (${code}): ${err}` });
      }
    } catch (err) {
      return _jsonOut({ ok: false, error: err.message });
    }
  }

  // ── STATUS ───────────────────────────────────────────────────────────────
  if (action === 'status') {
    const folder   = DriveApp.getFolderById(DRIVE_FOLDER_ID);
    const files    = folder.getFiles();
    const uploaded = [];

    while (files.hasNext()) {
      const f = files.next();
      if (!VALID_MIME_TYPES.includes(f.getMimeType())) continue;
      uploaded.push({
        id:      f.getId(),
        name:    f.getName(),
        created: f.getDateCreated().toISOString(),
        size:    f.getSize(),
      });
    }

    const found     = folder.getFilesByName(INDEX_FILE_NAME);
    const processed = found.hasNext()
      ? JSON.parse(found.next().getBlob().getDataAsString())
      : [];

    return _jsonOut({ uploaded, processed, ts: new Date().toISOString() });
  }

  // ── INDEX (default) ──────────────────────────────────────────────────────
  const folder = DriveApp.getFolderById(DRIVE_FOLDER_ID);
  const found  = folder.getFilesByName(INDEX_FILE_NAME);
  const json   = found.hasNext() ? found.next().getBlob().getDataAsString() : '[]';
  return ContentService
    .createTextOutput(json)
    .setMimeType(ContentService.MimeType.JSON);
}

// ── STEP 1: Push new CVs from Drive to GitHub inbox/ ────────────────────────

function pushNewCVs() {
  const folder = DriveApp.getFolderById(DRIVE_FOLDER_ID);
  const files  = folder.getFiles();
  const props  = PropertiesService.getScriptProperties();
  const pushed = JSON.parse(props.getProperty(PUSHED_KEY) || '{}');
  let   count  = 0;

  while (files.hasNext()) {
    const file     = files.next();
    const fileId   = file.getId();
    const fileName = file.getName();
    const mime     = file.getMimeType();

    if (!VALID_MIME_TYPES.includes(mime)) continue;
    if (pushed[fileId])                   continue;  // already pushed

    try {
      const base64 = Utilities.base64Encode(file.getBlob().getBytes());
      const apiUrl = `https://api.github.com/repos/${GH_OWNER}/${GH_REPO}/contents/inbox/${encodeURIComponent(fileName)}`;

      const res = UrlFetchApp.fetch(apiUrl, {
        method: 'PUT',
        headers: {
          Authorization: `token ${getToken()}`,
          Accept:        'application/vnd.github.v3+json',
          'Content-Type': 'application/json',
        },
        payload: JSON.stringify({
          message: `📥 New CV from Drive: ${fileName}`,
          content: base64,
          branch:  GH_BRANCH,
        }),
        muteHttpExceptions: true,
      });

      const code = res.getResponseCode();
      if (code === 200 || code === 201) {
        pushed[fileId] = { name: fileName, at: new Date().toISOString() };
        count++;
        Logger.log(`✅ Pushed to GitHub: ${fileName}`);
      } else {
        Logger.log(`❌ GitHub push failed (${code}) for "${fileName}":\n${res.getContentText().slice(0, 300)}`);
      }
    } catch (e) {
      Logger.log(`❌ Error pushing "${fileName}": ${e.message}`);
    }
  }

  props.setProperty(PUSHED_KEY, JSON.stringify(pushed));
  Logger.log(`Step 1 done. ${count} CV(s) sent to GitHub.`);
}

// ── STEP 2: Sync branded PDFs from GitHub → Drive ───────────────────────────

function syncFromGitHub() {
  const props  = PropertiesService.getScriptProperties();
  const synced = JSON.parse(props.getProperty(SYNCED_KEY) || '{}');

  // Fetch the GitHub-side index.json via authenticated Contents API
  // (raw.githubusercontent.com is blocked from GAS IPs — use api.github.com instead)
  const rawIndex = _ghGetFileContent('index.json', false);
  if (rawIndex === null) {
    Logger.log('⚠️ Could not reach GitHub index.json — skipping sync.');
    return;
  }

  const ghIndex = JSON.parse(rawIndex);
  if (!ghIndex.length) {
    Logger.log('GitHub index is empty — nothing to sync.');
    return;
  }

  // Get or create "✅ Processed CVs" subfolder
  const parent = DriveApp.getFolderById(DRIVE_FOLDER_ID);
  let   procFolder;
  const subs = parent.getFoldersByName(PROC_FOLDER_NAME);
  procFolder = subs.hasNext() ? subs.next() : parent.createFolder(PROC_FOLDER_NAME);

  const driveIndex = [];
  let   newCount   = 0;

  for (const entry of ghIndex) {
    const filename    = entry.filename;
    let   driveFileId = synced[filename];

    if (!driveFileId) {
      // Download the branded PDF from GitHub raw
      // Guard: skip clearly invalid filenames (section headers mistakenly used as names)
      const _JUNK_RE = /^(curriculum vitae|executive summary|contact|roles?|responsibilities|references?|personal (data|information)|skills|education|overview|resume)\b/i;
      if (_JUNK_RE.test(filename)) {
        Logger.log(`⚠️ Skipping invalid entry with section-header filename: "${filename}"`);
        continue;
      }

      // Download branded PDF via authenticated Contents API (raw accept header → binary blob)
      const blob = _ghGetFileContent(`processed/${encodeURIComponent(filename)}`, true);
      if (blob === null) {
        Logger.log(`⏳ PDF not ready or unreachable on GitHub yet: ${filename}`);
        continue;
      }
      blob.setName(filename).setContentType('application/pdf');
      const driveFile = procFolder.createFile(blob);
      try {
        driveFile.setSharing(DriveApp.Access.ANYONE_WITH_LINK, DriveApp.Permission.VIEW);
      } catch (shareErr) {
        Logger.log(`⚠️ Could not set public sharing for "${filename}" (Workspace policy): ${shareErr.message}`);
      }

      driveFileId      = driveFile.getId();
      synced[filename] = driveFileId;
      newCount++;
      Logger.log(`✅ Saved to Drive: ${filename} (ID: ${driveFileId})`);
    }

    driveIndex.push({
      name:             entry.name,
      title:            entry.title            || '',
      location:         entry.location         || '',
      years_experience: entry.years_experience || null,
      source_file:      entry.source_file      || '',
      filename:         filename,
      driveFileId:      driveFileId,
      drivePreviewUrl:  `https://drive.google.com/file/d/${driveFileId}/preview`,
      driveDownloadUrl: `https://drive.google.com/uc?export=download&id=${driveFileId}`,
      processed:        entry.processed,
    });
  }

  // Write Drive index.json
  updateDriveIndex(parent, driveIndex);
  props.setProperty(SYNCED_KEY, JSON.stringify(synced));
  Logger.log(`Step 2 done. ${newCount} new branded PDF(s) saved to Drive.`);
}

// ── Helpers ──────────────────────────────────────────────────────────────────

function updateDriveIndex(folder, data) {
  const content = JSON.stringify(data, null, 2);
  const found   = folder.getFilesByName(INDEX_FILE_NAME);

  let file;
  if (found.hasNext()) {
    file = found.next();
    file.setContent(content);
  } else {
    file = folder.createFile(INDEX_FILE_NAME, content, 'application/json');
  }
  try {
    file.setSharing(DriveApp.Access.ANYONE_WITH_LINK, DriveApp.Permission.VIEW);
  } catch (shareErr) {
    Logger.log(`⚠️ Could not set public sharing for index.json (Workspace policy): ${shareErr.message}`);
  }
  Logger.log(`✅ palmdeck-index.json updated in Drive (${data.length} candidate(s))`);
}

// ── STEP 3: Purge CVs older than EXPIRY_DAYS ────────────────────────────────

function cleanupExpiredCVs() {
  const props   = PropertiesService.getScriptProperties();
  const pushed  = JSON.parse(props.getProperty(PUSHED_KEY) || '{}');
  const synced  = JSON.parse(props.getProperty(SYNCED_KEY) || '{}');
  const cutoff  = new Date(Date.now() - EXPIRY_DAYS * 24 * 60 * 60 * 1000);
  let   count   = 0;

  // ── Part A: Trash original CVs pushed > EXPIRY_DAYS ago ──────────────────
  for (const [fileId, info] of Object.entries(pushed)) {
    const pushedAt = new Date(info.at || 0);
    if (pushedAt > cutoff) continue;
    try {
      DriveApp.getFileById(fileId).setTrashed(true);
      Logger.log(`🗑️ Trashed original CV: ${info.name}`);
    } catch (e) {
      Logger.log(`⚠️ Could not trash "${info.name}": ${e.message}`);
    }
    delete pushed[fileId];
    count++;
  }

  // ── Part B: Expire processed entries older than cutoff ───────────────────
  const folder = DriveApp.getFolderById(DRIVE_FOLDER_ID);
  const found  = folder.getFilesByName(INDEX_FILE_NAME);
  if (!found.hasNext()) {
    Logger.log('No Drive index found — skipping processed-entry cleanup.');
    props.setProperty(PUSHED_KEY, JSON.stringify(pushed));
    Logger.log(`Step 3 done. ${count} expired CV(s) removed.`);
    return;
  }

  const driveIndex = JSON.parse(found.next().getBlob().getDataAsString());
  const toKeep     = [];

  for (const entry of driveIndex) {
    const processedAt = new Date(entry.processed || 0);
    if (processedAt > cutoff) { toKeep.push(entry); continue; }

    Logger.log(`🗑️ Expiring processed entry: ${entry.name} (${processedAt.toISOString()})`);

    // Delete branded PDF from Drive
    if (entry.driveFileId) {
      try {
        DriveApp.getFileById(entry.driveFileId).setTrashed(true);
        Logger.log(`  ✅ Trashed branded PDF (Drive ID: ${entry.driveFileId})`);
      } catch (e) {
        Logger.log(`  ⚠️ Could not trash branded PDF: ${e.message}`);
      }
    }

    // Remove from synced tracker (prevents re-download on next sync)
    if (synced[entry.filename]) delete synced[entry.filename];

    // Delete from GitHub: processed/<filename>
    _ghDeleteFile(`processed/${encodeURIComponent(entry.filename)}`, `🗑️ Expired after ${EXPIRY_DAYS}d: ${entry.name}`);

    // NOTE: _ghUpdateIndex is called ONCE after the loop (not here) to avoid
    // intermediate writes that restore already-expired entries.
    count++;
  }

  // Write the final pruned index to GitHub exactly once, after all deletions
  if (count > 0) {
    _ghUpdateIndex(toKeep);
  }

  // Write updated Drive index
  updateDriveIndex(folder, toKeep);
  props.setProperty(PUSHED_KEY, JSON.stringify(pushed));
  props.setProperty(SYNCED_KEY, JSON.stringify(synced));
  Logger.log(`Step 3 done. ${count} expired CV(s) cleaned up (cutoff: ${cutoff.toISOString()}).`);
}

// Delete a single file from the GitHub repo by path
function _ghDeleteFile(path, message) {
  const token = getToken();
  try {
    const getRes = UrlFetchApp.fetch(
      `https://api.github.com/repos/${GH_OWNER}/${GH_REPO}/contents/${path}`,
      { headers: { Authorization: `token ${token}`, Accept: 'application/vnd.github.v3+json' },
        muteHttpExceptions: true }
    );
    if (getRes.getResponseCode() !== 200) return; // file not found — nothing to delete
    const sha = JSON.parse(getRes.getContentText()).sha;
    UrlFetchApp.fetch(
      `https://api.github.com/repos/${GH_OWNER}/${GH_REPO}/contents/${path}`,
      { method: 'DELETE',
        headers: { Authorization: `token ${token}`, Accept: 'application/vnd.github.v3+json',
                   'Content-Type': 'application/json' },
        payload: JSON.stringify({ message, sha, branch: GH_BRANCH }),
        muteHttpExceptions: true }
    );
    Logger.log(`  ✅ Deleted from GitHub: ${path}`);
  } catch (e) {
    Logger.log(`  ⚠️ GitHub delete failed (${path}): ${e.message}`);
  }
}

// Overwrite GitHub index.json with a new array of entries (preserves only name/title/location/filename/processed)
function _ghUpdateIndex(entries) {
  const token = getToken();
  const path  = 'index.json';
  try {
    // Get current SHA
    const getRes = UrlFetchApp.fetch(
      `https://api.github.com/repos/${GH_OWNER}/${GH_REPO}/contents/${path}`,
      { headers: { Authorization: `token ${token}`, Accept: 'application/vnd.github.v3+json' },
        muteHttpExceptions: true }
    );
    if (getRes.getResponseCode() !== 200) return;
    const sha     = JSON.parse(getRes.getContentText()).sha;
    const payload = entries.map(e => ({
      name: e.name, title: e.title || '', location: e.location || '',
      filename: e.filename, processed: e.processed,
    }));
    UrlFetchApp.fetch(
      `https://api.github.com/repos/${GH_OWNER}/${GH_REPO}/contents/${path}`,
      { method: 'PUT',
        headers: { Authorization: `token ${token}`, Accept: 'application/vnd.github.v3+json',
                   'Content-Type': 'application/json' },
        payload: JSON.stringify({
          message: `🗑️ Remove expired CVs from index`,
          content: Utilities.base64Encode(JSON.stringify(payload, null, 2)),
          sha, branch: GH_BRANCH }),
        muteHttpExceptions: true }
    );
    Logger.log(`  ✅ GitHub index.json updated (${payload.length} remaining).`);
  } catch (e) {
    Logger.log(`  ⚠️ Could not update GitHub index.json: ${e.message}`);
  }
}

// ── Main entry point (set as the trigger function) ───────────────────────────

function runPipeline() {
  Logger.log('══════════════════════════════════');
  Logger.log('  Simpalm CV Pipeline — ' + new Date().toLocaleString());
  Logger.log('══════════════════════════════════');

  Logger.log('\n› Step 1: Checking for new CVs in Drive...');
  pushNewCVs();

  Logger.log('\n› Step 2: Syncing processed PDFs from GitHub...');
  syncFromGitHub();

  Logger.log('\n› Step 3: Cleaning up CVs older than ' + EXPIRY_DAYS + ' days...');
  cleanupExpiredCVs();

  Logger.log('\n══════════════════════════════════');
  Logger.log('  Pipeline complete.');
  Logger.log('══════════════════════════════════\n');
}

// ── Auto-trigger DISABLED ─────────────────────────────────────────────────────
// Pipeline now runs ONLY when the user explicitly presses "Trigger Now" in PalmDeck.
// onDriveChange kept for reference but will not fire (trigger not installed).
// To re-enable auto-processing: install an onChange trigger pointing to onDriveChange.

function onDriveChange(e) {
  // AUTO-TRIGGER DISABLED — manual "Trigger Now" only.
  Logger.log('onDriveChange fired but auto-trigger is disabled. Use PalmDeck → Trigger Now.');
}

// ── Trigger management ────────────────────────────────────────────────────────
// Pipeline is MANUAL-ONLY. Run removeAllTriggers() once from the editor to
// disable any existing auto-triggers that may have been installed previously.

function removeAllTriggers() {
  const existing = ScriptApp.getProjectTriggers();
  existing.forEach(t => ScriptApp.deleteTrigger(t));
  Logger.log(`✅ Removed ${existing.length} trigger(s). Pipeline is now manual-only.`);
}

// Kept for documentation — NOT called anywhere active.
// To re-enable auto-processing, restore the old installTriggers body.
function installTriggers() {
  Logger.log('⚠️ Auto-triggers are disabled. Pipeline runs via PalmDeck → Trigger Now only.');
  Logger.log('   To re-enable, restore the old installTriggers() body and run it.');
}

// ── Manual helpers (run once from editor to test) ─────────────────────────────

// Run this to test the push step alone
function testPush() { pushNewCVs(); }

// Run this to test the sync step alone
function testSync() { syncFromGitHub(); }

// Run this to reset the "already pushed" tracker (re-push all files)
function resetPushedTracker() {
  PropertiesService.getScriptProperties().deleteProperty(PUSHED_KEY);
  Logger.log('Pushed tracker reset.');
}

// Run this to reset the "already synced" tracker (re-download all from GitHub)
function resetSyncedTracker() {
  PropertiesService.getScriptProperties().deleteProperty(SYNCED_KEY);
  Logger.log('Synced tracker reset.');
}
