const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {JSDOM} = require('jsdom');
/* global setTimeout */

function harness({engine='qwen', failSpeech=false}={}) {
  const html = fs.readFileSync('launcher.html','utf8');
  const dom = new JSDOM(html.replace(/<script>[\s\S]*?<\/script>/,''),{url:'http://localhost:8770',runScripts:'outside-only'});
  const w = dom.window, spoken = [], played = [];
  w.fetch = async (url, opts={}) => {
    if (url === '/api/speech/status') return {ok:true, json:async()=>({engine})};
    if (url === '/api/speech') { spoken.push(JSON.parse(opts.body).text); return failSpeech ? {ok:false,status:502} : {ok:true, blob:async()=>new w.Blob(['wav'])}; }
    return {ok:true, json:async()=>[]};
  };
  w.URL.createObjectURL = () => 'blob:' + spoken.length;
  w.Audio = class { constructor(src){ this.src=src; this.paused=true; played.push(this); } async play(){ this.paused=false; } pause(){ this.paused=true; } };
  w.eval(html.match(/<script>([\s\S]*?)<\/script>/)[1].replace(/boot\(\);\s*$/, '')+'\nwindow.api={speechChunks,speakControls,speech,setSpeechRate};');
  return {w, spoken, played};
}
const settle = () => new Promise(r => setTimeout(r, 0));

test('replies are chunked by sentence, markdown stripped, a short first chunk for a quick start', () => {
  const {w} = harness();
  const long = 'A'.repeat(140) + '. ' + 'Second sentence here. '.repeat(30);
  const chunks = w.api.speechChunks(long);
  assert.ok(chunks[0].length <= 150, 'the first chunk is short so audio starts quickly');
  assert.ok(chunks.every(c => c.length <= 400), 'every chunk fits the voice service limit');
  assert.deepEqual([...w.api.speechChunks('Plants make food from light. They take in sunlight, water, and carbon dioxide, and turn them into sugar and oxygen. It happens in chloroplasts.')], ['Plants make food from light.', 'They take in sunlight, water, and carbon dioxide, and turn them into sugar and oxygen. It happens in chloroplasts.'], 'the opener is spoken while the rest renders');
  assert.equal(w.api.speechChunks('**Bold** and [a link](http://x). `code`\n\n```js\nx()\n```\nDone.').join(' '), 'Bold and a link. code Done.');
  const unbroken = w.api.speechChunks('word '.repeat(300));
  assert.ok(unbroken.length > 1 && unbroken.every(c => c.length <= 400), 'a sentence with no stops is split at spaces');
});

test('play renders chunks through the engine and pause pauses the audio', async () => {
  const {w, spoken, played} = harness();
  const row = w.api.speakControls('First sentence. Second one.');
  w.document.body.append(row);
  const [play, pause] = row.querySelectorAll('button');
  play.click(); await settle(); await settle(); await settle();
  assert.deepEqual(spoken, ['First sentence. Second one.']);
  assert.equal(played.length, 1); assert.equal(played[0].paused, false);
  assert.equal(play.getAttribute('aria-pressed'), 'true');
  pause.click();
  assert.equal(played[0].paused, true); assert.equal(pause.getAttribute('aria-pressed'), 'true');
  play.click(); await settle();
  assert.equal(played[0].paused, false, 'play resumes where it paused');
  assert.equal(played.length, 1, 'resuming does not re-render');
});

test('the speed multiplier applies to playing audio and is shared by every reply', async () => {
  const {w, played} = harness();
  const a = w.api.speakControls('One.'), b = w.api.speakControls('Two.');
  w.document.body.append(a, b);
  a.querySelector('button').click(); await settle(); await settle(); await settle();
  const select = b.querySelector('select');
  select.value = '1.5'; select.dispatchEvent(new w.Event('change'));
  assert.equal(played[0].playbackRate, 1.5, 'speed changes take effect mid-reply');
  assert.equal(a.querySelector('select').value, '1.5');
  assert.equal(w.localStorage.getItem('app-engine:speech-rate'), '1.5');
});

test('starting another reply stops the one that is speaking', async () => {
  const {w, played} = harness();
  const a = w.api.speakControls('One.'), b = w.api.speakControls('Two.');
  w.document.body.append(a, b);
  a.querySelector('button').click(); await settle(); await settle(); await settle();
  b.querySelector('button').click(); await settle(); await settle(); await settle();
  assert.equal(played[0].paused, true); assert.equal(played[1].paused, false);
  assert.equal(a.querySelector('button').getAttribute('aria-pressed'), 'false');
});

test('without a voice service the browser voice reads at the chosen rate', async () => {
  const {w} = harness({engine:'browser'});
  const said = [];
  w.SpeechSynthesisUtterance = class { constructor(t){ this.text=t; } };
  w.speechSynthesis = {speak:u=>said.push(u), cancel(){}, pause(){}, resume(){}};
  w.api.setSpeechRate(2);
  const row = w.api.speakControls('Hello there.');
  row.querySelector('button').click(); await settle();
  assert.equal(said[0].text, 'Hello there.'); assert.equal(said[0].rate, 2);
});
