import { isIP } from 'node:net';

const normalizeAddress = (value) => {
  const raw = String(value || '').trim();
  const unwrapped = raw.startsWith('[') && raw.endsWith(']') ? raw.slice(1, -1) : raw;
  const address = unwrapped.startsWith('::ffff:') ? unwrapped.slice(7) : unwrapped;
  return isIP(address) ? address.toLowerCase() : null;
};

const isTrustedProxy = (address) => {
  const ip = normalizeAddress(address);
  if (!ip) return false;
  if (ip === '127.0.0.1' || ip === '::1') return true;
  if (ip.startsWith('10.') || ip.startsWith('192.168.')) return true;
  const m = /^172\.(\d+)\./.exec(ip);
  return Boolean(m && Number(m[1]) >= 16 && Number(m[1]) <= 31);
};

/** Trust forwarded identity only from the private, non-published Caddy hop. */
export function canonicalClientIdentity(req) {
  const peer = normalizeAddress(req?.socket?.remoteAddress) || 'unknown';
  if (!isTrustedProxy(peer)) return peer;
  const forwarded = String(req?.headers?.['x-forwarded-for'] || '').split(',')[0].trim();
  return normalizeAddress(forwarded) || peer;
}

export class TtlLruCache {
  constructor({ max = 128, ttlMs = 300_000, now = Date.now } = {}) {
    this.max = max;
    this.ttlMs = ttlMs;
    this.now = now;
    this.entries = new Map();
  }
  get(key) {
    const hit = this.entries.get(key);
    if (!hit) return undefined;
    if (this.now() - hit.at >= this.ttlMs) {
      this.entries.delete(key);
      return undefined;
    }
    this.entries.delete(key);
    this.entries.set(key, hit);
    return hit.value;
  }
  set(key, value) {
    this.entries.delete(key);
    this.entries.set(key, { at: this.now(), value });
    while (this.entries.size > this.max) this.entries.delete(this.entries.keys().next().value);
  }
  get size() { return this.entries.size; }
}

export function createCoalescer() {
  const inflight = new Map();
  return {
    run(key, work) {
      if (inflight.has(key)) return inflight.get(key);
      const promise = Promise.resolve().then(work).finally(() => inflight.delete(key));
      inflight.set(key, promise);
      return promise;
    },
    get size() { return inflight.size; },
  };
}

export class ConcurrencyGate {
  constructor(max = 4) { this.max = max; this.active = 0; }
  async run(work) {
    if (this.active >= this.max) {
      const error = new Error('live profile upstream capacity is busy; retry shortly');
      error.status = 429;
      error.retryAfter = 2;
      throw error;
    }
    this.active += 1;
    try { return await work(); } finally { this.active -= 1; }
  }
}

export function createRateLimiter({ limit, windowMs = 60_000, maxClients = 4096, now = Date.now }) {
  const buckets = new Map();
  return {
    enforce(client) {
      const time = now();
      let bucket = buckets.get(client);
      if (!bucket) {
        if (buckets.size >= maxClients) buckets.delete(buckets.keys().next().value);
        bucket = [];
        buckets.set(client, bucket);
      } else {
        buckets.delete(client);
        buckets.set(client, bucket);
      }
      while (bucket.length && time - bucket[0] >= windowMs) bucket.shift();
      if (bucket.length >= limit) {
        const error = new Error('too many requests; retry later');
        error.status = 429;
        error.retryAfter = Math.max(1, Math.ceil((windowMs - (time - bucket[0])) / 1000));
        throw error;
      }
      bucket.push(time);
    },
    reset() { buckets.clear(); },
    get size() { return buckets.size; },
  };
}
