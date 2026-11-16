/** Authenticated KYNTRION Whisper bridge. Bot tokens are never stored here. */
const TOKEN_HEADER = 'x-kyntrion-bot-token';
const MAX_AUDIO_BYTES = 20 * 1024 * 1024;
const MODEL = '@cf/openai/whisper-large-v3-turbo';
const HEADERS = { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' };

function reply(status, body) {
  return new Response(JSON.stringify(body), { status, headers: HEADERS });
}

async function authorized(token, env) {
  if (!/^\d+:[A-Za-z0-9_-]{8,200}$/.test(token || '')) return false;
  const allowed = (env.ALLOWED_BOT_SHA256 || '').split(',').map(value => value.trim().toLowerCase());
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(token));
  const hex = Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, '0')).join('');
  return allowed.includes(hex);
}

async function readBounded(body) {
  if (!body) return new Uint8Array();
  const reader = body.getReader();
  const chunks = [];
  let size = 0;
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > MAX_AUDIO_BYTES) return null;
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const data = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    data.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return data;
}

function base64(bytes) {
  const chunks = [];
  for (let index = 0; index < bytes.length; index += 8192) {
    chunks.push(String.fromCharCode(...bytes.subarray(index, index + 8192)));
  }
  return btoa(chunks.join(''));
}

export async function handleVoiceRequest(request, env) {
  const path = new URL(request.url).pathname;
  if (path !== '/transcribe') return reply(404, { ok: false, error: 'Not found' });
  if (request.method !== 'POST') return reply(405, { ok: false, error: 'Method not allowed' });

  const token = request.headers.get(TOKEN_HEADER);
  if (!token) return reply(401, { ok: false, error: 'Token required' });
  if (!(await authorized(token, env))) return reply(403, { ok: false, error: 'Bot not authorized' });

  const declaredSize = Number(request.headers.get('content-length') || 0);
  if (declaredSize > MAX_AUDIO_BYTES) return reply(413, { ok: false, error: 'Audio too large' });
  let audio;
  try {
    audio = await readBounded(request.body);
  } catch {
    return reply(400, { ok: false, error: 'Invalid audio body' });
  }
  if (audio === null) return reply(413, { ok: false, error: 'Audio too large' });
  if (!audio.length) return reply(400, { ok: false, error: 'Empty audio' });
  if (!env.AI || typeof env.AI.run !== 'function') {
    return reply(503, { ok: false, error: 'Speech recognition unavailable' });
  }
  try {
    const result = await env.AI.run(MODEL, { audio: base64(audio), task: 'transcribe' });
    if (typeof result?.text !== 'string' || !result.text.trim()) {
      return reply(422, { ok: false, error: 'Speech not recognized' });
    }
    return reply(200, { ok: true, result: { text: result.text.trim() } });
  } catch {
    return reply(503, { ok: false, error: 'Speech recognition unavailable' });
  }
}

export default {
  fetch(request, env) { return handleVoiceRequest(request, env); },
};
