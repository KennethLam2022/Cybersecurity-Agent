const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const templatePath = path.join(__dirname, '..', 'templates', 'admin.html');

test('workspace machine id stays secondary to the workspace name', async () => {
  const html = fs.readFileSync(templatePath, 'utf8');
  const start = html.indexOf('async function loadWorkspaces() {');
  const end = html.indexOf('\nasync function loadCustomRoles()', start);
  assert.ok(start >= 0 && end > start);

  const workspace = {
    id: 'org-0123456789abcdef0123456789abcdef',
    name: 'Customer Risk Assessment Workspace With A Long Name',
    owner_name: 'Workspace Owner',
    user_count: 8,
    agent_count: 2,
    knowledge_base_count: 3,
    status: 'active',
    users: [],
    agents: [],
  };
  const shortWorkspace = {
    ...workspace,
    id: 'org-short',
    name: 'Audit Workspace',
  };
  const container = {innerHTML: ''};
  const sandbox = {
    document: {
      getElementById(id) {
        return id === 'workspaceStatus' ? {textContent: ''} : container;
      },
    },
    fetch: async () => ({ok: true, json: async () => ({items: [workspace, shortWorkspace]})}),
    escHtml: value => String(value),
    loadProfileProposals() {},
    loadConflictProposals() {},
    loadCustomRoles() {},
    loadIndustryProfiles() {},
    loadUserRegistrations() {},
    loadLoginLocks() {},
  };
  vm.runInNewContext(html.slice(start, end), sandbox);
  await sandbox.loadWorkspaces();

  assert.match(
    container.innerHTML,
    /<div class="chart-title" style="overflow-wrap:anywhere">Customer Risk Assessment Workspace With A Long Name<\/div>/,
  );
  assert.match(
    container.innerHTML,
    /font-size:11px;color:var\(--t2\);margin-top:4px;overflow-wrap:anywhere"><code style="white-space:normal;overflow-wrap:anywhere">org-0123456789abcdef0123456789abcdef<\/code>/,
  );
  assert.match(container.innerHTML, /<div class="chart-title" style="overflow-wrap:anywhere">Audit Workspace<\/div>/);
  assert.match(container.innerHTML, /<code style="white-space:normal;overflow-wrap:anywhere">org-short<\/code>/);
  assert.match(container.innerHTML, /display:flex;gap:6px;flex-wrap:wrap/);
  assert.equal(workspace.name, 'Customer Risk Assessment Workspace With A Long Name');
  assert.equal(shortWorkspace.name, 'Audit Workspace');
});
