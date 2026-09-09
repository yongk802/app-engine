const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {JSDOM} = require('jsdom');
function harness() {
  const html = fs.readFileSync('launcher.html','utf8');
  const dom = new JSDOM(html.replace(/<script>[\s\S]*?<\/script>/,''),{url:'http://localhost:8042',runScripts:'outside-only'});
  const w = dom.window;
  const calls=[];
  w.fetch=async (url,options)=>{calls.push([url,options]);return {ok:true,status:200,json:async()=>({entry_url:'/apps/cyberpunk-tcg/sessions/s/',session_id:'s'})};};
  w.confirm=()=>true;
  w.eval(html.match(/<script>([\s\S]*?)<\/script>/)[1].replace(/boot\(\);\s*$/, '')+'\nwindow.api={openApp,state};');
  return {w,calls,close:()=>w.close()};
}
test('managed app opens a runtime session and delegates its browser permissions', async()=>{
 const h=harness();
 await h.w.api.openApp({id:'cyberpunk-tcg',label:'Night City',engine_managed:true,allow:'microphone',sandbox:'allow-scripts allow-same-origin',url:'http://cyberpunk-tcg.localhost:8042/apps/cyberpunk-tcg/'});
 assert.equal(h.calls[0][0],'/api/app-engine/apps/cyberpunk-tcg/open');
 assert.equal(h.w.document.querySelector('iframe').getAttribute('src'),'http://cyberpunk-tcg.localhost:8042/api/app-engine/sessions/s/proxy/');
 assert.equal(h.w.document.querySelector('iframe').allow,'microphone');
 h.close();
});
test('only current Night City frame and origin can focus; exit and app switch restore chrome',async()=>{
 const h=harness(),w=h.w;
 const game={id:'cyberpunk-tcg',label:'Night City',url:'http://game.localhost:8042/'};
 await w.api.openApp(game);
 const frame=w.document.querySelector('iframe');
 const send=(source,origin,active)=>w.dispatchEvent(new w.MessageEvent('message',{source,origin,data:{type:'night-city:play-mode',active}}));
 const focused=()=>w.document.body.classList.contains('night-city-playing');
 send(w, 'http://game.localhost:8042',true); assert.equal(focused(),false);
 send(frame.contentWindow,'http://evil.test',true); assert.equal(focused(),false);
 send(frame.contentWindow,'http://game.localhost:8042','true'); assert.equal(focused(),false);
 send(frame.contentWindow,'http://game.localhost:8042',true); assert.equal(focused(),true);
 send(frame.contentWindow,'http://game.localhost:8042',false); assert.equal(focused(),false);
 send(frame.contentWindow,'http://game.localhost:8042',true);
 await w.api.openApp({id:'other',label:'Other',url:'/other'}); assert.equal(focused(),false);
 send(frame.contentWindow,'http://game.localhost:8042',true); assert.equal(focused(),false);
 h.close();
});
test('managed launch shows preview commands and does not approve when declined',async()=>{
 const h=harness(); let prompt='';
 h.w.fetch=async(url)=>{h.calls.push([url]); return url.endsWith('/open') ? {ok:false,status:409,json:async()=>({detail:{code:'approval_required',fingerprint:'abc'}})} : {ok:true,status:200,json:async()=>({fingerprint:'abc',steps:[{phase:'run',argv:['python','server.py']}]})};};
 h.w.confirm=text=>{prompt=text; return false;};
 await h.w.api.openApp({id:'cyberpunk-tcg',label:'Night City',engine_managed:true,url:'/legacy'});
 assert.match(prompt,/python server.py/);
 assert.equal(h.calls.some(([u])=>u.endsWith('/approve')),false);
 assert.equal(h.w.document.querySelector('iframe'),null);
 assert.match(h.w.document.querySelector('#empty').textContent,/cancelled/);
 h.close();
});
