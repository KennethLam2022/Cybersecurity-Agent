const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const templatePath = path.join(__dirname, '..', 'templates', 'index.html');

function loadUiText(language) {
  const html = fs.readFileSync(templatePath, 'utf8');
  const start = html.indexOf('const UI_TRANSLATIONS = {');
  const end = html.indexOf('function applyUiLanguage()', start);
  assert.ok(start >= 0 && end > start);
  const sandbox = {responseLanguage: language};
  vm.runInNewContext(html.slice(start, end), sandbox);
  return sandbox.uiText;
}

function applyChatLanguage(language, emptyState) {
  const html = fs.readFileSync(templatePath, 'utf8');
  const start = html.indexOf('const UI_TRANSLATIONS = {');
  const end = html.indexOf('const nativeFetch =', start);
  assert.ok(start >= 0 && end > start);
  const sandbox = {
    responseLanguage: language,
    document: {
      getElementById: (id) => id === 'chatEmptyState' ? emptyState : null,
      querySelector: () => null,
    },
  };
  vm.runInNewContext(html.slice(start, end), sandbox);
  sandbox.applyUiLanguage();
  return emptyState.textContent;
}

function loadChatStateHelpers(container, language) {
  const html = fs.readFileSync(templatePath, 'utf8');
  const start = html.indexOf('function resetChatMessages() {');
  const end = html.indexOf('async function loadMessages(convId)', start);
  assert.ok(start >= 0 && end > start);
  const sandbox = {
    document: {getElementById: () => container},
    escapeHtml: (value) => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;'),
    uiText: loadUiText(language),
  };
  vm.runInNewContext(html.slice(start, end), sandbox);
  return sandbox;
}

test('empty state is accessible and localized until the first message is appended', () => {
  const html = fs.readFileSync(templatePath, 'utf8');
  let emptyStateRemoved = false;
  const events = [];
  const emptyState = {
    remove() {
      emptyStateRemoved = true;
      events.push('remove');
    },
  };
  const appended = [];
  const container = {
    innerHTML: '',
    querySelector(selector) {
      assert.equal(selector, '#chatEmptyState');
      return emptyState;
    },
    appendChild(node) {
      appended.push(node);
      events.push('append');
    },
  };
  const chat = loadChatStateHelpers(container, 'en-US');
  assert.match(html, /id="chatEmptyState" role="status"/);
  assert.match(html, /chatEmptyState:'emptyChat'/);

  chat.resetChatMessages();
  assert.equal(
    container.innerHTML,
    '<p class="chat-empty-state" id="chatEmptyState" role="status">Start a new conversation</p>',
  );
  loadChatStateHelpers(container, 'zh-CN').resetChatMessages();
  assert.match(container.innerHTML, /开始一段新的对话/);

  const firstMessage = {className: 'message user'};
  chat.appendChatMessage(container, firstMessage);
  assert.equal(emptyStateRemoved, true);
  assert.deepEqual(appended, [firstMessage]);
  assert.deepEqual(events, ['remove', 'append']);
});

test('language changes update the visible empty prompt', () => {
  const emptyState = {textContent: ''};

  assert.equal(applyChatLanguage('zh-CN', emptyState), '开始一段新的对话');
  assert.equal(applyChatLanguage('en-US', emptyState), 'Start a new conversation');
});
