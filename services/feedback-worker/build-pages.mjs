import { promises as fs } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { resolve, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('.', import.meta.url));
const bundle = resolve(root, 'dist/bundle');
const output = resolve(root, 'dist/pages');
await fs.mkdir(output, { recursive: true });
execFileSync(join(root, 'node_modules/.bin/wrangler'), ['deploy', '--dry-run', '--config', join(root, 'wrangler.jsonc'), '--outdir', bundle], { cwd: root, stdio: 'inherit' });
const code = await fs.readFile(join(bundle, 'index.js'));
await fs.writeFile(join(output, '_worker.js'), code);
await fs.writeFile(join(output, '_routes.json'), JSON.stringify({ version: 1, include: ['/*'], exclude: [] }) + '\n');
const hashes = {};
for (const name of ['src/index.mjs', 'package-lock.json']) hashes[name] = createHash('sha256').update(await fs.readFile(join(root, name))).digest('hex');
hashes['_worker.js'] = createHash('sha256').update(code).digest('hex');
await fs.writeFile(resolve(root, 'dist/build-manifest.json'), JSON.stringify({ hashes, builtAt: new Date().toISOString(), containsSecrets: false }, null, 2) + '\n');
console.log(JSON.stringify({ pagesOutput: output, hashes, containsSecrets: false }));
