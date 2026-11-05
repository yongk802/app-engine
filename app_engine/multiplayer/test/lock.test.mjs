import assert from 'node:assert/strict';
import test from 'node:test';
import {spawn} from 'node:child_process';
import {existsSync} from 'node:fs';
import {mkdtemp,readFile,rm,utimes,writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {setTimeout} from 'node:timers';
import {fileURLToPath,URL} from 'node:url';
import {claimDataDir,DataDirBusy,LOCK_FILE} from '../lock.mjs';

const here=fileURLToPath(new URL('.',import.meta.url));
const start=(dir,{stdin='ignore',env={}}={})=>new Promise((resolve,reject)=>{
 const child=spawn(process.execPath,[join(here,'..','server.mjs'),'--rules',join(here,'fake-rules.mjs'),'--data-dir',dir,'--port','0'],
  {stdio:[stdin,'pipe','pipe'],env:{...process.env,...env}});
 let out='',err='';
 child.stdout.on('data',d=>{out+=d;if(/listening on /.test(out))resolve(child);});
 child.stderr.on('data',d=>{err+=d;});
 child.on('exit',code=>reject(new Error(`exited ${code}: ${err.trim()}`)));
});
const exited=child=>child.exitCode!==null||child.signalCode!==null?Promise.resolve(child.exitCode):new Promise(r=>child.once('exit',r));

test('a second service on the same data directory is refused while the first lives',async()=>{
 const dir=await mkdtemp(join(tmpdir(),'app-engine-mp-lock-'));
 let first,third;
 try{
  first=await start(dir);
  const held=JSON.parse(await readFile(join(dir,LOCK_FILE),'utf8'));
  assert.equal(held.pid,first.pid);
  await assert.rejects(start(dir),new RegExp(`Another room service \\(pid ${first.pid}\\)`));
  first.kill('SIGKILL');await exited(first);
  assert.ok(existsSync(join(dir,LOCK_FILE)),'SIGKILL leaves the lock behind');
  third=await start(dir);
  assert.equal(JSON.parse(await readFile(join(dir,LOCK_FILE),'utf8')).pid,third.pid,'a dead holder is taken over');
  third.kill('SIGTERM');await exited(third);
  // Windows has no SIGTERM handler: kill() terminates outright, and the dead-pid takeover above covers it.
  if(process.platform!=='win32')assert.ok(!existsSync(join(dir,LOCK_FILE)),'a clean stop releases the lock');
 }finally{first?.kill('SIGKILL');third?.kill('SIGKILL');await rm(dir,{recursive:true,force:true});}
});

test('with EXIT_WITH_PARENT the service stops when its stdin pipe closes',async()=>{
 const dir=await mkdtemp(join(tmpdir(),'app-engine-mp-lock-'));
 let child;
 try{
  child=await start(dir,{stdin:'pipe',env:{APP_ENGINE_MULTIPLAYER_EXIT_WITH_PARENT:'1'}});
  child.stdin.end();
  assert.equal(await exited(child),0);
  assert.ok(!existsSync(join(dir,LOCK_FILE)));
 }finally{child?.kill('SIGKILL');await rm(dir,{recursive:true,force:true});}
});

test('without the flag a standalone service ignores stdin',async()=>{
 const dir=await mkdtemp(join(tmpdir(),'app-engine-mp-lock-'));
 let child;
 try{
  child=await start(dir,{stdin:'pipe'});
  child.stdin.end();
  await new Promise(r=>setTimeout(r,300));
  assert.equal(child.exitCode,null);
 }finally{child?.kill('SIGKILL');await rm(dir,{recursive:true,force:true});}
});

test('a live holder is stale once its heartbeat stops; a taken-over holder is told',async()=>{
 const dir=await mkdtemp(join(tmpdir(),'app-engine-mp-lock-'));
 try{
  const file=join(dir,LOCK_FILE);
  await writeFile(file,JSON.stringify({pid:process.ppid,token:'other'}));
  await assert.rejects(claimDataDir(dir),DataDirBusy);
  const old=new Date(Date.now()-60_000);await utimes(file,old,old);
  let lost=0;
  const lock=await claimDataDir(dir,{heartbeatMs:20,onLost:()=>{lost++;}});
  assert.equal(JSON.parse(await readFile(file,'utf8')).token,lock.token);
  await writeFile(file,JSON.stringify({pid:process.ppid,token:'thief'}));
  await new Promise(r=>setTimeout(r,100));
  assert.equal(lost,1);
  lock.release();
  assert.equal(JSON.parse(await readFile(file,'utf8')).token,'thief','release never removes a lock it no longer holds');
  await writeFile(file,'');
  const fresh=await claimDataDir(dir);fresh.release();
  assert.ok(!existsSync(file),'an unreadable lock file is treated as stale');
 }finally{await rm(dir,{recursive:true,force:true});}
});
