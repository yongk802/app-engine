const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {JSDOM} = require('jsdom');
const tick = () => new Promise(resolve => setTimeout(resolve, 5));

function harness(settings = {}) {
  const html = fs.readFileSync('launcher.html', 'utf8');
  const dom = new JSDOM(html, {url:'http://localhost:8770', runScripts:'outside-only'});
  const w = dom.window, calls = [], opened = [], timers = new Map();
  let timerId = 0;
  let current = {available:true, chat_url:'', mcp_url:'http://localhost:8770/api/mcp', enabled:false, last_connected_at:null, event_cursor:7, ...settings};
  w.AppEngineAssistantHost = {
    open: async id => opened.push(id),
    request: async (url, options={}) => {
      calls.push({url, ...options});
      if (url.includes('/events?')) return {events:[{id:8, app_id:'pool', type:'open'}], cursor:8};
      if (options.method === 'PUT') current = {...current, ...JSON.parse(options.body)};
      if (options.method === 'POST') return {key:'one-time-secret'};
      return current;
    },
  };
  w.setTimeout = fn => {timers.set(++timerId, fn); return timerId;};
  w.clearTimeout = id => timers.delete(id);
  if (fs.existsSync('app_engine/assistant_ui/panel.js')) w.eval(fs.readFileSync('app_engine/assistant_ui/panel.js', 'utf8'));
  const get = id => w.document.getElementById(id);
  const click = async id => { assert.ok(get(id), `Missing ${id}`); get(id).click(); await tick(); };
  return {w, calls, opened, timers, get, click, close:() => w.close()};
}

test('Chat rail is lazy until expansion and settings begin with the current event cursor', async () => {
  const h = harness();
  try {
    assert.ok(h.get('assistant-toggle'), 'Chat rail button should exist');
    assert.equal(h.get('assistant-toggle').getAttribute('aria-expanded'), 'false');
    assert.equal(h.calls.length, 0);
    assert.equal(h.w.document.querySelector('#assistant-panel iframe'), null);
    await h.click('assistant-toggle');
    assert.equal(h.calls[0].url, '/api/assistant/settings');
    assert.ok(h.calls.some(c => c.url === '/api/assistant/events?after=7'));
    assert.deepEqual(h.opened, ['pool']);
    assert.equal(h.w.document.querySelector('#assistant-panel iframe'), null);
  } finally { h.close(); }
});

test('Connect saves the URL, retains the conversation on collapse, and Disconnect removes it', async () => {
  const h = harness();
  try {
    await h.click('assistant-toggle');
    h.get('assistant-chat-url').value = 'https://chat.example/conversation';
    await h.click('assistant-connect');
    const frame = h.w.document.querySelector('#assistant-panel iframe');
    assert.ok(frame);
    assert.equal(frame.src, 'https://chat.example/conversation');
    assert.equal(frame.getAttribute('sandbox'), 'allow-scripts allow-same-origin allow-forms allow-popups');
    assert.equal(frame.getAttribute('referrerpolicy'), 'no-referrer');
    assert.ok(h.calls.some(c => c.method === 'PUT' && JSON.parse(c.body).chat_url === frame.src));
    assert.equal(h.get('assistant-external').href, frame.src);
    await h.click('assistant-collapse');
    assert.equal(h.get('assistant-panel').hidden, true);
    assert.equal(h.timers.size, 0);
    assert.equal(h.w.document.querySelector('#assistant-panel iframe'), frame);
    await h.click('assistant-toggle');
    assert.equal(h.w.document.querySelector('#assistant-panel iframe'), frame);
    await h.click('assistant-disconnect');
    assert.equal(h.w.document.querySelector('#assistant-panel iframe'), null);
  } finally { h.close(); }
});

test('key is shown once in settings and never transferred to the embedded chat', async () => {
  const h = harness({chat_url:'https://chat.example/'});
  try {
    await h.click('assistant-toggle');
    await h.click('assistant-connect');
    await h.click('assistant-key-generate');
    assert.equal(h.get('assistant-key').value, 'one-time-secret');
    assert.equal(h.w.document.querySelector('#assistant-panel iframe').src, 'https://chat.example/');
    await h.click('assistant-collapse');
    assert.equal(h.get('assistant-key').value, '');
    await h.click('assistant-toggle');
    await h.click('assistant-key-revoke');
    assert.ok(h.calls.some(c => c.url === '/api/assistant/key' && c.method === 'DELETE'));
    assert.match(h.get('assistant-status').textContent, /revoked/i);
  } finally { h.close(); }
});

test('public installations explain unavailability without loading a chat or polling', async () => {
  const h = harness({available:false});
  try {
    await h.click('assistant-toggle');
    assert.match(h.get('assistant-status').textContent, /public/i);
    assert.equal(h.get('assistant-connect').disabled, true);
    assert.equal(h.calls.length, 1);
    assert.equal(h.timers.size, 0);
  } finally { h.close(); }
});

test('launcher exposes only catalog opening and authenticated requests to panel assets', async () => {
  const html = fs.readFileSync('launcher.html', 'utf8');
  assert.match(html, /src="\/assistant-assets\/panel.js"/);
  assert.match(html, /href="\/assistant-assets\/panel.css"/);
  const dom = new JSDOM(html, {url:'http://localhost:8770', runScripts:'outside-only'});
  try {
    const w = dom.window, calls = [];
    w.fetch = async (path, options) => { calls.push({path,options}); return {ok:true,status:200,json:async()=>({available:true})}; };
    w.eval(html.match(/<script>([\s\S]*?)<\/script>/)[1].replace(/boot\(\);\s*$/, ''));
    assert.deepEqual(Object.keys(w.AppEngineAssistantHost).sort(), ['open', 'request']);
    await w.AppEngineAssistantHost.request('/api/assistant/settings');
    assert.equal(calls[0].options.headers['X-App-Engine-Admin'], '__APP_ENGINE_ADMIN_CAPABILITY__');
    await assert.rejects(w.AppEngineAssistantHost.open('missing'), /not found/i);
  } finally { dom.window.close(); }
});


test('a collapsed panel ignores an in-flight open event and resumes without duplicate opens', async () => {
  const h = harness();
  try {
    await h.click('assistant-toggle');
    let finish;
    const original = h.w.AppEngineAssistantHost.request;
    h.w.AppEngineAssistantHost.request = (url, options) => url.includes('/events?') ? new Promise(resolve => { finish = resolve; }) : original(url, options);
    const scheduled = [...h.timers.values()][0];
    h.timers.clear();
    scheduled();
    await h.click('assistant-collapse');
    finish({events:[{id:9, type:'open', app_id:'nano-llm'}], cursor:9});
    await tick();
    assert.deepEqual(h.opened, ['pool']);
    assert.equal(h.timers.size, 0);
    h.w.AppEngineAssistantHost.request = original;
    await h.click('assistant-toggle');
    assert.deepEqual(h.opened, ['pool']);
  } finally { h.close(); }
});

test('rejected settings never create an iframe and surface the server error', async () => {
  const h = harness();
  try {
    await h.click('assistant-toggle');
    h.get('assistant-chat-url').value = 'http://remote.example/';
    h.w.AppEngineAssistantHost.request = async () => { throw new Error('HTTPS required'); };
    await h.click('assistant-connect');
    assert.equal(h.w.document.querySelector('#assistant-panel iframe'), null);
    assert.match(h.get('assistant-status').textContent, /HTTPS required/);
    assert.equal(h.get('assistant-connect').disabled, false);
  } finally { h.close(); }
});

test('Disconnect wins over an earlier Connect whose settings save finishes late', async () => {
  const h = harness({chat_url:'https://chat.example/'});
  try {
    await h.click('assistant-toggle');
    await h.click('assistant-connect');
    h.get('assistant-settings').open = true;
    let finish;
    const original = h.w.AppEngineAssistantHost.request;
    h.w.AppEngineAssistantHost.request = (url, options) => options?.method === 'PUT'
      ? new Promise(resolve => { finish = resolve; }) : original(url, options);
    h.get('assistant-chat-url').value = 'https://chat.example/new';
    await h.click('assistant-connect');
    await h.click('assistant-disconnect');
    finish({chat_url:'https://chat.example/new'});
    await tick();
    assert.equal(h.w.document.querySelector('#assistant-panel iframe'), null);
    assert.match(h.get('assistant-status').textContent, /disconnected/i);
  } finally { h.close(); }
});

test('an app open already dispatched is not repeated after collapsing during its launch', async () => {
  const h = harness();
  let finish;
  try {
    h.w.AppEngineAssistantHost.open = id => { h.opened.push(id); return new Promise(resolve => { finish = resolve; }); };
    await h.click('assistant-toggle');
    assert.deepEqual(h.opened, ['pool']);
    await h.click('assistant-collapse');
    finish();
    await tick();
    h.w.AppEngineAssistantHost.open = async id => h.opened.push(id);
    await h.click('assistant-toggle');
    assert.deepEqual(h.opened, ['pool']);
  } finally { h.close(); }
});
