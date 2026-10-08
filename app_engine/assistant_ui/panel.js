(() => {
  'use strict';
  const host = window.AppEngineAssistantHost;
  if (!host) return;
  const rail = document.createElement('button');
  rail.id = 'assistant-toggle';
  rail.type = 'button';
  rail.setAttribute('aria-controls', 'assistant-panel');
  rail.setAttribute('aria-expanded', 'false');
  rail.setAttribute('aria-label', 'Open external chat');
  rail.innerHTML = '<span aria-hidden="true">◌</span><span>Chat</span>';
  const panel = document.createElement('aside');
  panel.id = 'assistant-panel';
  panel.hidden = true;
  panel.setAttribute('aria-labelledby', 'assistant-title');
  panel.innerHTML = `
    <header class="assistant-header"><div><h2 id="assistant-title">Chat</h2><span>Bring your own assistant</span></div><button type="button" id="assistant-collapse" aria-label="Collapse chat">›</button></header>
    <p id="assistant-status" role="status" aria-live="polite">Configure a chat page to get started.</p>
    <details id="assistant-settings" open><summary>Connection settings</summary>
      <form id="assistant-form">
        <label for="assistant-chat-url">Chat page URL</label>
        <input id="assistant-chat-url" type="url" placeholder="https://your-chat.example" autocomplete="off" required>
        <p class="assistant-help">Use your chat service’s web page. Sign in there as usual.</p>
        <div class="assistant-actions"><button type="submit" id="assistant-connect">Connect</button><button type="button" id="assistant-disconnect" hidden>Disconnect</button></div>
      </form>
      <div class="assistant-mcp">
        <label for="assistant-mcp-url">App tools · MCP endpoint</label>
        <input id="assistant-mcp-url" type="text" readonly>
        <p id="assistant-harness" class="assistant-help">No harness connection recorded.</p>
        <p class="assistant-help">Add this endpoint and a bearer key in your assistant’s MCP settings. Your assistant must be able to reach this engine.</p>
        <div class="assistant-actions"><button type="button" id="assistant-key-generate">Generate key</button><button type="button" id="assistant-key-revoke" disabled>Revoke key</button></div>
        <div id="assistant-key-reveal" hidden><label for="assistant-key">Copy this key now — shown once</label><input id="assistant-key" type="text" readonly autocomplete="off" spellcheck="false"><p class="assistant-help">Keep it private. Rotating or revoking a key immediately disables the previous key.</p></div>
      </div>
    </details>
    <div class="assistant-link"><a id="assistant-external" target="_blank" rel="noopener noreferrer" hidden>Open chat in new tab ↗</a><p class="assistant-help">If your service blocks embedding, open it in a new tab. Loading this page does not connect its app tools.</p></div>
    <div id="assistant-frame"><div id="assistant-placeholder"><span aria-hidden="true">◌</span><h3>Your assistant, alongside your apps</h3><p>Connect a chat page above. App tools become available when you configure MCP in your assistant.</p></div></div>`;
  document.body.append(rail, panel);
  const get = id => document.getElementById('assistant-' + id);
  const status = message => { get('status').textContent = message; };
  let loaded = false, available = false, loading = null, cursor = 0, timer = null, generation = 0, frame = null, pollCount = 0, connectionGeneration = 0;
  const clearKey = () => { get('key').value = ''; get('key-reveal').hidden = true; };
  function keyState(enabled) {
    get('key-generate').textContent = enabled ? 'Rotate key' : 'Generate key';
    get('key-revoke').disabled = !enabled;
  }
  function harnessStatus(data) {
    get('harness').textContent = data.last_connected_at
      ? 'Harness last connected: ' + new Date(data.last_connected_at).toLocaleString()
      : 'No harness connection recorded.';
  }
  function externalLink(url) {
    const link = get('external');
    link.hidden = !url;
    if (url) link.href = url; else link.removeAttribute('href');
  }
  async function loadSettings() {
    const data = await host.request('/api/assistant/settings');
    available = data.available !== false;
    loaded = true;
    for (const id of ['chat-url', 'connect', 'key-generate']) get(id).disabled = !available;
    if (!available) {
      status('External chat and app tools are unavailable on public installations. Use a local engine to configure a connection.');
      return;
    }
    cursor = data.event_cursor || 0;
    get('chat-url').value = data.chat_url || '';
    get('mcp-url').value = data.mcp_url || '';
    keyState(data.enabled);
    harnessStatus(data);
    externalLink(data.chat_url);
    status(data.chat_url ? 'Chat configured. Select Connect to load it.' : 'Add a chat page URL, then select Connect.');
  }
  async function poll(token) {
    if (panel.hidden || token !== generation || !available) return;
    try {
      const data = await host.request('/api/assistant/events?after=' + cursor);
      if (panel.hidden || token !== generation) return;
      for (const event of data.events || []) {
        if (event.id <= cursor) continue;
        cursor = Math.max(cursor, event.id || 0);
        if (event.type === 'open') {
          try { await host.open(event.app_id); }
          catch (error) { status('Could not open app: ' + error.message); }
        }
        if (panel.hidden || token !== generation) return;
        cursor = Math.max(cursor, event.id || 0);
      }
      cursor = Math.max(cursor, data.cursor || 0);
      if (++pollCount % 10 === 0) {
        const settings = await host.request('/api/assistant/settings');
        if (!panel.hidden && token === generation) harnessStatus(settings);
      }
    } catch (error) {
      if (!panel.hidden && token === generation) status('App tools unavailable: ' + error.message + '. Retrying…');
    }
    if (!panel.hidden && token === generation) timer = setTimeout(() => poll(token), 1500);
  }
  function collapse() {
    panel.hidden = true;
    rail.setAttribute('aria-expanded', 'false');
    ++generation;
    clearTimeout(timer);
    timer = null;
    clearKey();
    rail.focus();
  }
  rail.onclick = async () => {
    if (!panel.hidden) { collapse(); return; }
    panel.hidden = false;
    rail.setAttribute('aria-expanded', 'true');
    get('collapse').focus();
    const token = ++generation;
    try {
      if (!loaded) {
        status('Loading connection settings…');
        if (!loading) loading = loadSettings().finally(() => { loading = null; });
        await loading;
      }
      if (!panel.hidden && token === generation) poll(token);
    } catch (error) { status('Could not load settings: ' + error.message + '. Close and reopen Chat to retry.'); }
  };
  get('collapse').onclick = collapse;
  panel.addEventListener('keydown', event => { if (event.key === 'Escape') { event.preventDefault(); collapse(); } });
  get('settings').addEventListener('toggle', () => { if (!get('settings').open) clearKey(); });
  get('form').onsubmit = async event => {
    event.preventDefault();
    if (!available) return;
    const connection = ++connectionGeneration;
    get('connect').disabled = true;
    try {
      const chatURL = get('chat-url').value.trim();
      const data = await host.request('/api/assistant/settings', {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({chat_url:chatURL})});
      if (connection !== connectionGeneration) return;
      const url = data.chat_url || chatURL;
      externalLink(url);
      if (!frame || frame.src !== url) {
        const next = document.createElement('iframe');
        next.title = 'External assistant chat';
        next.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-forms allow-popups');
        next.setAttribute('referrerpolicy', 'no-referrer');
        next.src = url;
        frame?.remove();
        frame = next;
        get('frame').append(frame);
      }
      get('placeholder').hidden = true;
      get('disconnect').hidden = false;
      get('settings').open = false;
      clearKey();
      status('Chat page opened. App tools connect separately through MCP.');
    } catch (error) { if (connection === connectionGeneration) status('Could not connect: ' + error.message); }
    finally { get('connect').disabled = !available; }
  };
  get('disconnect').onclick = () => {
    ++connectionGeneration;
    frame?.remove();
    frame = null;
    get('placeholder').hidden = false;
    get('disconnect').hidden = true;
    get('settings').open = true;
    clearKey();
    status('Chat disconnected. Your saved URL and MCP key are unchanged.');
  };
  get('key-generate').onclick = async () => {
    if (!available) return;
    clearKey();
    get('key-generate').disabled = true;
    try {
      const data = await host.request('/api/assistant/key', {method:'POST'});
      keyState(true);
      if (!panel.hidden) {
        get('settings').open = true;
        get('key').value = data.key;
        get('key-reveal').hidden = false;
        get('key').focus();
        get('key').select();
      }
      status('New MCP key generated. Copy it into your assistant’s settings.');
    } catch (error) { status('Could not generate key: ' + error.message); }
    finally { get('key-generate').disabled = !available; }
  };
  get('key-revoke').onclick = async () => {
    if (!available) return;
    clearKey();
    get('key-revoke').disabled = true;
    try {
      await host.request('/api/assistant/key', {method:'DELETE'});
      keyState(false);
      status('MCP key revoked. The harness can no longer access app tools.');
    } catch (error) { get('key-revoke').disabled = false; status('Could not revoke key: ' + error.message); }
  };
})();
