import { spawnSync } from 'node:child_process';

export function readCloudflareToken(executable, run = spawnSync) {
  const result = run(executable, ['auth', 'token', '--json'], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
  if (result.error || result.status !== 0) throw new Error('Cloudflare authorization unavailable; no credential output logged');
  let value;
  try { value = JSON.parse(result.stdout); } catch { throw new Error('Cloudflare credential response is invalid'); }
  if (typeof value.token !== 'string' || !value.token.trim()) throw new Error('Cloudflare authorization unavailable');
  return value.token;
}
