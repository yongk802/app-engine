/** One room service per data directory.
 * Two services on the same rooms.json (an orphan left behind by an engine crash and the
 * replacement the next engine starts, or a hand-started copy) each keep their own copy in
 * memory and overwrite each other's moves. The directory is claimed with `service.lock`,
 * which names the holder's pid and a random token and is refreshed on a heartbeat. A lock
 * whose pid is gone, or that nobody has refreshed within `staleMs`, may be taken over. */
import {randomBytes} from 'node:crypto';
import {readFileSync, unlinkSync} from 'node:fs';
import {link, mkdir, readFile, stat, unlink, utimes, writeFile} from 'node:fs/promises';
import path from 'node:path';
import {clearInterval, setInterval} from 'node:timers';

export const LOCK_FILE = 'service.lock';
const HEARTBEAT_MS = 10_000;
const STALE_MS = 30_000;

export class DataDirBusy extends Error {}

function alive(pid) {
  // Our own pid in an old lock is an earlier run in another pid namespace, not a live peer.
  if (!Number.isInteger(pid) || pid <= 0 || pid === process.pid) return false;
  try { process.kill(pid, 0); return true; } catch (error) { return error.code === 'EPERM'; }
}

async function holder(file) {
  try {
    const [text, info] = await Promise.all([readFile(file, 'utf8'), stat(file)]);
    const {pid, token} = JSON.parse(text);
    return {pid, token, age: Date.now() - info.mtimeMs};
  } catch (error) {
    if (error.code === 'ENOENT') return null;
    return {pid: null, token: null, age: Infinity};  // unreadable: complete locks are linked into place whole
  }
}

/** Claims *dataDir* or throws DataDirBusy. `onLost` runs if another process takes the lock
 * over (this one was frozen past `staleMs`); the caller must stop writing at once. */
export async function claimDataDir(dataDir, {heartbeatMs = HEARTBEAT_MS, staleMs = STALE_MS, onLost = () => {}} = {}) {
  await mkdir(dataDir, {recursive: true});
  const file = path.join(dataDir, LOCK_FILE);
  const token = randomBytes(16).toString('hex');
  const draft = `${file}.${token}`;
  await writeFile(draft, JSON.stringify({pid: process.pid, token}));
  try {
    for (let attempt = 0; ; attempt++) {
      try {
        await link(draft, file);  // atomic: fails if a lock exists, never exposes a half-written one
        break;
      } catch (error) {
        if (error.code !== 'EEXIST' || attempt > 0) {
          if (error.code === 'EEXIST') throw new DataDirBusy(`Another room service took ${dataDir} while this one was starting.`);
          throw error;
        }
        const current = await holder(file);
        if (current && current.age < staleMs && (current.pid === null || alive(current.pid))) {
          throw new DataDirBusy(`Another room service (pid ${current.pid ?? 'unknown'}) is using ${dataDir}. Stop it before starting this one.`);
        }
        await unlink(file).catch(e => { if (e.code !== 'ENOENT') throw e; });
      }
    }
  } finally {
    await unlink(draft).catch(() => {});
  }
  const ours = () => {
    try { return JSON.parse(readFileSync(file, 'utf8')).token === token; } catch { return false; }
  };
  let lost = false;
  const beat = setInterval(async () => {
    if (lost) return;
    if (!ours()) { lost = true; clearInterval(beat); onLost(); return; }
    const now = new Date();
    await utimes(file, now, now).catch(() => {});
  }, heartbeatMs);
  beat.unref();
  const release = () => {
    clearInterval(beat);
    if (!lost && ours()) { try { unlinkSync(file); } catch {} }
  };
  return {file, token, release};
}
