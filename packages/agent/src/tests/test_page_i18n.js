const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const i18nPath = path.join(__dirname, '..', 'static', 'page_i18n.js');
const sources = [
  ['SMTP 主机', 'host'],
  ['端口', 'port'],
  ['加密方式', 'security'],
  ['发件人', 'sender'],
  ['用户名', 'username'],
  ['密码', 'password'],
  ['收件人（逗号分隔）', 'recipients'],
  ['启用邮件通知', 'enabled'],
];

function makeLabel(source, controlId) {
  const label = {dataset: {i18n: source}, attributes: {}, childNodes: []};
  const text = {nodeType: 3, nodeValue: source, parentElement: label};
  const control = {nodeType: 1, tagName: 'INPUT', id: controlId};
  label.childNodes = [text, control];
  Object.defineProperty(label, 'textContent', {
    get() {
      return this.childNodes.map((node) => node.nodeValue || '').join('');
    },
    set(value) {
      this.childNodes = [{nodeType: 3, nodeValue: value, parentElement: this}];
    },
  });
  label.setAttribute = (name, value) => {
    label.attributes[name] = value;
  };
  return label;
}

function loadI18n(labels) {
  const document = {
    documentElement: {},
    body: {},
    readyState: 'loading',
    addEventListener() {},
    querySelectorAll(selector) {
      if (selector === '[data-i18n]') return labels;
      return [];
    },
    createTreeWalker() {
      const textNodes = labels.flatMap((label) =>
        label.childNodes.filter((node) => node.nodeType === 3),
      );
      let index = 0;
      return {nextNode: () => textNodes[index++] || null};
    },
  };
  const window = {};
  vm.runInNewContext(fs.readFileSync(i18nPath, 'utf8'), {
    document,
    window,
    NodeFilter: {SHOW_TEXT: 4},
  });
  return window.SecureNexusPageI18n;
}

test('language changes translate labels without removing nested form controls', () => {
  const labels = sources.map(([source, id]) => makeLabel(source, id));
  const i18n = loadI18n(labels);

  i18n.apply('en-US');
  assert.deepEqual(
    labels.map((label) => label.childNodes.find((node) => node.id)?.id),
    sources.map(([, id]) => id),
  );
  assert.deepEqual(
    labels.map((label) => label.childNodes.find((node) => node.nodeType === 3)?.nodeValue),
    ['SMTP Host', 'Port', 'Security', 'Sender', 'Username', 'Password', 'Recipients (comma-separated)', 'Enable email notifications'],
  );

  i18n.apply('zh-CN');
  assert.deepEqual(
    labels.map((label) => label.childNodes.find((node) => node.id)?.id),
    sources.map(([, id]) => id),
  );
  assert.deepEqual(
    labels.map((label) => label.childNodes.find((node) => node.nodeType === 3)?.nodeValue),
    sources.map(([source]) => source),
  );
});
