import { REPOSITORY } from './setup-github-app.mjs';

export function pagesConfiguration(config, app, outputDirectory, enabled) {
  if (app.repository !== REPOSITORY || !config.account_id || !config.name || !config.d1_databases?.[0]?.database_id
      || config.d1_databases[0].database_id.startsWith('00000000-') || !/^\d+$/.test(String(config.vars?.GITHUB_RELEASE_ID))) {
    throw new Error('Deployment configuration is not bound to approved resources');
  }
  return { name: config.name, pages_build_output_dir: outputDirectory, compatibility_date: config.compatibility_date,
    vars: { GITHUB_APP_ID: String(app.app_id), GITHUB_INSTALLATION_ID: String(app.installation_id),
      GITHUB_RELEASE_ID: String(config.vars.GITHUB_RELEASE_ID), ENABLED: enabled ? 'true' : 'false' },
    d1_databases: [{ binding: 'DB', database_name: config.d1_databases[0].database_name, database_id: config.d1_databases[0].database_id }] };
}
