import { execFileSync } from 'node:child_process';
import { parseArgs } from 'node:util';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const { values } = parseArgs({ options: {
  configuration: { type: 'string' },
  apply: { type: 'boolean', default: false },
} });

if (!values.configuration) throw new Error('A retention Worker configuration is required');
const config = JSON.parse(readFileSync(values.configuration, 'utf8'));
const database = config.d1_databases?.[0]?.database_name;
if (!database || !config.d1_databases?.[0]?.database_id || config.d1_databases[0].database_id.startsWith('00000000-')) {
  throw new Error('Retention migration configuration is not bound to a D1 database');
}
const wrangler = fileURLToPath(new URL('./node_modules/.bin/wrangler', import.meta.url));
const args = ['d1', 'migrations', 'apply', database, '--remote', '--config', values.configuration];
if (!values.apply) {
  console.log(JSON.stringify({ dry_run: true, command: [wrangler, ...args] }));
} else {
  execFileSync(wrangler, args, { stdio: 'inherit' });
}
