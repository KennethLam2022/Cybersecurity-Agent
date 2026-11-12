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

async function runNavigation(requestedTab) {
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
  ]);
  const url = new URL(`http://localhost/admin?tab=${requestedTab}`);
  const selected = [];
  const messages = [];
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
      json: async () => ({authenticated: true, role: 'org_admin'}),
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
  return {tabs, url, selected, messages};
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
