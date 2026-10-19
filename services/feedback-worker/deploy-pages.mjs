import { promises as fs } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { resolve, join } from 'node:path';
import { parseArgs } from 'node:util';
import { fileURLToPath } from 'node:url';
import { REPOSITORY, validateInstallation } from './setup-github-app.mjs';
import { readCloudflareToken } from './cloudflare-auth.mjs';
import { pagesConfiguration } from './pages-configuration.mjs';

const root = fileURLToPath(new URL('.', import.meta.url));
const wrangler = join(root, 'node_modules/.bin/wrangler');
const { values } = parseArgs({ options: { configuration: { type: 'string' },
  'app-configuration': { type: 'string' }, directory: { type: 'string' }, enable: { type: 'boolean', default: false } } });
if (!values.configuration || !values['app-configuration'] || !values.directory) throw new Error('Configuration, verified GitHub App and private deployment directory are required');
const config = JSON.parse(await fs.readFile(resolve(values.configuration), 'utf8'));
const app = JSON.parse(await fs.readFile(resolve(values['app-configuration']), 'utf8'));
const pages = pagesConfiguration(config, app, resolve(root, 'dist/pages'), values.enable);
const mode = (await fs.stat(app.key_path)).mode & 0o777;
if (mode !== 0o600) throw new Error('GitHub App private key permissions must be 0600');
const key = await fs.readFile(app.key_path, 'utf8');
if (!await validateInstallation({ appId: app.app_id, installationId: app.installation_id, key, repositoryId: app.repository_id })) throw new Error('GitHub App must select exactly the approved private feedback repository');
const credential = readCloudflareToken(wrangler);
async function cloudflare(path, method = 'GET', data) {
  const response = await fetch('https://api.cloudflare.com/client/v4/accounts/' + config.account_id + path, {
    method, redirect: 'error', signal: AbortSignal.timeout(30000),
    headers: { Authorization: 'Bearer ' + credential, 'Content-Type': 'application/json' },
    body: data ? JSON.stringify(data) : undefined,
  });
  const result = await response.json();
  if (!response.ok || !result.success) throw new Error('Cloudflare deployment API HTTP ' + response.status);
  return result.result;
}
const directory = resolve(values.directory);
await fs.mkdir(directory, { recursive: true, mode: 0o700 });
await fs.chmod(directory, 0o700);
await fs.writeFile(join(directory, 'wrangler.jsonc'), JSON.stringify(pages, null, 2) + '\n', { mode: 0o600 });
const env_vars = Object.fromEntries(Object.entries(pages.vars).map(([name, value]) => [name, { type: 'plain_text', value }]));
env_vars.GITHUB_PRIVATE_KEY_PKCS8 = { type: 'secret_text', value: key };
await cloudflare('/pages/projects/' + config.name, 'PATCH', { deployment_configs: { production: {
  compatibility_date: config.compatibility_date, env_vars,
  d1_databases: { DB: { id: config.d1_databases[0].database_id } },
} } });
execFileSync(wrangler, ['pages', 'deploy', pages.pages_build_output_dir, '--project-name', config.name,
  '--branch', 'main', '--commit-dirty=true'], { cwd: directory, stdio: 'inherit',
  env: { ...process.env, CLOUDFLARE_ACCOUNT_ID: config.account_id, WRANGLER_SEND_METRICS: 'false' } });
const project = await cloudflare('/pages/projects/' + config.name);
const receipt = { repository: REPOSITORY, repository_count: 1, repository_selection: 'selected',
  project: project.name, endpoint: 'https://' + project.subdomain + '/v1/feedback', enabled: values.enable,
  deployment_id: project.canonical_deployment?.id, environment: project.canonical_deployment?.environment,
  deployed_at: new Date().toISOString(), contains_client_credentials: false, private_key_logged: false,
  paid_plan_activated: false,
  build_manifest: JSON.parse(await fs.readFile(join(root, 'dist/build-manifest.json'), 'utf8')) };
await fs.writeFile(join(directory, 'deployment-receipt.json'), JSON.stringify(receipt, null, 2) + '\n', { mode: 0o600 });
console.log(JSON.stringify(receipt));
