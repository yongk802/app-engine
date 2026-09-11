const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {JSDOM} = require('jsdom');

const APPS = [
  {id:'pool', label:'Pool', icon:'🎱', description:'8-ball pool.', categories:['games'], url:'/apps/pool/'},
  {id:'nano-llm', label:'Build a Tiny LLM', icon:'🔬', description:'Train a GPT.', categories:['learning','ai'], url:'/apps/nano-llm/'},
];
const CATEGORIES = [
  {id:'games', label:'Games', description:'', count:1},
  {id:'learning', label:'Learning', description:'', count:1},
  {id:'ai', label:'AI & Models', description:'', count:1},
  {id:'security', label:'Security', description:'', count:0},
];

function respond(body) { return {ok:true, status:200, json:async()=>body}; }

function harness() {
  const html = fs.readFileSync('launcher.html','utf8');
  const dom = new JSDOM(html.replace(/<script>[\s\S]*?<\/script>/,''),{url:'http://localhost:8770',runScripts:'outside-only'});
  const w = dom.window;
  const calls = [];
  w.fetch = async url => {
    calls.push(url);
    if (url === '/api/apps') return respond(APPS);
    if (url === '/api/apps/categories') return respond(CATEGORIES);
    if (url.startsWith('/api/apps?')) return respond([APPS[1]]);
    return respond([]);
  };
  w.eval(html.match(/<script>([\s\S]*?)<\/script>/)[1].replace(/boot\(\);\s*$/, '')+'\nwindow.api={boot,runSearch,state};');
  return {w, calls, close:()=>w.close()};
}

const appIds = w => [...w.document.querySelectorAll('#applist .app-btn')].map(b => b.dataset.appId);

test('category filter offers only used categories and the list groups by label', async () => {
  const h = harness();
  await h.w.api.boot();
  const select = h.w.document.querySelector('#app-category');
  assert.equal(select.hidden, false);
  assert.deepEqual([...select.options].map(o => [o.value, o.textContent]), [
    ['', 'All categories (2)'], ['games', 'Games (1)'], ['learning', 'Learning (1)'], ['ai', 'AI & Models (1)'],
  ]);
  assert.deepEqual([...h.w.document.querySelectorAll('#applist .cat-head')].map(n => n.textContent), ['Games', 'Learning']);
  h.close();
});

test('search and category filter render server-ranked results, and clearing restores the full list', async () => {
  const h = harness();
  await h.w.api.boot();
  h.w.api.state.query = 'gpt';
  h.w.api.state.category = 'ai';
  await h.w.api.runSearch();
  assert.equal(h.calls.at(-1), '/api/apps?q=gpt&category=ai');
  assert.deepEqual(appIds(h.w), ['nano-llm']);
  assert.equal(h.w.document.querySelectorAll('#applist .cat-head').length, 0);
  h.w.api.state.query = '';
  h.w.api.state.category = '';
  await h.w.api.runSearch();
  assert.deepEqual(appIds(h.w), ['pool', 'nano-llm']);
  h.close();
});

test('a slower earlier search cannot overwrite newer results', async () => {
  const h = harness();
  await h.w.api.boot();
  const pending = [];
  h.w.fetch = url => new Promise(resolve => pending.push({url, resolve}));
  h.w.api.state.query = 'pool';
  const first = h.w.api.runSearch();
  h.w.api.state.query = 'llm';
  const second = h.w.api.runSearch();
  pending[1].resolve(respond([APPS[1]]));
  await second;
  pending[0].resolve(respond([APPS[0]]));
  await first;
  assert.deepEqual(appIds(h.w), ['nano-llm']);
  h.close();
});
