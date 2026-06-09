'use strict';

const express = require('express');
const yaml = require('js-yaml');
const path = require('path');
const fs = require('fs');
const { execSync } = require('child_process');

const PROJECT_ROOT = path.resolve(__dirname, '..');
const STATE_DIR = path.join(PROJECT_ROOT, 'state');
const DB_PATH = path.join(STATE_DIR, 'pipeline.db');
const IDEAS_YAML = path.join(STATE_DIR, 'ideas.yaml');
const AGENTS_YAML = path.join(PROJECT_ROOT, 'agents.yaml');
const SPA_BUILD = path.join(__dirname, 'client', 'build');

// Load config with fallback defaults so the server starts without agents.yaml.
let cfg = {};
try {
  cfg = yaml.load(fs.readFileSync(AGENTS_YAML, 'utf8'));
} catch { /* use defaults */ }
const HEARTBEAT_MS = ((cfg.orchestrator || {}).heartbeat_seconds || 120) * 1000;
const POLL_MS = 5000;

const app = express();

// SSE state
const sseClients = new Set();
let fileSnapshot = null;
let lastIssueHash = null;
let lastIdeasHash = null;

// --------------------------------------------------------------------------
// DB helpers — uses the built-in node:sqlite module (Node 22+).
// The server is strictly read-only; it never writes to pipeline.db.
// --------------------------------------------------------------------------

function openDb() {
  if (!fs.existsSync(DB_PATH)) return null;
  try {
    const { DatabaseSync } = require('node:sqlite');
    return new DatabaseSync(DB_PATH, { readOnly: true });
  } catch { // DB locked, corrupt, or node:sqlite unavailable
    return null;
  }
}

function oneLiner(body) {
  if (!body) return '';
  const line = body.split('\n')
    .map(l => l.trim())
    .find(l => l && !l.startsWith('#') && !l.startsWith('- ['));
  return line || '';
}

function getIssues() {
  const db = openDb();
  if (!db) return [];
  try {
    // Detect whether the agent_name column exists (added in a later migration).
    const cols = new Set(
      db.prepare('PRAGMA table_info(tasks)').all().map(r => r.name)
    );
    const fields = ['issue', 'title', 'body', 'state'];
    if (cols.has('agent_name')) fields.push('agent_name');
    const rows = db.prepare(
      `SELECT ${fields.join(',')} FROM tasks WHERE state NOT IN ('MERGED','DONE') ORDER BY priority ASC, issue ASC`
    ).all();
    return rows.map(r => ({
      number: Number(r.issue),
      title: r.title,
      oneLiner: oneLiner(r.body),
      state: r.state,
      agentName: r.agent_name || null,
    }));
  } finally {
    db.close();
  }
}

function getIdeas() {
  if (!fs.existsSync(IDEAS_YAML)) return [];
  try {
    return yaml.load(fs.readFileSync(IDEAS_YAML, 'utf8')) || [];
  } catch { // malformed YAML
    return [];
  }
}

// --------------------------------------------------------------------------
// File snapshot (for the Files panel)
// --------------------------------------------------------------------------

function takeSnapshot() {
  try {
    const files = execSync('git ls-files', { cwd: PROJECT_ROOT, encoding: 'utf8' })
      .split('\n').filter(Boolean);
    const snap = {};
    for (const f of files) {
      try {
        snap[f] = fs.statSync(path.join(PROJECT_ROOT, f)).mtimeMs;
      } catch { // file deleted between ls-files and stat
        snap[f] = 0;
      }
    }
    return snap;
  } catch { // git unavailable or not a repo
    return {};
  }
}

function classifyFiles(current, prev) {
  return Object.keys(current).map(f => ({
    path: f,
    status: !prev
      ? 'unchanged'
      : prev[f] === undefined
        ? 'new'
        : prev[f] !== current[f]
          ? 'changed'
          : 'unchanged',
  }));
}

// --------------------------------------------------------------------------
// SSE broadcast
// --------------------------------------------------------------------------

function broadcast(type, data) {
  if (sseClients.size === 0) return;
  const msg = `event: ${type}\ndata: ${JSON.stringify(data)}\n\n`;
  for (const res of sseClients) {
    try {
      res.write(msg);
    } catch { // client disconnected mid-write
      sseClients.delete(res);
    }
  }
}

// --------------------------------------------------------------------------
// Routes
// --------------------------------------------------------------------------

app.get('/api/issues', (_req, res) => res.json(getIssues()));

app.get('/api/files', (_req, res) => {
  const current = takeSnapshot();
  res.json(classifyFiles(current, fileSnapshot));
});

app.get('/api/ideas', (_req, res) => res.json(getIdeas()));

app.get('/events', (req, res) => {
  res.setHeader('Content-Type', 'text/event-stream');
  res.setHeader('Cache-Control', 'no-cache');
  res.setHeader('Connection', 'keep-alive');
  res.flushHeaders();

  // Push current state immediately so the client does not need to poll.
  res.write(`event: issues\ndata: ${JSON.stringify(getIssues())}\n\n`);
  res.write(`event: ideas\ndata: ${JSON.stringify(getIdeas())}\n\n`);
  const snap = takeSnapshot();
  res.write(`event: files\ndata: ${JSON.stringify(classifyFiles(snap, fileSnapshot))}\n\n`);

  sseClients.add(res);
  req.on('close', () => sseClients.delete(res));
});

// Serve the built React SPA when available.
if (fs.existsSync(SPA_BUILD)) {
  app.use(express.static(SPA_BUILD));
  app.get('*', (_req, res) => res.sendFile(path.join(SPA_BUILD, 'index.html')));
}

// --------------------------------------------------------------------------
// Heartbeat: refresh file snapshot + drop MERGED/DONE from active set
// --------------------------------------------------------------------------

function heartbeat() {
  const newSnap = takeSnapshot();
  if (fileSnapshot) {
    const files = classifyFiles(newSnap, fileSnapshot);
    if (files.some(f => f.status !== 'unchanged')) {
      broadcast('files', files);
    }
  }
  fileSnapshot = newSnap;
  // Calling pollChanges here drops MERGED/DONE issues from broadcasts
  // by refreshing the full issue list (finished issues won't appear if filtered).
  pollChanges();
}

function pollChanges() {
  const issues = getIssues();
  const h = JSON.stringify(issues);
  if (h !== lastIssueHash) {
    lastIssueHash = h;
    broadcast('issues', issues);
  }

  const ideas = getIdeas();
  const ih = JSON.stringify(ideas);
  if (ih !== lastIdeasHash) {
    lastIdeasHash = ih;
    broadcast('ideas', ideas);
  }
}

// --------------------------------------------------------------------------
// Server lifecycle (timers start only when explicitly started, not on import)
// --------------------------------------------------------------------------

function start(port) {
  fileSnapshot = takeSnapshot();
  lastIssueHash = JSON.stringify(getIssues());
  lastIdeasHash = JSON.stringify(getIdeas());

  setInterval(heartbeat, HEARTBEAT_MS);
  setInterval(pollChanges, POLL_MS);

  return app.listen(port, () => {
    console.log(`Agent Monitor server listening on port ${port}`);
  });
}

module.exports = { app, start, getIssues, getIdeas, classifyFiles, takeSnapshot, IDEAS_YAML };

if (require.main === module) {
  start(process.env.PORT || 3000);
}
