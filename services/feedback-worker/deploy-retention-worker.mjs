import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { parseArgs } from 'node:util';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const { values } = parseArgs({ options: {
  configuration: { type: 'string' },
  'app-id': { type: 'string' },
  'installation-id': { type: 'string' },
  'private-key-file': { type: 'string' },
  apply: { type: 'boolean', default: false },
} });

for (const name of ['configuration', 'app-id', 'installation-id', 'private-key-file']) {
  if (!values[name]) throw new Error(`Missing --${name}`);
}
const configPath = resolve(values.configuration);
const config = JSON.parse(readFileSync(configPath, 'utf8'));
if (!config.account_id || !config.d1_databases?.[0]?.database_id || config.d1_databases[0].database_id.startsWith('00000000-')
    || !/^\d+$/.test(values['app-id']) || !/^\d+$/.test(values['installation-id']) || !/^\d+$/.test(String(config.vars?.GITHUB_RELEASE_ID))) {
  throw new Error('Retention deployment is not bound to approved Cloudflare, D1, GitHub App, and Release identities');
}
const privateKey = readFileSync(resolve(values['private-key-file']), 'utf8');
const pemHeader = ['-----BEGIN', 'PRIVATE KEY-----'].join(' ');
if (!privateKey.startsWith(pemHeader)) {
  throw new Error('Retention deployment secrets are invalid');
}
const resolvedConfig = { ...config, vars: { ...config.vars,
  GITHUB_APP_ID: values['app-id'], GITHUB_INSTALLATION_ID: values['installation-id'] } };
const resolvedPath = configPath.replace(/\.jsonc?$/, '.resolved.jsonc');
const wrangler = fileURLToPath(new URL('./node_modules/.bin/wrangler', import.meta.url));
const plan = {
  config: resolvedPath,
  migration: ['d1', 'migrations', 'apply', config.d1_databases[0].database_name, '--remote', '--config', resolvedPath],
  secrets: ['GITHUB_PRIVATE_KEY_PKCS8'],
  receiver_notice: 'Configure FEEDBACK_ADMIN_DELETE_TOKEN separately on the receiver according to its actual Pages or Worker deployment type; this plan contains no token value.',
  deploy: ['deploy', '--config', resolvedPath],
};
if (!values.apply) {
  console.log(JSON.stringify({ dry_run: true, plan, vars: resolvedConfig.vars }));
  process.exit(0);
}
await import('node:fs/promises').then(({ writeFile }) => writeFile(resolvedPath, JSON.stringify(resolvedConfig, null, 2) + '\n', { mode: 0o600 }));
const run = args => execFileSync(wrangler, args, { stdio: 'inherit', env: { ...process.env, CLOUDFLARE_ACCOUNT_ID: config.account_id, WRANGLER_SEND_METRICS: 'false' } });
run(plan.migration);
for (const [name, value] of [['GITHUB_PRIVATE_KEY_PKCS8', privateKey]]) {
  execFileSync(wrangler, ['secret', 'put', name, '--config', resolvedPath], { stdio: ['pipe', 'inherit', 'inherit'], input: value + '\n', env: { ...process.env, CLOUDFLARE_ACCOUNT_ID: config.account_id, WRANGLER_SEND_METRICS: 'false' } });
}
run(plan.deploy);
console.log(JSON.stringify({ deployed: config.name, cron: config.triggers?.crons || [], migration_applied: true }));
