import { promises as fs } from 'node:fs';
import { createHash } from 'node:crypto';
import { join } from 'node:path';

export async function verifyBuild(root) {
  const manifest = JSON.parse(await fs.readFile(join(root, 'dist/build-manifest.json'), 'utf8'));
  const files = { 'src/index.mjs': 'src/index.mjs', 'package-lock.json': 'package-lock.json',
    '_worker.js': 'dist/pages/_worker.js' };
  for (const [name, path] of Object.entries(files)) {
    const hash = createHash('sha256').update(await fs.readFile(join(root, path))).digest('hex');
    if (manifest.hashes?.[name] !== hash) throw new Error('Stale receiver build; rebuild before deployment');
  }
  return manifest;
}
