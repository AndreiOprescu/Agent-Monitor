'use strict';

// Mock node:sqlite so tests can run without a real DB or Node 22+.
// The factory seeds four rows (two active, two terminal). The mock evaluates
// any "NOT IN (...)" clause in the SQL so that the WHERE-based filter in
// getIssues() is actually exercised — if the clause is absent, all rows come
// back and the terminal-state assertions will fail, catching the regression.
jest.mock('node:sqlite', () => ({
  DatabaseSync: jest.fn(() => ({
    prepare: (sql) => ({
      all: () => {
        if (sql.includes('PRAGMA table_info')) {
          return [
            { name: 'issue' }, { name: 'title' }, { name: 'body' },
            { name: 'state' }, { name: 'agent_name' },
          ];
        }
        const rows = [
          { issue: '1', title: 'Pending issue', body: '', state: 'PENDING', agent_name: null },
          { issue: '2', title: 'In progress', body: '', state: 'IN_PROGRESS', agent_name: 'x' },
          { issue: '3', title: 'Merged', body: '', state: 'MERGED', agent_name: null },
          { issue: '4', title: 'Done', body: '', state: 'DONE', agent_name: null },
        ];
        // Simulate SQLite's NOT IN (...) evaluation so the SQL WHERE clause is tested.
        const notIn = sql.match(/NOT IN \(([^)]+)\)/i);
        if (notIn) {
          const excluded = notIn[1].split(',').map(s => s.trim().replace(/'/g, ''));
          return rows.filter(r => !excluded.includes(r.state));
        }
        return rows;
      },
    }),
    close: jest.fn(),
  })),
}), { virtual: true });

const request = require('supertest');
const path = require('path');
const fs = require('fs');
const yaml = require('js-yaml');
const { app, getIssues, getIdeas, classifyFiles, IDEAS_YAML } = require('../server');

// Derive the same DB_PATH that server.js uses.
const DB_PATH = path.resolve(__dirname, '..', '..', 'state', 'pipeline.db');

// ---------------------------------------------------------------------------
// GET /api/issues
// ---------------------------------------------------------------------------
describe('GET /api/issues', () => {
  it('returns 200 with an array', async () => {
    const res = await request(app).get('/api/issues');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
  });

  it('each issue has the required shape', async () => {
    const res = await request(app).get('/api/issues');
    for (const issue of res.body) {
      expect(typeof issue.number).toBe('number');
      expect(typeof issue.title).toBe('string');
      expect(typeof issue.oneLiner).toBe('string');
      expect(typeof issue.state).toBe('string');
      expect(Object.prototype.hasOwnProperty.call(issue, 'agentName')).toBe(true);
    }
  });

  it('excludes MERGED and DONE; keeps PENDING and IN_PROGRESS', () => {
    // Create a placeholder DB file so openDb() passes the existsSync check;
    // node:sqlite is mocked above to return all four states.
    fs.mkdirSync(path.dirname(DB_PATH), { recursive: true });
    fs.writeFileSync(DB_PATH, '');
    try {
      const issues = getIssues();
      const states = issues.map(i => i.state);
      expect(states).not.toContain('MERGED');
      expect(states).not.toContain('DONE');
      expect(states).toContain('PENDING');
      expect(states).toContain('IN_PROGRESS');
      expect(issues.length).toBe(2);
    } finally {
      fs.unlinkSync(DB_PATH);
    }
  });
});

// ---------------------------------------------------------------------------
// GET /api/files
// ---------------------------------------------------------------------------
describe('GET /api/files', () => {
  it('returns 200 with an array', async () => {
    const res = await request(app).get('/api/files');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
  });

  it('each file has path and a valid status', async () => {
    const res = await request(app).get('/api/files');
    const validStatuses = new Set(['new', 'changed', 'unchanged']);
    for (const f of res.body) {
      expect(typeof f.path).toBe('string');
      expect(validStatuses.has(f.status)).toBe(true);
    }
  });
});

// ---------------------------------------------------------------------------
// GET /api/ideas
// ---------------------------------------------------------------------------
describe('GET /api/ideas', () => {
  afterEach(() => {
    if (fs.existsSync(IDEAS_YAML)) fs.unlinkSync(IDEAS_YAML);
  });

  it('returns empty array when ideas.yaml is absent', async () => {
    if (fs.existsSync(IDEAS_YAML)) fs.unlinkSync(IDEAS_YAML);
    const res = await request(app).get('/api/ideas');
    expect(res.status).toBe(200);
    expect(res.body).toEqual([]);
  });

  it('returns parsed records when ideas.yaml is present', async () => {
    const ideas = [
      { title: 'Add dark mode', priority: 'P2', rationale: 'UX win' },
      { title: 'Export CSV', priority: 'P3', rationale: 'Nice to have' },
    ];
    fs.mkdirSync(path.dirname(IDEAS_YAML), { recursive: true });
    fs.writeFileSync(IDEAS_YAML, yaml.dump(ideas));

    const res = await request(app).get('/api/ideas');
    expect(res.status).toBe(200);
    expect(res.body).toHaveLength(2);
    expect(res.body[0].title).toBe('Add dark mode');
  });
});

// ---------------------------------------------------------------------------
// GET /events  (SSE)
// ---------------------------------------------------------------------------
describe('GET /events', () => {
  let server;
  beforeAll(done => { server = app.listen(0, done); });
  afterAll(done => { server.close(done); });

  it('responds with SSE headers and emits initial events', done => {
    const http = require('http');
    const port = server.address().port;

    const req = http.get(`http://localhost:${port}/events`, res => {
      expect(res.headers['content-type']).toMatch(/text\/event-stream/);
      expect(res.headers['cache-control']).toBe('no-cache');

      let buf = '';
      res.on('data', chunk => {
        buf += chunk.toString();
        if (buf.includes('event: issues')) {
          req.destroy();
          done();
        }
      });
    });

    req.on('error', err => {
      if (err.code === 'ECONNRESET') return; // expected when req.destroy() is called
      done(err);
    });

    req.setTimeout(5000, () => {
      req.destroy();
      done(new Error('SSE did not emit within 5s'));
    });
  });
});

// ---------------------------------------------------------------------------
// classifyFiles (pure function)
// ---------------------------------------------------------------------------
describe('classifyFiles', () => {
  it('marks all files as unchanged when snapshot matches exactly', () => {
    const snap = { 'a.js': 1000, 'b.js': 2000 };
    const result = classifyFiles(snap, snap);
    expect(result.every(f => f.status === 'unchanged')).toBe(true);
  });

  it('marks a file as new when it was absent from the previous snapshot', () => {
    const prev = { 'a.js': 1000 };
    const curr = { 'a.js': 1000, 'b.js': 2000 };
    const result = classifyFiles(curr, prev);
    const b = result.find(f => f.path === 'b.js');
    expect(b.status).toBe('new');
  });

  it('marks a file as changed when its mtime differs', () => {
    const prev = { 'a.js': 1000 };
    const curr = { 'a.js': 9999 };
    const result = classifyFiles(curr, prev);
    expect(result[0].status).toBe('changed');
  });

  it('treats all files as unchanged when there is no previous snapshot', () => {
    const curr = { 'a.js': 1000 };
    const result = classifyFiles(curr, null);
    expect(result[0].status).toBe('unchanged');
  });
});

// ---------------------------------------------------------------------------
// getIdeas (unit)
// ---------------------------------------------------------------------------
describe('getIdeas', () => {
  afterEach(() => {
    if (fs.existsSync(IDEAS_YAML)) fs.unlinkSync(IDEAS_YAML);
  });

  it('returns empty array when file absent', () => {
    if (fs.existsSync(IDEAS_YAML)) fs.unlinkSync(IDEAS_YAML);
    expect(getIdeas()).toEqual([]);
  });

  it('returns parsed array from valid YAML', () => {
    const ideas = [{ title: 'Feature X' }];
    fs.mkdirSync(path.dirname(IDEAS_YAML), { recursive: true });
    fs.writeFileSync(IDEAS_YAML, yaml.dump(ideas));
    expect(getIdeas()).toEqual(ideas);
  });

  it('returns empty array on malformed YAML', () => {
    fs.mkdirSync(path.dirname(IDEAS_YAML), { recursive: true });
    fs.writeFileSync(IDEAS_YAML, '{ bad: yaml: [');
    expect(getIdeas()).toEqual([]);
  });
});
