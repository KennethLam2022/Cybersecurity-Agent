const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const templatePath = path.join(__dirname, '..', 'templates', 'admin.html');
const template = fs.readFileSync(templatePath, 'utf8');
const loader = template.match(/<script>([\s\S]*?)<\/script>/i)?.[1];

assert.ok(loader, 'admin template must define the chart loader');

function createLoader(failAt = -1) {
  const scripts = [];
  let context;
  context = {
    console: { warn() {} },
    document: {
      createElement: () => ({}),
      head: {
        appendChild(script) {
          scripts.push(script);
          queueMicrotask(() => {
            if (scripts.length - 1 === failAt) {
              script.onerror();
              return;
            }
            if (scripts.length === 1) context.echarts = {};
            script.onload();
          });
        },
      },
    },
  };
  vm.runInNewContext(loader, context);
  return { context, scripts };
}

test('loads pinned chart resources with SRI and anonymous CORS', async () => {
  const { context, scripts } = createLoader();
  await context.loadECharts();

  assert.equal(context._eChartLoaded, true);
  assert.deepEqual(
    scripts.map(({ src, integrity, crossOrigin }) => ({ src, integrity, crossOrigin })),
    [
      {
        src: 'https://cdn.jsdelivr.net/npm/echarts@5.6.0/dist/echarts.min.js',
        integrity: 'sha384-pPi0zxBAoDu6+JXW/C68UZLvBUUtU+7zonhif43rqj7pxsGyqyqzcian2Rj37Rss',
        crossOrigin: 'anonymous',
      },
      {
        src: 'https://cdn.jsdelivr.net/npm/echarts-wordcloud@2.1.0/dist/echarts-wordcloud.min.js',
        integrity: 'sha384-U1KEY0DDCF4Dq6Yx1J+EZ5Hnj8X5bMn52OAcJB8C4OiAWeU4iJhJ/Tv5KhTqu8zZ',
        crossOrigin: 'anonymous',
      },
    ],
  );
});

test('keeps charts disabled when ECharts fails integrity or network loading', async () => {
  const { context, scripts } = createLoader(0);
  await context.loadECharts();

  assert.equal(context._eChartLoaded, false);
  assert.equal(context.echarts, undefined);
  assert.equal(scripts.length, 1);
});

test('keeps charts disabled when the wordcloud extension fails loading', async () => {
  const { context, scripts } = createLoader(1);
  await context.loadECharts();

  assert.equal(context._eChartLoaded, false);
  assert.equal(scripts.length, 2);
});
