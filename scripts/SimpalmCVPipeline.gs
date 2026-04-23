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
  if (action === 'trigger') {
    try {
      Logger.log('⚡ Manual trigger from PalmDeck — running pipeline…');
      runPipeline();
      return _jsonOut({ ok: true, triggered: true,
        message: 'Pipeline started. Branded PDF should be ready in ~2–3 min.' });
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

  // Fetch the GitHub-side index.json
  const indexUrl = `https://raw.githubusercontent.com/${GH_OWNER}/${GH_REPO}/${GH_BRANCH}/index.json`;
  const idxRes   = UrlFetchApp.fetch(indexUrl, { muteHttpExceptions: true });

  if (idxRes.getResponseCode() !== 200) {
    Logger.log('GitHub index.json not available yet — skipping sync.');
    return;
  }

  const ghIndex = JSON.parse(idxRes.getContentText());
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
      const pdfUrl = `https://raw.githubusercontent.com/${GH_OWNER}/${GH_REPO}/${GH_BRANCH}/processed/${encodeURIComponent(filename)}`;
      const pdfRes = UrlFetchApp.fetch(pdfUrl, { muteHttpExceptions: true });

      if (pdfRes.getResponseCode() !== 200) {
        Logger.log(`⏳ PDF not ready on GitHub yet: ${filename}`);
        continue;
      }

      const blob      = pdfRes.getBlob().setName(filename).setContentType('application/pdf');
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
      name:            entry.name,
      title:           entry.title    || '',
      location:        entry.location || '',
      filename:        filename,
      driveFileId:     driveFileId,
      drivePreviewUrl: `https://drive.google.com/file/d/${driveFileId}/preview`,
      driveDownloadUrl:`https://drive.google.com/uc?export=download&id=${driveFileId}`,
      processed:       entry.processed,
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

// ── Main entry point (set as the trigger function) ───────────────────────────

function runPipeline() {
  Logger.log('══════════════════════════════════');
  Logger.log('  Simpalm CV Pipeline — ' + new Date().toLocaleString());
  Logger.log('══════════════════════════════════');

  Logger.log('\n› Step 1: Checking for new CVs in Drive...');
  pushNewCVs();

  Logger.log('\n› Step 2: Syncing processed PDFs from GitHub...');
  syncFromGitHub();

  Logger.log('\n══════════════════════════════════');
  Logger.log('  Pipeline complete.');
  Logger.log('══════════════════════════════════\n');
}

// ── Instant trigger: fires the moment a file lands in the folder ─────────────
// The onChange Drive trigger fires for ANY Drive change.
// We filter to only act when a new CV appears in our specific folder.

function onDriveChange(e) {
  if (!e || e.changeType !== 'ADD') return;  // only care about new files

  try {
    const fileId = e.driveEvent && e.driveEvent.id;
    if (!fileId) return;

    const file    = DriveApp.getFileById(fileId);
    const mime    = file.getMimeType();

    // Only PDF / DOCX / DOC
    if (!VALID_MIME_TYPES.includes(mime)) return;

    // Confirm it's inside our target folder
    const parents = file.getParents();
    while (parents.hasNext()) {
      const parent = parents.next();
      if (parent.getId() === DRIVE_FOLDER_ID) {
        Logger.log(`⚡ Instant trigger: "${file.getName()}" added — starting pipeline…`);
        runPipeline();
        return;
      }
    }
  } catch (err) {
    Logger.log(`onDriveChange error: ${err.message}`);
  }
}

// ── One-time trigger installer ────────────────────────────────────────────────
// Run this ONCE from the Apps Script editor (Run → installTriggers).
// It sets up both the instant onChange trigger AND the 5-min backup poller.

function installTriggers() {
  const existing = ScriptApp.getProjectTriggers();

  // Remove any old triggers for this script to avoid duplicates
  existing.forEach(t => ScriptApp.deleteTrigger(t));

  // NOTE: The Drive onChange trigger cannot be installed programmatically.
  // Add it manually: Triggers page → Add Trigger → onDriveChange → From Drive → On change.

  // Time-based backup: every 5 minutes — handles the GitHub→Drive sync leg
  ScriptApp.newTrigger('runPipeline')
    .timeBased()
    .everyMinutes(5)
    .create();
  Logger.log('✅ 5-minute backup poller installed.');

  Logger.log('\nTriggers active:');
  ScriptApp.getProjectTriggers().forEach(t =>
    Logger.log(`  • ${t.getHandlerFunction()} (${t.getEventType()})`)
  );
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
