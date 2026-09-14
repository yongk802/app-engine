/** HTTP adapter over an initialized RoomService: GET /health and POST /v1/command. Lifted
 * verbatim from Night City Table so every client keeps the same wire contract. No static
 * files, no CORS: browsers reach it through their host's same-origin multiplayer route. */
import http from 'node:http';
import {PROTOCOL_VERSION, RoomError} from './contract.mjs';

const MAX_BODY = 65536;
function send(res, status, value) {
  const body = JSON.stringify(value);
  res.writeHead(status, {'Content-Type': 'application/json; charset=utf-8', 'Content-Length': Buffer.byteLength(body), 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'});
  res.end(body);
}

async function readCommand(req) {
  if (!/^application\/json(?:\s*;|$)/i.test(req.headers['content-type'] || '')) throw new RoomError('INVALID_REQUEST', 'Commands require application/json.', 415);
  if (req.headers['content-encoding'] && req.headers['content-encoding'] !== 'identity') throw new RoomError('INVALID_REQUEST', 'Encoded requests are not supported.', 415);
  if (Number(req.headers['content-length']) > MAX_BODY) throw new RoomError('INVALID_REQUEST', 'Request is too large.', 413);
  let size = 0;
  const chunks = [];
  for await (const chunk of req) {
    size += chunk.length;
    if (size > MAX_BODY) throw new RoomError('INVALID_REQUEST', 'Request is too large.', 413);
    chunks.push(chunk);
  }
  let body;
  try { body = JSON.parse(Buffer.concat(chunks).toString('utf8')); }
  catch { throw new RoomError('INVALID_REQUEST', 'Malformed JSON.'); }
  if (!body || typeof body !== 'object' || Array.isArray(body) || typeof body.op !== 'string') throw new RoomError('INVALID_REQUEST', 'A command operation is required.');
  const {op, ...payload} = body;
  return {op, payload};
}

/** An HTTP adapter over an initialized RoomService; no static files or CORS. */
export function createRoomServer(service) {
  const server = http.createServer({maxHeaderSize: 8192}, async (req, res) => {
    try {
      if (req.url === '/health' && req.method === 'GET') {
        send(res, 200, {service: 'night-city-rooms', protocol: PROTOCOL_VERSION, rulesVersion: service.rulesVersion, catalogDigest: service.catalogDigest});
        return;
      }
      if (req.url !== '/v1/command' || req.method !== 'POST') throw new RoomError('INVALID_REQUEST', 'Endpoint not found.', 404);
      // Browser clients use their same-origin application proxy. Direct requests
      // carrying Origin are never accepted by the private room service.
      if (req.headers.origin || req.headers['sec-fetch-site'] === 'cross-site') throw new RoomError('FORBIDDEN', 'Use the application room connection.', 403);
      const auth = req.headers.authorization;
      let token = '';
      if (auth !== undefined) {
        const match = /^Bearer ([A-Za-z0-9_-]{43})$/.exec(auth);
        if (!match) throw new RoomError('AUTH_REQUIRED', 'Invalid authorization.', 401);
        token = match[1];
      }
      const {op, payload} = await readCommand(req);
      // A host with player accounts names the signed-in player; nothing else sets this.
      const player = typeof req.headers['x-app-player'] === 'string' && /^[A-Za-z0-9_-]{1,64}$/.test(req.headers['x-app-player']) ? req.headers['x-app-player'] : '';
      const result = await service.dispatch(op, payload, token, player);
      send(res, 200, {ok: true, result});
    } catch (error) {
      if (res.destroyed || res.headersSent) return;
      const safe = error instanceof RoomError ? error : new RoomError('INTERNAL_ERROR', 'The room service could not process this request.', 500);
      send(res, safe.status, {ok: false, error: {code: safe.code, message: safe.message}});
    }
  });
  server.requestTimeout = 15000;
  server.headersTimeout = 10000;
  server.keepAliveTimeout = 5000;
  server.maxRequestsPerSocket = 100;
  return server;
}

