import assert from 'node:assert/strict';
import test from 'node:test';
import {spawn} from 'node:child_process';
import {mkdtemp,rm,writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {checkRules,createService} from '../server.mjs';
import {engine} from './fake-rules.mjs';

const here=fileURLToPath(new URL('.',import.meta.url));
const compatibility={protocol:1,rulesVersion:engine.rulesVersion,catalogDigest:engine.catalogDigest};
const start=(args,dir)=>new Promise((resolve,reject)=>{
 const child=spawn(process.execPath,[join(here,'..','server.mjs'),...args],{stdio:['ignore','pipe','pipe']});
 let out='',err='';
 child.stdout.on('data',d=>{out+=d;const m=/listening on ([^:]+):(\d+)/.exec(out);if(m)resolve({child,host:m[1],port:Number(m[2])});});
 child.stderr.on('data',d=>{err+=d;});
 child.on('exit',code=>reject(new Error(`exited ${code}: ${err.trim()}`)));
});
const command=(base,body,token)=>fetch(`${base}/v1/command`,{method:'POST',headers:{'Content-Type':'application/json',...(token?{Authorization:`Bearer ${token}`}:{})},body:JSON.stringify(body)}).then(r=>r.json());

test('the launcher loads a rules module, picks a free port and answers health and commands',async()=>{
 const dir=await mkdtemp(join(tmpdir(),'app-engine-mp-'));
 let proc;
 try{
  proc=await start(['--rules',join(here,'fake-rules.mjs'),'--data-dir',dir,'--port','0'],dir);
  const base=`http://${proc.host}:${proc.port}`;
  assert.deepEqual(await (await fetch(`${base}/health`)).json(),{service:'night-city-rooms',...compatibility});
  const created=await command(base,{op:'create',name:'Alex',...compatibility});
  assert.equal(created.ok,true);assert.equal(created.result.seat,0);assert.match(created.result.token,/^[A-Za-z0-9_-]{43}$/);
  const snap=await command(base,{op:'snapshot',roomId:created.result.roomId},created.result.token);
  assert.equal(snap.result.status,'lobby');
  const denied=await command(base,{op:'snapshot',roomId:created.result.roomId});
  assert.equal(denied.error.code,'AUTH_REQUIRED');
  const cross=await fetch(`${base}/v1/command`,{method:'POST',headers:{'Content-Type':'application/json',Origin:'http://evil.example'},body:'{"op":"create"}'});
  assert.equal(cross.status,403,'direct browser requests are refused; hosts relay without an Origin');
 }finally{proc?.child.kill('SIGTERM');await rm(dir,{recursive:true,force:true});}
});

test('a rules module missing the contract is refused before anything listens',async()=>{
 const dir=await mkdtemp(join(tmpdir(),'app-engine-mp-'));
 try{
  const broken=join(dir,'broken.mjs');
  await writeFile(broken,"export default {createMatch(){}, rulesVersion:'x', catalogDigest:'y'};\n");
  await assert.rejects(start(['--rules',broken,'--data-dir',dir,'--port','0']),/missing legalActions/);
  await assert.rejects(start(['--data-dir',dir,'--port','0']),/--rules module is required/);
  assert.throws(()=>checkRules({...engine,catalogDigest:''}),/catalogDigest/);
  assert.throws(()=>checkRules({...engine,daily:{validDay(){}}}),/daily plugin/);
 }finally{await rm(dir,{recursive:true,force:true});}
});

test('without an AI policy or daily plugin the service says so instead of failing oddly',async()=>{
 const dir=await mkdtemp(join(tmpdir(),'app-engine-mp-'));
 try{
  const plain=join(dir,'plain.mjs');
  await writeFile(plain,`import {engine} from '${join(here,'fake-rules.mjs')}';\nconst {chooseAction,...rest}=engine;\nexport default rest;\n`);
  const service=await createService({rules:plain,dataDir:dir});
  assert.equal(service.chooseAction,null);
  await assert.rejects(service.dispatch('dailyBoard',{day:'2026-09-13',playerId:'p'.repeat(20),...compatibility}),e=>e.code==='MULTIPLAYER_UNAVAILABLE');
  await assert.rejects(service.dispatch('tourneyCreate',{name:'Cup',playerName:'A',format:'single',ai:true,...compatibility}),e=>e.code==='MULTIPLAYER_UNAVAILABLE');
  const human=await service.dispatch('tourneyCreate',{name:'Cup',playerName:'A',format:'single',ai:false,...compatibility});
  assert.equal(human.snapshot.ai,false);
  const withAI=await createService({rules:plain,ai:join(here,'fake-rules.mjs'),dataDir:join(dir,'ai')});
  assert.equal(typeof withAI.chooseAction,'function','a separate ai module supplies the policy');
 }finally{await rm(dir,{recursive:true,force:true});}
});
