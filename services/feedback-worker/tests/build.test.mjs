import test from 'node:test';
import assert from 'node:assert/strict';
import { promises as fs } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createHash } from 'node:crypto';
import { verifyBuild } from '../verify-build.mjs';

test('receiver deployment rejects stale sources and outputs locally', async t => {
  const root = await fs.mkdtemp(join(tmpdir(), 'feedback-build-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  await fs.mkdir(join(root, 'src'), { recursive: true });
  await fs.mkdir(join(root, 'dist/pages'), { recursive: true });
  const paths = { 'src/index.mjs': 'src/index.mjs', 'package-lock.json': 'package-lock.json',
    '_worker.js': 'dist/pages/_worker.js' };
  const hashes = {};
  for (const [name, path] of Object.entries(paths)) {
    await fs.writeFile(join(root, path), 'synthetic');
    hashes[name] = createHash('sha256').update('synthetic').digest('hex');
  }
  await fs.writeFile(join(root, 'dist/build-manifest.json'), JSON.stringify({ hashes }));
  assert.deepEqual((await verifyBuild(root)).hashes, hashes);
  await fs.writeFile(join(root, 'src/index.mjs'), 'changed');
  await assert.rejects(verifyBuild(root), /Stale receiver build/);
  await fs.writeFile(join(root, 'src/index.mjs'), 'synthetic');
  await fs.writeFile(join(root, 'dist/pages/_worker.js'), 'changed');
  await assert.rejects(verifyBuild(root), /Stale receiver build/);
});
