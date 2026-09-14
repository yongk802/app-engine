import assert from 'node:assert/strict';
import test from 'node:test';
import {mkdir, mkdtemp, rename, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {engine} from './fake-rules.mjs';

const compatibility = {
  protocol: 1,
  rulesVersion: engine.rulesVersion,
  catalogDigest: engine.catalogDigest,
};
const deck = engine.generateDeck({}, 1701);
let requestSequence = 0;
const requestId = (prefix = 'request') => `${prefix}-${++requestSequence}`;

async function loadRoomService() {
  const implementation = await import('../rooms.mjs').catch(() => ({}));
  assert.equal(typeof implementation.RoomService, 'function');
  return implementation.RoomService;
}

async function withService(run, {now = () => 1_900_000_000_000} = {}) {
  const storageDir = await mkdtemp(join(tmpdir(), 'night-city-rooms-'));
  try {
    const RoomService = await loadRoomService();
    const service = new RoomService({engine, ...compatibility, storageDir, now});
    await service.init();
    return await run(service, storageDir, RoomService);
  } finally {
    await rm(storageDir, {recursive: true, force: true});
  }
}

async function create(service, name = 'V') {
  return service.dispatch('create', {name, ...compatibility});
}

async function joinRoom(service, owner, name = 'Lucy', extra = {}) {
  return service.dispatch('join', {
    roomId: owner.roomId,
    invite: owner.invite,
    name,
    requestId: requestId('join'),
    ...compatibility,
    ...extra,
  });
}

async function acceptedRoom(service) {
  const owner = await create(service);
  const guest = await joinRoom(service, owner);
  const pending = await service.dispatch('snapshot', {roomId: owner.roomId}, owner.token);
  await service.dispatch('accept', {
    roomId: owner.roomId,
    joinId: pending.pendingJoin.id,
    requestId: requestId('accept'),
  }, owner.token);
  return {owner, guest};
}

async function startedRoom(service) {
  const sessions = await acceptedRoom(service);
  for (const session of [sessions.owner, sessions.guest]) {
    await service.dispatch('deck', {
      roomId: sessions.owner.roomId,
      requestId: requestId('deck'),
      deck,
    }, session.token);
  }
  await service.dispatch('ready', {
    roomId: sessions.owner.roomId,
    requestId: requestId('ready'),
    ready: true,
  }, sessions.owner.token);
  await service.dispatch('ready', {
    roomId: sessions.owner.roomId,
    requestId: requestId('ready'),
    ready: true,
  }, sessions.guest.token);
  return sessions;
}

async function rejectsCode(promise, code) {
  await assert.rejects(promise, error => error?.code === code,
    `expected RoomError code ${code}`);
}

test('RoomService exposes the portable async lifecycle', async () => {
  const RoomService = await loadRoomService();
  assert.equal(typeof RoomService.prototype.init, 'function');
  assert.equal(typeof RoomService.prototype.dispatch, 'function');
});

test('admission keeps pending players restricted and match views private', async () => {
  await withService(async service => {
    const owner = await create(service, 'V');
    const guest = await joinRoom(service, owner, 'Lucy');
    assert.equal(guest.snapshot.self, 1);
    assert.equal(guest.snapshot.players[1].accepted, false);
    assert.equal(guest.snapshot.view, null);
    assert.deepEqual(guest.snapshot.messages, []);
    await rejectsCode(service.dispatch('deck', {
      roomId: owner.roomId, requestId: requestId(), deck,
    }, guest.token), 'FORBIDDEN');

    const ownerPending = await service.dispatch('snapshot', {roomId: owner.roomId}, owner.token);
    assert.deepEqual(ownerPending.pendingJoin, {id: ownerPending.pendingJoin.id, name: 'Lucy'});
    await service.dispatch('accept', {
      roomId: owner.roomId,
      joinId: ownerPending.pendingJoin.id,
      requestId: requestId('accept'),
    }, owner.token);
    for (const session of [owner, guest]) {
      await service.dispatch('deck', {
        roomId: owner.roomId, requestId: requestId('deck'), deck,
      }, session.token);
    }
    const before = await service.dispatch('snapshot', {roomId: owner.roomId}, owner.token);
    assert.equal(JSON.stringify(before).includes(JSON.stringify(deck.cards)), false);
    await service.dispatch('ready', {roomId: owner.roomId, requestId: requestId(), ready: true}, owner.token);
    await service.dispatch('ready', {roomId: owner.roomId, requestId: requestId(), ready: true}, guest.token);
    const [ownerView, guestView] = await Promise.all([
      service.dispatch('snapshot', {roomId: owner.roomId}, owner.token),
      service.dispatch('snapshot', {roomId: owner.roomId}, guest.token),
    ]);
    assert.equal(ownerView.status, 'playing');
    assert.equal(ownerView.view.player, 0);
    assert.equal(guestView.view.player, 1);
    assert.ok(ownerView.view.players[0].hand.length > 0 || ownerView.view.players[0].handCount === 0);
    assert.deepEqual(ownerView.view.players[1].hand, []);
    assert.deepEqual(guestView.view.players[0].hand, []);
    assert.deepEqual(ownerView.actions, engine.legalActions(ownerView.view, 0));
    assert.deepEqual(guestView.actions, engine.legalActions(guestView.view, 1));
  });
});

test('compatibility, deck validity, authentication, and invite rules are enforced', async () => {
  let clock = 2_000_000;
  await withService(async service => {
    const owner = await create(service);
    await rejectsCode(service.dispatch('snapshot', {roomId: owner.roomId}, 'wrong-token'), 'AUTH_REQUIRED');
    await rejectsCode(joinRoom(service, owner, 'Old client', {protocol: 0}), 'INCOMPATIBLE');
    await rejectsCode(joinRoom(service, owner, 'Wrong rules', {rulesVersion: 'other'}), 'INCOMPATIBLE');
    await rejectsCode(joinRoom(service, owner, 'Wrong cards', {catalogDigest: 'other'}), 'INCOMPATIBLE');

    const guest = await joinRoom(service, owner);
    await rejectsCode(joinRoom(service, owner, 'Third'), 'ROOM_FULL');
    const pending = await service.dispatch('snapshot', {roomId: owner.roomId}, owner.token);
    await rejectsCode(service.dispatch('accept', {
      roomId: owner.roomId, joinId: pending.pendingJoin.id, requestId: requestId(),
    }, guest.token), 'FORBIDDEN');
    await service.dispatch('accept', {roomId: owner.roomId, joinId: pending.pendingJoin.id, requestId: requestId()}, owner.token);
    await rejectsCode(service.dispatch('deck', {
      roomId: owner.roomId, requestId: requestId(), deck: {name: 'bad', legends: [], cards: []},
    }, guest.token), 'INVALID_DECK');
    await rejectsCode(joinRoom(service, owner, 'Third'), 'INVALID_INVITE');

    const expiring = await create(service, 'Rogue');
    clock += 24 * 60 * 60 * 1000 + 1;
    await rejectsCode(joinRoom(service, expiring, 'Late'), 'INVALID_INVITE');
  }, {now: () => clock});
});

test('mutations are idempotent, conflicting reuse is rejected, and stale actions do nothing', async () => {
  await withService(async service => {
    const {owner, guest} = await startedRoom(service);
    const messageRequest = requestId('same-message');
    await service.dispatch('message', {roomId: owner.roomId, requestId: messageRequest, text: 'Once'}, owner.token);
    await service.dispatch('message', {roomId: owner.roomId, requestId: messageRequest, text: 'Once'}, owner.token);
    let snapshot = await service.dispatch('snapshot', {roomId: owner.roomId}, owner.token);
    assert.equal(snapshot.messages.filter(message => message.text === 'Once').length, 1);
    await rejectsCode(service.dispatch('message', {
      roomId: owner.roomId, requestId: messageRequest, text: 'Changed',
    }, owner.token), 'REQUEST_CONFLICT');

    const before = structuredClone(snapshot);
    const offered = snapshot.actions[0]?.id;
    assert.ok(offered);
    await rejectsCode(service.dispatch('action', {
      roomId: owner.roomId,
      requestId: requestId('stale'),
      expectedRevision: snapshot.gameRevision - 1,
      actionId: offered,
    }, owner.token), 'STALE_REVISION');
    snapshot = await service.dispatch('snapshot', {roomId: owner.roomId}, owner.token);
    assert.equal(snapshot.gameRevision, before.gameRevision);
    assert.deepEqual(snapshot.view, before.view);
    assert.deepEqual(snapshot.actions, before.actions);
    assert.equal(snapshot.messages.length, before.messages.length);
    assert.equal(snapshot.players[1].name, 'Lucy');
  });
});

test('chat is plain, bounded, attributed to the authenticated seat, and keeps the last 100', async () => {
  await withService(async service => {
    const {owner, guest} = await acceptedRoom(service);
    await rejectsCode(service.dispatch('message', {roomId: owner.roomId, requestId: requestId(), text: ''}, owner.token), 'INVALID_REQUEST');
    await rejectsCode(service.dispatch('message', {roomId: owner.roomId, requestId: requestId(), text: 'x'.repeat(2001)}, owner.token), 'INVALID_REQUEST');
    for (let index = 0; index < 101; index++) {
      await service.dispatch('message', {roomId: owner.roomId, requestId: requestId('chat'), text: `<b>${index}</b>`}, index % 2 ? guest.token : owner.token);
    }
    const snapshot = await service.dispatch('snapshot', {roomId: owner.roomId}, owner.token);
    assert.equal(snapshot.messages.length, 100);
    assert.equal(snapshot.messages[0].text, '<b>1</b>');
    assert.equal(snapshot.messages.at(-1).text, '<b>100</b>');
    assert.equal(snapshot.messages[0].sender, 1);
    assert.equal(snapshot.messages.at(-1).sender, 0);
  });
});

test('voice signals are bounded, ephemeral, and delivered only to the peer', async () => {
  await withService(async service => {
    const {owner, guest} = await acceptedRoom(service);
    const sent = await service.dispatch('signal', {
      roomId: owner.roomId, requestId: requestId(), callId: 'call-a', kind: 'offer', data: {sdp: 'hello'},
    }, owner.token);
    assert.deepEqual(sent.signals, []);
    let peer = await service.dispatch('snapshot', {roomId: owner.roomId}, guest.token);
    assert.equal(peer.signals.length, 1);
    assert.equal(peer.signals[0].sender, 0);
    assert.equal(peer.signals[0].callId, 'call-a');
    assert.deepEqual(peer.signals[0].data, {sdp: 'hello'});
    await rejectsCode(service.dispatch('signal', {
      roomId: owner.roomId, requestId: requestId(), callId: 'call-a', kind: 'recording', data: {},
    }, owner.token), 'INVALID_REQUEST');
    await rejectsCode(service.dispatch('signal', {
      roomId: owner.roomId, requestId: requestId(), callId: 'call-a', kind: 'ice', data: {candidate: 'x'.repeat(17 * 1024)},
    }, owner.token), 'INVALID_REQUEST');
    for (let index = 0; index < 65; index++) {
      await service.dispatch('signal', {
        roomId: owner.roomId, requestId: requestId('signal'), callId: 'call-b', kind: 'ice', data: {index},
      }, owner.token);
    }
    peer = await service.dispatch('snapshot', {roomId: owner.roomId}, guest.token);
    assert.equal(peer.signals.length, 64);
    assert.equal(peer.signals[0].data.index, 1);
    assert.equal(peer.signals.at(-1).data.index, 64);
  });
});

test('restart restores match, seats, chat, and dedup while dropping voice signals', async () => {
  await withService(async (service, storageDir, RoomService) => {
    const {owner, guest} = await startedRoom(service);
    const persistedRequest = requestId('persisted-message');
    await service.dispatch('message', {roomId: owner.roomId, requestId: persistedRequest, text: 'Remember me'}, guest.token);
    await service.dispatch('signal', {
      roomId: owner.roomId, requestId: requestId(), callId: 'ephemeral', kind: 'offer', data: {sdp: 'secret'},
    }, owner.token);

    const restarted = new RoomService({engine, ...compatibility, storageDir, now: () => 1_900_000_000_000});
    await restarted.init();
    let snapshot = await restarted.dispatch('snapshot', {roomId: owner.roomId}, guest.token);
    assert.equal(snapshot.status, 'playing');
    assert.equal(snapshot.players[0].accepted, true);
    assert.equal(snapshot.players[1].accepted, true);
    assert.ok(snapshot.view);
    assert.equal(snapshot.messages.at(-1).text, 'Remember me');
    assert.deepEqual(snapshot.signals, []);
    await restarted.dispatch('message', {roomId: owner.roomId, requestId: persistedRequest, text: 'Remember me'}, guest.token);
    snapshot = await restarted.dispatch('snapshot', {roomId: owner.roomId}, guest.token);
    assert.equal(snapshot.messages.filter(message => message.text === 'Remember me').length, 1);
  });
});

test('a persistence failure does not publish the candidate mutation in memory', async () => {
  await withService(async (service, storageDir) => {
    const {owner} = await acceptedRoom(service);
    const storageFile = join(storageDir, 'rooms.json');
    const backupFile = join(storageDir, 'rooms.backup.json');
    await rename(storageFile, backupFile);
    await mkdir(storageFile);
    await rejectsCode(service.dispatch('message', {
      roomId: owner.roomId, requestId: requestId('failed-write'), text: 'must roll back',
    }, owner.token), 'STORAGE_FAILURE');
    await rm(storageFile, {recursive: true});
    await rename(backupFile, storageFile);
    const snapshot = await service.dispatch('snapshot', {roomId: owner.roomId}, owner.token);
    assert.equal(snapshot.messages.some(message => message.text === 'must roll back'), false);
  });
});

test('rematch retains decks but requires both players to ready again', async () => {
  await withService(async service => {
    const {owner, guest} = await startedRoom(service);
    let snapshot = await service.dispatch('snapshot', {roomId: owner.roomId}, owner.token);
    const concede = snapshot.actions.find(action => action.kind === 'concede');
    assert.ok(concede);
    snapshot = await service.dispatch('action', {
      roomId: owner.roomId, requestId: requestId(), expectedRevision: snapshot.gameRevision, actionId: concede.id,
    }, owner.token);
    assert.equal(snapshot.status, 'over');
    const endedRevision = snapshot.gameRevision;
    snapshot = await service.dispatch('rematch', {roomId: owner.roomId, requestId: requestId()}, guest.token);
    assert.equal(snapshot.status, 'lobby');
    assert.equal(snapshot.players[0].ready, false);
    assert.equal(snapshot.players[1].ready, false);
    assert.equal(snapshot.players[0].deckReady, true);
    assert.equal(snapshot.players[1].deckReady, true);
    snapshot = await service.dispatch('ready', {roomId: owner.roomId, requestId: requestId(), ready: true}, owner.token);
    assert.equal(snapshot.status, 'lobby');
    snapshot = await service.dispatch('ready', {roomId: owner.roomId, requestId: requestId(), ready: true}, guest.token);
    assert.equal(snapshot.status, 'playing');
    assert.ok(snapshot.gameRevision > endedRevision);
  });
});

test('leave closes a lobby and concedes an active match', async () => {
  await withService(async service => {
    const lobby = await acceptedRoom(service);
    const closed = await service.dispatch('leave', {roomId: lobby.owner.roomId, requestId: requestId()}, lobby.guest.token);
    assert.equal(closed.status, 'closed');
    const repeated = await service.dispatch('leave', {roomId: lobby.owner.roomId, requestId: requestId()}, lobby.guest.token);
    assert.equal(repeated.status, 'closed');
    assert.equal(repeated.revision, closed.revision);

    const playing = await startedRoom(service);
    const conceded = await service.dispatch('leave', {roomId: playing.owner.roomId, requestId: requestId()}, playing.owner.token);
    assert.equal(conceded.status, 'over');
    assert.equal(conceded.view.winner, 1);
  });
});

test('a pending guest can withdraw and retry leaving without blocking a new admission', async()=>withService(async service=>{
 const owner=await create(service),guest=await joinRoom(service,owner);
 const payload={roomId:owner.roomId,requestId:requestId('withdraw')};
 await service.dispatch('leave',payload,guest.token);
 await service.dispatch('leave',payload,guest.token);
 await rejectsCode(service.dispatch('snapshot',{roomId:owner.roomId},guest.token),'AUTH_REQUIRED');
 const other=await joinRoom(service,owner,'Replacement');
 assert.equal(other.snapshot.players[1].accepted,false);
}));
