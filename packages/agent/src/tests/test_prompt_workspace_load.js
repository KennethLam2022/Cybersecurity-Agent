const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const adminHtml = fs.readFileSync(
  path.join(__dirname, "../templates/admin.html"),
  "utf8"
);
const functionMatch = adminHtml.match(
  /async function loadWorkspacePrompt\(\) \{[\s\S]*?\n\}/
);

assert.ok(functionMatch, "loadWorkspacePrompt must exist in admin.html");
const promptLoader = functionMatch[0];

function createPromptPage(fetch) {
  const elements = {
    workspacePromptEditor: { value: "" },
    workspacePromptLoadStatus: { textContent: "", style: { display: "" } },
  };
  const context = {
    document: { getElementById: (id) => elements[id] },
    fetch,
    Error,
  };
  vm.runInNewContext(`${promptLoader}; loadWorkspacePrompt()`, context);
  return elements;
}

test("loads a prompt from a successful response", async () => {
  const elements = createPromptPage(async () => ({
    ok: true,
    json: async () => ({ system_prompt: "  approved prompt  " }),
  }));
  await new Promise(setImmediate);
  assert.equal(elements.workspacePromptEditor.value, "approved prompt");
  assert.equal(elements.workspacePromptLoadStatus.style.display, "none");
});

test("reports HTTP failures", async () => {
  const elements = createPromptPage(async () => ({ ok: false, status: 403 }));
  await new Promise(setImmediate);
  assert.match(elements.workspacePromptLoadStatus.textContent, /HTTP 403/);
  assert.equal(elements.workspacePromptLoadStatus.style.display, "block");
});

test("reports network failures", async () => {
  const elements = createPromptPage(async () => {
    throw new Error("offline");
  });
  await new Promise(setImmediate);
  assert.match(elements.workspacePromptLoadStatus.textContent, /offline/);
  assert.equal(elements.workspacePromptLoadStatus.style.display, "block");
});

test("does not overwrite an existing draft", async () => {
  const elements = createPromptPage(async () => ({
    ok: true,
    json: async () => ({ prompt: "server prompt" }),
  }));
  elements.workspacePromptEditor.value = "local draft";
  await new Promise(setImmediate);
  assert.equal(elements.workspacePromptEditor.value, "local draft");
});
