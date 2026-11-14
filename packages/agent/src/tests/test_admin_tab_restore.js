const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const templatePath = path.join(__dirname, '..', 'templates', 'admin.html');

function loadNavigationFunctions() {
  const html = fs.readFileSync(templatePath, 'utf8');
  const loaderStart = html.indexOf('async function loadAdminPrincipal() {');
  const loaderEnd = html.indexOf('function currentAdminActor()', loaderStart);
  const switchStart = html.indexOf('function switchTab(name) {');
  const switchEnd = html.indexOf('async function loadWorkspaces() {', switchStart);
  assert.ok(loaderStart >= 0 && loaderEnd > loaderStart);
  assert.ok(switchStart >= 0 && switchEnd > switchStart);
  return html.slice(loaderStart, loaderEnd) + html.slice(switchStart, switchEnd);
}

function loadAdminStartup() {
  const html = fs.readFileSync(templatePath, 'utf8');
  const loaderStart = html.indexOf('async function loadAdminPrincipal() {');
  const loaderEnd = html.indexOf('function currentAdminActor()', loaderStart);
  const connectionStart = html.indexOf('function connectSSE() {');
  const connectionEnd = html.indexOf('\nlet _convCatFilter', connectionStart);
  const startupStart = html.indexOf("document.addEventListener('DOMContentLoaded', function() {");
  const startupEnd = html.indexOf('\n});', startupStart) + 4;
  assert.ok(loaderStart >= 0 && loaderEnd > loaderStart);
  assert.ok(connectionStart >= 0 && connectionEnd > connectionStart);
  assert.ok(startupStart >= 0 && startupEnd > startupStart);
  return {
    source: html.slice(loaderStart, loaderEnd)
      + html.slice(connectionStart, connectionEnd)
      + html.slice(startupStart, startupEnd),
    sidebarIsPlatformOnly: /<div class="sidebar"[^>]*data-platform-only/.test(html),
    platformOnlyStatus: ['connDot', 'connStatus'].map((id) => {
      const element = html.match(new RegExp(`<[^>]+id="${id}"[^>]*>`))?.[0] || '';
      return element.includes('data-platform-only');
    }),
  };
}

async function runAdminStartup(role) {
  const {source, sidebarIsPlatformOnly, platformOnlyStatus} = loadAdminStartup();
  const dotHidden = new Set();
  const statusHidden = new Set();
  const sidebarHidden = new Set();
  const dot = {className: '', classList: {toggle(name, value) {
    if (value) dotHidden.add(name);
    else dotHidden.delete(name);
  }}};
  const status = {textContent: '连接中...', classList: {toggle(name, value) {
    if (value) statusHidden.add(name);
    else statusHidden.delete(name);
  }}};
  const sidebar = {classList: {toggle(name, value) {
    if (value) sidebarHidden.add(name);
    else sidebarHidden.delete(name);
  }}};
  const platformOnlyElements = [dot, status].filter((_, index) => platformOnlyStatus[index]);
  if (sidebarIsPlatformOnly) platformOnlyElements.push(sidebar);
  const eventSources = [];
  let onReady;
  let conversationLoads = 0;
  const sandbox = {
    URLSearchParams,
    location: {search: ''},
    document: {
      addEventListener(_event, callback) { onReady = callback; },
      getElementById(id) { return id === 'connDot' ? dot : status; },
      querySelector() { return null; },
      querySelectorAll(selector) {
        if (selector === '[data-platform-only]') return platformOnlyElements;
        return [];
      },
    },
    fetch: async () => ({ok: true, json: async () => ({authenticated: true, role})}),
    EventSource: class {
      constructor(url) {
        eventSources.push(url);
      }
      addEventListener() {}
      close() {}
    },
    fetchConversations() { conversationLoads += 1; },
    setTimeout,
    clearTimeout,
  };
  vm.runInNewContext(source, sandbox);
  onReady();
  await new Promise(setImmediate);
  return {eventSources, dotHidden, statusHidden, sidebarHidden, conversationLoads};
}

async function runNavigation(requestedTab, principal = {authenticated: true, role: 'org_admin'}) {
  const tabs = ['detail', 'overview', 'workspaces', 'notifications'].map((name) => ({
    dataset: {tab: name, group: 'operations'},
    hidden: ['detail', 'overview'].includes(name),
    active: name === 'detail',
    attributes: {},
    classList: {
      contains(className) {
        return className === 'role-hidden' ? this.tab.hidden : this.tab.active;
      },
      toggle(className, value) {
        if (className === 'role-hidden') this.tab.hidden = value;
        if (className === 'active') this.tab.active = value;
      },
      tab: null,
    },
    setAttribute(name, value) {
      this.attributes[name] = value;
    },
  }));
  tabs.forEach((tab) => { tab.classList.tab = tab; });
  const contents = ['tabDetail', 'tabOverview', 'tabWorkspaces', 'tabNotifications'].map((id) => ({
    id,
    classList: {toggle(_className, value) { this.active = value; }},
  }));
  const contextElements = new Map([
    ['tabContextTitle', {textContent: ''}],
    ['tabContextDescription', {textContent: ''}],
    ['tabContext', {style: {}}],
    ['notificationTenant', {value: 'local-default'}],
    ['monitoringTenant', {value: 'local-default'}],
  ]);
  const url = new URL(`http://localhost/admin?tab=${requestedTab}`);
  const selected = [];
  const messages = [];
  const qualityAccessNotice = {
    hidden: true,
    classList: {toggle(className, value) {
      if (className === 'role-hidden') qualityAccessNotice.hidden = value;
    }},
  };
  const primaryGroup = {dataset: {group: 'operations'}, classList: {toggle() {}}};
  const sandbox = {
    URL,
    URLSearchParams,
    CSS: {escape: (value) => value},
    adminPrincipal: {authenticated: false, role: 'user'},
    adminLanguage: 'zh-CN',
    adminTabContextEn: {},
    platformOnlyTabs: new Set(['detail', 'overview']),
    chartInstances: {initialized: true},
    currentConvId: null,
    promptRadarChart: null,
    promptHistoryChart: null,
    promptContributionChart: null,
    location: {href: url.href, get search() {return url.search;}},
    history: {replaceState(_state, _title, nextUrl) {
      url.href = new URL(nextUrl, url).href;
      this.replaced = url.href;
    }},
    document: {
      querySelector(selector) {
        if (selector === '.tab-item:not(.role-hidden)') return tabs.find((tab) => !tab.hidden);
        if (selector === '.tab-item.active') return tabs.find((tab) => tab.active);
        const match = selector.match(/^\.tab-item\[data-tab="(.+)"\]$/);
        if (match) return tabs.find((tab) => tab.dataset.tab === match[1]);
        return null;
      },
      querySelectorAll(selector) {
        if (selector === '.tab-item') return tabs;
        if (selector === '.tab-content') return contents;
        if (selector === '.primary-nav-item') return [primaryGroup];
        if (selector === '[data-org-admin-only]') return [qualityAccessNotice];
        if (selector === '[data-platform-only]') return [];
        const groupMatch = selector.match(/^\.tab-item\[data-group="(.+)"\]$/);
        if (groupMatch) return tabs.filter((tab) => tab.dataset.group === groupMatch[1]);
        return [];
      },
      getElementById(id) {
        return contextElements.get(id) || null;
      },
    },
    showToast(message) { messages.push(message); },
    fetch: async () => ({
      ok: true,
      json: async () => principal,
    }),
    syncPrimaryGroup() {},
    groupForTab: () => 'operations',
    loadWorkspaces() { selected.push('workspaces-loaded'); },
    setTimeout() {},
    console,
  };
  await vm.runInNewContext(
    `${loadNavigationFunctions()}; loadAdminPrincipal();`,
    sandbox,
  );
  return {tabs, url, selected, messages, contextElements, qualityAccessNotice};
}

function loadReflectionRuleLabel(language) {
  const html = fs.readFileSync(templatePath, 'utf8');
  const start = html.indexOf('const reflectionRuleLabels = {');
  const end = html.indexOf('function clearReflectionRuleForm()', start);
  assert.ok(start >= 0 && end > start);
  const sandbox = {adminLanguage: language};
  vm.runInNewContext(html.slice(start, end), sandbox);
  return sandbox.reflectionRuleLabel;
}

function loadAgentEvalErrorSummary() {
  const html = fs.readFileSync(templatePath, 'utf8');
  const start = html.indexOf('function agentEvalErrorSummary(error) {');
  const end = html.indexOf('function renderAgentEval(run, results)', start);
  assert.ok(start >= 0 && end > start);
  const sandbox = {};
  vm.runInNewContext(html.slice(start, end), sandbox);
  return sandbox.agentEvalErrorSummary;
}

function loadE2eMetricNames(language) {
  const html = fs.readFileSync(templatePath, 'utf8');
  const start = html.indexOf('function e2eMetricNames(language) {');
  const end = html.indexOf('async function loadE2eEval()', start);
  assert.ok(start >= 0 && end > start);
  const sandbox = {};
  vm.runInNewContext(html.slice(start, end), sandbox);
  return sandbox.e2eMetricNames(language);
}

function loadOperationsTaskDisplay() {
  const html = fs.readFileSync(templatePath, 'utf8');
  const start = html.indexOf('function operationsTaskDisplay(item) {');
  const end = html.indexOf('async function loadOperationsTasks()', start);
  assert.ok(start >= 0 && end > start);
  const sandbox = {};
  vm.runInNewContext(html.slice(start, end), sandbox);
  return sandbox.operationsTaskDisplay;
}

test('refresh restores only after role filtering and redirects restricted tabs', async () => {
  const {tabs, url, selected, messages} = await runNavigation('overview');
  assert.equal(tabs.find((tab) => tab.active).dataset.tab, 'workspaces');
  assert.equal(url.searchParams.get('tab'), 'workspaces');
  assert.deepEqual(selected, ['workspaces-loaded']);
  assert.equal(messages.length, 1);
});

test('unknown tab request selects a visible tab and replaces the URL', async () => {
  const {tabs, url, selected} = await runNavigation('does-not-exist');
  assert.equal(tabs.find((tab) => tab.active).dataset.tab, 'workspaces');
  assert.equal(url.searchParams.get('tab'), 'workspaces');
  assert.deepEqual(selected, ['workspaces-loaded']);
});

test('valid tab selection is restored without a permission warning', async () => {
  const {tabs, url, messages} = await runNavigation('workspaces');
  assert.equal(tabs.find((tab) => tab.active).dataset.tab, 'workspaces');
  assert.equal(url.searchParams.get('tab'), 'workspaces');
  assert.deepEqual(messages, []);
});

test('does not connect org admins to the platform event stream', async () => {
  const {eventSources, dotHidden, statusHidden} = await runAdminStartup('org_admin');

  assert.deepEqual(eventSources, []);
  assert.equal(dotHidden.has('role-hidden'), true);
  assert.equal(statusHidden.has('role-hidden'), true);
});

test('keeps the platform admin event stream connected', async () => {
  const {eventSources, dotHidden, statusHidden} = await runAdminStartup('platform_admin');

  assert.deepEqual(eventSources, ['/api/admin/stream']);
  assert.equal(dotHidden.has('role-hidden'), false);
  assert.equal(statusHidden.has('role-hidden'), false);
});

test('hides the conversation sidebar from org admins', async () => {
  const {sidebarHidden} = await runAdminStartup('org_admin');

  assert.equal(sidebarHidden.has('role-hidden'), true);
});

test('keeps the conversation sidebar available to platform admins', async () => {
  const {sidebarHidden} = await runAdminStartup('platform_admin');

  assert.equal(sidebarHidden.has('role-hidden'), false);
});

test('does not load conversations for org admins', async () => {
  const {conversationLoads} = await runAdminStartup('org_admin');

  assert.equal(conversationLoads, 0);
});

test('continues loading conversations for platform admins', async () => {
  const {conversationLoads} = await runAdminStartup('platform_admin');

  assert.equal(conversationLoads, 1);
});

test('org admin scope defaults to the tenant from the authenticated session', async () => {
  const {contextElements} = await runNavigation('workspaces', {
    authenticated: true,
    role: 'org_admin',
    tenant_id: 'tenant-org-42',
  });

  assert.equal(contextElements.get('notificationTenant').value, 'tenant-org-42');
  assert.equal(contextElements.get('monitoringTenant').value, 'tenant-org-42');
});

test('platform admin scope selection is not overwritten during session loading', async () => {
  const {contextElements} = await runNavigation('workspaces', {
    authenticated: true,
    role: 'platform_admin',
    tenant_id: 'tenant-session',
  });

  assert.equal(contextElements.get('notificationTenant').value, 'local-default');
  assert.equal(contextElements.get('monitoringTenant').value, 'local-default');
});

test('quality access notice is visible only to org admins without exposing quality tabs', async () => {
  const orgResult = await runNavigation('workspaces', {authenticated: true, role: 'org_admin'});
  const platformResult = await runNavigation('workspaces', {authenticated: true, role: 'platform_admin'});
  const html = fs.readFileSync(templatePath, 'utf8');

  assert.equal(orgResult.qualityAccessNotice.hidden, false);
  assert.equal(platformResult.qualityAccessNotice.hidden, true);
  assert.match(html, /data-org-admin-only[^>]*>[^<]*质量评测仅限平台管理员访问/);
  assert.match(html, /const platformOnlyTabs = new Set\([^;]*'retrieval','e2e','agentEval'/s);
});

test('reflection rule enum labels follow the selected language and preserve unknown values', () => {
  const zhLabel = loadReflectionRuleLabel('zh-CN');
  const enLabel = loadReflectionRuleLabel('en-US');
  const html = fs.readFileSync(templatePath, 'utf8');

  assert.equal(zhLabel('safety', 'category'), '安全与越权');
  assert.equal(zhLabel('critical', 'severity'), '严重');
  assert.equal(zhLabel('chat', 'capability'), '问答');
  assert.equal(zhLabel('writing', 'capability'), '写作');
  assert.equal(zhLabel('presentation', 'capability'), 'PPT');
  assert.equal(enLabel('evidence', 'category'), 'Evidence & Sources');
  assert.equal(enLabel('high', 'severity'), 'High');
  assert.equal(enLabel('chat', 'capability'), 'Chat');
  assert.equal(zhLabel('custom-boundary', 'category'), 'custom-boundary');
  assert.equal(zhLabel('', 'category'), '-');
  assert.match(html, /reflectionRuleLabel\(item\.category, 'category'\)/);
  assert.match(html, /reflectionRuleLabel\(item\.severity, 'severity'\)/);
  assert.match(html, /map\(mode => reflectionRuleLabel\(mode, 'capability'\)\)/);
});

test('Agent Eval errors use a readable summary without exposing raw exceptions', () => {
  const summarize = loadAgentEvalErrorSummary();
  const html = fs.readFileSync(templatePath, 'utf8');

  assert.equal(summarize('judge response did not contain JSON'), 'Judge 返回格式异常');
  assert.equal(summarize('unrecognized private endpoint token-123'), '评测执行失败，请检查配置后重试');
  assert.match(html, /agentEvalErrorSummary\(item\.error\)/);
  assert.doesNotMatch(html, /escHtml\(item\.error \|\| ''\)/);
});

test('E2E quality metric names follow language and clarify hallucination direction', () => {
  const zhNames = loadE2eMetricNames('zh-CN');
  const enNames = loadE2eMetricNames('en-US');
  const html = fs.readFileSync(templatePath, 'utf8');

  assert.equal(zhNames.context_precision, '上下文精确度');
  assert.equal(zhNames.context_recall, '上下文召回率');
  assert.equal(zhNames.faithfulness, '忠实度');
  assert.equal(zhNames.relevancy, '相关性');
  assert.equal(zhNames.hallucination, '无幻觉率（越高越好）');
  assert.equal(enNames.hallucination, 'No-Hallucination Rate (higher is better)');
  assert.match(html, /hallucination: 'e2eHCLabel'/);
  assert.match(html, /data:keys\.map\(k=>metricNames\[k\]\)/);
});

test('monitoring task machine id is secondary to a readable title', () => {
  const display = loadOperationsTaskDisplay();
  const html = fs.readFileSync(templatePath, 'utf8');
  const id = 'mir-d5c7e9bcca2a4270';

  assert.match(html, /const display = operationsTaskDisplay\(item\)/);
  assert.match(html, /escHtml\(display\.title\)/);
  assert.match(html, /escHtml\(display\.sourceId\)/);
  assert.deepEqual(
    JSON.parse(JSON.stringify(display({
      source_type: 'monitoring_report',
      source_id: id,
      title: `审阅监控预案：${id}`,
    }))),
    {title: '审阅监控预案', sourceId: id},
  );
  assert.deepEqual(
    JSON.parse(JSON.stringify(display({
      source_type: 'monitoring_report',
      source_id: id,
      title: '审阅监控预案：客户风险评估',
    }))),
    {title: '审阅监控预案：客户风险评估', sourceId: id},
  );
  assert.deepEqual(
    JSON.parse(JSON.stringify(display({
      source_type: 'knowledge_gap',
      source_id: 'doc-123',
      title: '补充资料',
    }))),
    {title: '补充资料', sourceId: ''},
  );
});
