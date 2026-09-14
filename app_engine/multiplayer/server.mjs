/** Entry point the host launches for one app: loads the app's rules module (and optional AI
 * policy), then serves rooms, tournaments, friends and the optional daily board for it.
 *   node server.mjs --rules /abs/app/multiplayer/rules.mjs [--ai /abs/app/ai.mjs] --data-dir DIR [--host H] [--port P]
 * The rules module's default export is a constructed RulesModule (see rooms.mjs); it may also
 * carry `chooseAction` and `daily`. Port 0 picks a free port; the chosen port is printed as
 * `multiplayer rooms listening on HOST:PORT` so a host process can read it. */
import os from 'node:os';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {RoomService} from './rooms.mjs';
import {RoomError} from './contract.mjs';
import {createRoomServer} from './transport.mjs';

export function defaultStorageDir() {
  const base = process.platform === 'darwin' ? path.join(os.homedir(), 'Library', 'Application Support')
    : process.platform === 'win32' ? process.env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local')
    : process.env.XDG_STATE_HOME || path.join(os.homedir(), '.local', 'state');
  return path.join(base, 'app-engine', 'multiplayer');
}

const REQUIRED = ['createMatch', 'legalActions', 'applyAction', 'viewFor', 'validateDeck', 'generateDeck'];

async function loadModule(file) {
  const module = await import(pathToFileURL(path.resolve(file)).href);
  return module.default ?? module;
}

/** Validates the rules module surface the service relies on; a bad module never starts a service. */
export function checkRules(engine) {
  if (!engine || typeof engine !== 'object' && typeof engine !== 'function') throw new Error('The rules module must default-export a RulesModule object.');
  for (const name of REQUIRED) if (typeof engine[name] !== 'function') throw new Error(`The rules module is missing ${name}().`);
  for (const name of ['rulesVersion', 'catalogDigest']) if (typeof engine[name] !== 'string' || !engine[name]) throw new Error(`The rules module must export a ${name} string.`);
  if (engine.chooseAction != null && typeof engine.chooseAction !== 'function') throw new Error('chooseAction must be a function.');
  if (engine.daily != null && ['validDay', 'replayDaily', 'gigResult', 'rankBoard'].some(n => typeof engine.daily[n] !== 'function')) throw new Error('The daily plugin needs validDay, replayDaily, gigResult and rankBoard.');
  return engine;
}

export async function createService({rules, ai = null, dataDir, now}) {
  const engine = checkRules(await loadModule(rules));
  let chooseAction = engine.chooseAction ?? null;
  if (ai) {
    const policy = await loadModule(ai);
    if (typeof policy.chooseAction !== 'function') throw new Error('The ai module must export chooseAction().');
    chooseAction = policy.chooseAction;
  }
  const service = new RoomService({engine, rulesVersion: engine.rulesVersion, catalogDigest: engine.catalogDigest, storageDir: dataDir, chooseAction, daily: engine.daily ?? null, ...(now ? {now} : {})});
  await service.init();
  return service;
}

export async function main(args = process.argv.slice(2)) {
  const options = {host: '127.0.0.1', port: 18791, dataDir: defaultStorageDir(), rules: null, ai: null};
  const names = {'--host': 'host', '--port': 'port', '--data-dir': 'dataDir', '--rules': 'rules', '--ai': 'ai'};
  for (let i = 0; i < args.length; i += 2) {
    if (!Object.hasOwn(names, args[i]) || !args[i + 1] || args[i + 1].startsWith('--')) throw new Error('Usage: node server.mjs --rules PATH [--ai PATH] [--host HOST] [--port PORT] [--data-dir PATH]');
    options[names[args[i]]] = args[i + 1];
  }
  options.port = Number(options.port);
  if (!Number.isInteger(options.port) || options.port < 0 || options.port > 65535) throw new Error('Port must be an integer from 0 to 65535.');
  if (!options.rules) throw new Error('A --rules module is required.');
  const service = await createService(options);
  const server = createRoomServer(service);
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(options.port, options.host, resolve);
  });
  const address = server.address();
  process.stdout.write(`multiplayer rooms listening on ${options.host}:${address.port}\n`);
  const shutdown = () => { server.close(); server.closeIdleConnections(); };
  process.once('SIGINT', shutdown);
  process.once('SIGTERM', shutdown);
  return {server, service};
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  main().catch(error => {
    process.stderr.write(`${error instanceof RoomError || error instanceof Error ? error.message : 'Room service could not start.'}\n`);
    process.exitCode = 1;
  });
}
