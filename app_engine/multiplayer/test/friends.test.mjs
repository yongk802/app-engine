import assert from 'node:assert/strict';
import test from 'node:test';
import {mkdtemp,readFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {engine} from './fake-rules.mjs';
import {RoomService} from '../rooms.mjs';

const compatibility={protocol:1,rulesVersion:engine.rulesVersion,catalogDigest:engine.catalogDigest};
const deck=engine.generateDeck({},1701);
let sequence=0;const rid=()=>`r-${++sequence}`;

async function service({now,storageDir}={}){
 storageDir||=await mkdtemp(join(tmpdir(),'night-city-friends-'));
 const s=new RoomService({engine,...compatibility,storageDir,now});
 await s.init();
 return {s,storageDir};
}
const register=(s,name)=>s.dispatch('friendRegister',{name,...compatibility});
const code=r=>`ncf1:${r.playerId}:${r.code}`;
const roster=(s,r)=>s.dispatch('friendSnapshot',{},r.token);
const rejects=(promise,code)=>assert.rejects(promise,error=>error.code===code||assert.fail(`expected ${code}, got ${error.code}: ${error.message}`));

test('friend codes add both players to each other, presence follows the heartbeat, removal is mutual',async()=>{
 let clock=1_900_000_000_000;
 const {s,storageDir}=await service({now:()=>clock});
 try{
  const a=await register(s,'  Alex '),b=await register(s,'Blair');
  assert.equal(a.snapshot.name,'Alex');assert.deepEqual(a.snapshot.friends,[]);
  assert.match(a.token,/^[A-Za-z0-9_-]{43}$/);assert.match(a.playerId,/^[A-Za-z0-9_-]{22}$/);
  await rejects(s.dispatch('friendAdd',{requestId:rid(),code:'nope'},a.token),'INVALID_REQUEST');
  await rejects(s.dispatch('friendAdd',{requestId:rid(),code:code(a)},a.token),'INVALID_REQUEST');
  await rejects(s.dispatch('friendAdd',{requestId:rid(),code:`ncf1:${b.playerId}:wrong`},a.token),'INVALID_INVITE');
  await rejects(s.dispatch('friendSnapshot',{},'x'.repeat(43)),'AUTH_REQUIRED');
  await rejects(s.dispatch('friendSnapshot',{}),'AUTH_REQUIRED');
  const added=await s.dispatch('friendAdd',{requestId:'add-1',code:code(b)},a.token);
  assert.deepEqual(added.friends.map(f=>[f.name,f.connected]),[['Blair',true]]);
  const again=await s.dispatch('friendAdd',{requestId:'add-1',code:code(b)},a.token);
  assert.equal(again.friends.length,1,'a repeated request is not reapplied');
  assert.equal((await s.dispatch('friendAdd',{requestId:'add-1',code:code(a)},a.token)).friends.length,1,'a remembered request ID returns the roster untouched');
  const bs=await roster(s,b);
  assert.deepEqual(bs.friends.map(f=>f.name),['Alex'],'the friendship is mutual');
  clock+=60_000;
  assert.equal((await roster(s,a)).friends[0].connected,false,'no heartbeat for a minute reads as away');
  await roster(s,b);
  assert.equal((await roster(s,a)).friends[0].connected,true);
  const renamed=await s.dispatch('friendRename',{requestId:rid(),name:'Blair Q'},b.token);
  assert.equal(renamed.name,'Blair Q');
  assert.equal((await roster(s,a)).friends[0].name,'Blair Q','friends see the new name');
  const recoded=await s.dispatch('friendRecode',{requestId:rid()},b.token);
  assert.notEqual(recoded.code,b.code);assert.equal(recoded.roster.name,'Blair Q');
  const c=await register(s,'Casey');
  await rejects(s.dispatch('friendAdd',{requestId:rid(),code:code(b)},c.token),'INVALID_INVITE','the old code is revoked');
  await s.dispatch('friendAdd',{requestId:rid(),code:`ncf1:${b.playerId}:${recoded.code}`},c.token);
  assert.deepEqual((await roster(s,b)).friends.map(f=>f.name).sort(),['Alex','Casey']);
  const removed=await s.dispatch('friendRemove',{requestId:rid(),friendId:b.playerId},a.token);
  assert.deepEqual(removed.friends,[]);
  assert.deepEqual((await roster(s,b)).friends.map(f=>f.name),['Casey'],'removal clears both rosters');
  const saved=JSON.parse(await readFile(join(storageDir,'friends.json'),'utf8'));
  assert.equal(saved.players.length,3);
  assert.ok(saved.players.every(p=>!('token' in p)&&!('code' in p)&&/^[a-f0-9]{64}$/.test(p.tokenHash)),'only hashes are stored');
 }finally{await rm(storageDir,{recursive:true,force:true});}
});

test('a roster invitation seats the friend directly and the match plays like any other table',async()=>{
 let clock=1_900_000_000_000;
 const {s,storageDir}=await service({now:()=>clock});
 try{
  const a=await register(s,'Alex'),b=await register(s,'Blair'),c=await register(s,'Casey');
  await rejects(s.dispatch('friendInvite',{requestId:rid(),friendId:b.playerId},a.token),'FORBIDDEN','only friends can be invited');
  await s.dispatch('friendAdd',{requestId:rid(),code:code(b)},a.token);
  await rejects(s.dispatch('friendInvite',{requestId:rid(),friendId:b.playerId,turnTimer:'1h'},a.token),'INVALID_REQUEST');
  const invite=await s.dispatch('friendInvite',{requestId:'inv-1',friendId:b.playerId,turnTimer:'24h'},a.token);
  assert.equal(invite.seat,0);assert.equal(invite.snapshot.status,'lobby');assert.equal(invite.snapshot.turnTimer,86400000);
  assert.deepEqual(invite.snapshot.friendInvite,{name:'Blair',declined:false,expired:false});
  assert.deepEqual(invite.roster.sent.map(x=>[x.to.name,x.declined]),[['Blair',false]]);
  const repeat=await s.dispatch('friendInvite',{requestId:'inv-1',friendId:b.playerId,turnTimer:'24h'},a.token);
  assert.equal(repeat.sent.length,1,'a retried invitation does not open a second table');
  const inbox=await roster(s,b);
  assert.deepEqual(inbox.invites.map(i=>[i.roomId,i.from.name]),[[invite.roomId,'Alex']]);
  assert.deepEqual((await roster(s,c)).invites,[],'nobody else sees it');
  await rejects(s.dispatch('friendJoin',{requestId:rid(),roomId:invite.roomId},c.token),'ROOM_NOT_FOUND');
  const seat=await s.dispatch('friendJoin',{requestId:rid(),roomId:invite.roomId},b.token);
  assert.equal(seat.seat,1);assert.equal(seat.roomId,invite.roomId);
  assert.deepEqual(seat.snapshot.players.map(p=>[p.name,p.accepted]),[['Alex',true],['Blair',true]],'no admission step');
  assert.deepEqual(seat.roster.invites,[]);
  const same=await s.dispatch('friendJoin',{requestId:rid(),roomId:invite.roomId},b.token);
  assert.equal(same.token,seat.token,'joining again returns the same seat credential');
  assert.equal((await s.dispatch('snapshot',{roomId:invite.roomId},invite.token)).players[1].name,'Blair');
  for(const session of [invite,seat]){
   await s.dispatch('deck',{roomId:invite.roomId,requestId:rid(),deck},session.token);
   await s.dispatch('ready',{roomId:invite.roomId,requestId:rid(),ready:true},session.token);
  }
  const started=await s.dispatch('snapshot',{roomId:invite.roomId},invite.token);
  assert.equal(started.status,'playing');assert.ok(started.turnDeadline>clock,'the friend table keeps its turn clock');
  const actor=started.yourMove?invite:seat;
  const snap=await s.dispatch('snapshot',{roomId:invite.roomId},actor.token);
  const action=snap.actions.find(x=>x.kind!=='concede');
  const moved=await s.dispatch('action',{roomId:invite.roomId,requestId:rid(),expectedRevision:snap.gameRevision,actionId:action.id},actor.token);
  assert.equal(moved.gameRevision,snap.gameRevision+1);
  assert.deepEqual((await roster(s,a)).sent,[],'a seated invitation leaves the sent list');
  // Persistence: a restarted service reloads rosters and the friend table.
  const {s:reloaded}=await service({now:()=>clock,storageDir});
  assert.deepEqual((await roster(reloaded,b)).friends.map(f=>f.name),['Alex']);
  assert.equal((await reloaded.dispatch('snapshot',{roomId:invite.roomId},seat.token)).status,'playing');
 }finally{await rm(storageDir,{recursive:true,force:true});}
});

test('declined, replaced, expired and withdrawn invitations disappear from the inbox',async()=>{
 let clock=1_900_000_000_000;
 const {s,storageDir}=await service({now:()=>clock});
 try{
  const a=await register(s,'Alex'),b=await register(s,'Blair');
  await s.dispatch('friendAdd',{requestId:rid(),code:code(a)},b.token);
  const first=await s.dispatch('friendInvite',{requestId:rid(),friendId:b.playerId},a.token);
  const declined=await s.dispatch('friendDecline',{requestId:rid(),roomId:first.roomId},b.token);
  assert.deepEqual(declined.invites,[]);
  assert.deepEqual((await roster(s,a)).sent.map(x=>x.declined),[true],'the inviter learns of the decline');
  assert.equal((await s.dispatch('snapshot',{roomId:first.roomId},first.token)).friendInvite.declined,true);
  await rejects(s.dispatch('friendJoin',{requestId:rid(),roomId:first.roomId},b.token),'INVALID_INVITE');
  const second=await s.dispatch('friendInvite',{requestId:rid(),friendId:b.playerId},a.token);
  const third=await s.dispatch('friendInvite',{requestId:rid(),friendId:b.playerId},a.token);
  assert.deepEqual((await roster(s,b)).invites.map(i=>i.roomId),[third.roomId],'a new invitation replaces the unanswered one');
  assert.equal((await s.dispatch('snapshot',{roomId:second.roomId},second.token)).status,'closed');
  clock+=86400001;
  assert.deepEqual((await roster(s,b)).invites,[],'invitations expire after a day');
  await rejects(s.dispatch('friendJoin',{requestId:rid(),roomId:third.roomId},b.token),'INVALID_INVITE');
  const fourth=await s.dispatch('friendInvite',{requestId:rid(),friendId:b.playerId},a.token);
  await s.dispatch('leave',{roomId:fourth.roomId,requestId:rid()},fourth.token);
  assert.deepEqual((await roster(s,b)).invites,[],'leaving the table withdraws the invitation');
  const fifth=await s.dispatch('friendInvite',{requestId:rid(),friendId:b.playerId},a.token);
  assert.equal((await roster(s,b)).invites.length,1);
  await s.dispatch('friendRemove',{requestId:rid(),friendId:a.playerId},b.token);
  assert.deepEqual((await roster(s,b)).invites,[],'removing the friend withdraws their invitation');
  assert.equal((await s.dispatch('snapshot',{roomId:fifth.roomId},fifth.token)).status,'closed');
 }finally{await rm(storageDir,{recursive:true,force:true});}
});

