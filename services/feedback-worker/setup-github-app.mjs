import { createServer } from 'node:http';
import { createPrivateKey, randomBytes, sign } from 'node:crypto';
import { promises as fs } from 'node:fs';
import { execFile, execFileSync } from 'node:child_process';
import { resolve, join } from 'node:path';
import { parseArgs } from 'node:util';
import { pathToFileURL } from 'node:url';

export const REPOSITORY = '1056572031-boop/Skyrocket-Private-Feedback';
export const OWNER = REPOSITORY.split('/')[0];
const html = value => String(value).replace(/[&<>"']/g, x => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[x]);

export function appJwt(appId, key, now = Math.floor(Date.now() / 1000)) {
  const encode = value => Buffer.from(JSON.stringify(value)).toString('base64url');
  const payload = encode({ alg: 'RS256', typ: 'JWT' }) + '.' + encode({ iat: now - 60, exp: now + 540, iss: String(appId) });
  return payload + '.' + sign('RSA-SHA256', Buffer.from(payload), key).toString('base64url');
}

export function manifest({ origin, workerURL, nonce }) {
  if (!workerURL.startsWith('https://') || !/^[a-f0-9]{32}$/.test(nonce)) throw new Error('Invalid setup origin');
  const callbackOrigin = new URL(origin);
  if (['127.0.0.1', '[::1]'].includes(callbackOrigin.hostname)) callbackOrigin.hostname = 'localhost';
  if (callbackOrigin.hostname !== 'localhost' || callbackOrigin.protocol !== 'http:') throw new Error('Setup requires loopback localhost');
  return {
    name: 'SkyrocketFeedback-' + nonce.slice(0, 12), url: 'https://github.com/' + REPOSITORY,
    redirect_url: callbackOrigin.origin + '/created/' + nonce,
    setup_url: callbackOrigin.origin + '/installed/' + nonce,
    hook_attributes: { url: workerURL + '/disabled-github-events', active: false },
    public: false, default_permissions: { contents: 'write' }, default_events: [],
  };
}

async function privateFile(path, data) {
  const temporary = path + '.' + randomBytes(8).toString('hex') + '.tmp';
  const handle = await fs.open(temporary, 'wx', 0o600);
  try { await handle.writeFile(data); await handle.sync(); } finally { await handle.close(); }
  await fs.rename(temporary, path);
}

async function github(path, { jwt, token, method = 'GET', data } = {}) {
  const headers = { Accept: 'application/vnd.github+json', 'User-Agent': 'Skyrocket-Feedback-Setup/1', 'X-GitHub-Api-Version': '2022-11-28' };
  if (jwt || token) headers.Authorization = 'Bearer ' + (jwt || token);
  if (data) headers['Content-Type'] = 'application/json';
  const response = await fetch('https://api.github.com' + path, {
    method, headers, body: data ? JSON.stringify(data) : undefined,
    redirect: 'error', signal: AbortSignal.timeout(30000),
  });
  if (!response.ok) throw new Error('GitHub setup HTTP ' + response.status);
  return response.json();
}

export async function validateInstallation({ appId, installationId, key, repositoryId, api = github }) {
  const jwt = appJwt(appId, key);
  const installation = await api('/app/installations/' + installationId, { jwt });
  const permissions = installation.permissions || {};
  if (installation.app_id !== Number(appId) || installation.account?.login !== OWNER
      || installation.repository_selection !== 'selected' || permissions.contents !== 'write'
      || Object.keys(permissions).some(name => !['contents', 'metadata'].includes(name))) throw new Error('请只授权 Skyrocket-Private-Feedback 仓库的内容权限');
  const token = await api('/app/installations/' + installationId + '/access_tokens', { jwt, method: 'POST' });
  const repos = await api('/installation/repositories?per_page=100', { token: token.token });
  return repos.total_count === 1 && repos.repositories?.[0]?.id === repositoryId && repos.repositories[0].private === true;
}

export async function startSetup({ directory, workerURL, port = 8863, gh = 'gh', api = github, openBrowser = true,
  loadRepository = executable => JSON.parse(execFileSync(executable, ['api', 'repos/' + REPOSITORY], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] })) }) {
  directory = resolve(directory);
  await fs.mkdir(directory, { recursive: true, mode: 0o700 });
  await fs.chmod(directory, 0o700);
  const statePath = join(directory, 'setup-state.json');
  let state;
  try { state = JSON.parse(await fs.readFile(statePath, 'utf8')); }
  catch (error) { if (error.code !== 'ENOENT') throw error; state = { nonce: randomBytes(16).toString('hex') }; }
  const repo = await loadRepository(gh);
  if (repo.private !== true || repo.full_name !== REPOSITORY) throw new Error('Approved private repository not available');
  let origin = 'http://localhost:' + port;
  state.origin = origin;
  await privateFile(statePath, JSON.stringify(state));
  let complete;
  const completion = new Promise(resolve => { complete = resolve; });
  const redirectInstall = response => {
    const url = new URL(state.app_url + '/installations/new');
    url.searchParams.set('state', state.nonce);
    url.searchParams.set('target_id', String(repo.owner.id));
    url.searchParams.append('repository_ids[]', String(repo.id));
    response.writeHead(302, { Location: url.href, 'Cache-Control': 'no-store' }); response.end();
  };
  const server = createServer(async (request, response) => {
    response.setHeader('Cache-Control', 'no-store');
    response.setHeader('Content-Type', 'text/html; charset=utf-8');
    try {
      const url = new URL(request.url, origin);
      const callback = /^\/(created|installed)\/([a-f0-9]{32})$/.exec(url.pathname);
      const queryState = url.searchParams.get('state');
      const pathState = callback?.[2];
      if (request.method !== 'GET' || (queryState && queryState !== state.nonce)
          || (queryState !== state.nonce && pathState !== state.nonce)) {
        response.writeHead(403); response.end('Invalid setup state'); return;
      }
      const route = callback ? '/' + callback[1] : url.pathname;
      if (route === '/') {
        if (state.app_id) { redirectInstall(response); return; }
        const value = manifest({ origin, workerURL, nonce: state.nonce });
        response.end('<!doctype html><meta charset="utf-8"><title>Skyrocket 反馈授权</title>'
          + '<p>即将创建免费的专用 GitHub App，只需要反馈仓库的内容读写权限，不包含账户或工作流管理。</p>'
          + '<form method="post" action="https://github.com/settings/apps/new?state=' + state.nonce + '">'
          + '<input type="hidden" name="manifest" value="' + html(JSON.stringify(value)) + '">'
          + '<button>前往 GitHub 确认</button></form><script>document.forms[0].submit()</script>');
      } else if (route === '/created') {
        const code = url.searchParams.get('code');
        if (!code || !/^[A-Za-z0-9_-]{8,200}$/.test(code)) throw new Error('Missing GitHub authorization code');
        if (!state.app_id) {
          const result = await api('/app-manifests/' + encodeURIComponent(code) + '/conversions', { method: 'POST' });
          if (result.owner?.login !== OWNER || !result.pem || !result.id || !result.html_url?.startsWith('https://github.com/apps/')) throw new Error('GitHub App owner mismatch');
          const keyPath = join(directory, 'github-app-' + result.id + '.pem');
          const pem = createPrivateKey(result.pem).export({ type: 'pkcs8', format: 'pem' });
          await privateFile(keyPath, pem);
          state = { ...state, app_id: result.id, app_url: result.html_url, key_path: keyPath };
          await privateFile(statePath, JSON.stringify(state));
          console.log(JSON.stringify({ state: 'app_created', app_id: state.app_id, private_key_logged: false }));
        }
        redirectInstall(response);
      } else if (route === '/installed') {
        const id = url.searchParams.get('installation_id');
        if (!state.app_id || !/^\d+$/.test(id || '')) throw new Error('Missing GitHub installation');
        const selected = await validateInstallation({ appId: state.app_id, installationId: id,
          key: await fs.readFile(state.key_path, 'utf8'), repositoryId: repo.id, api });
        if (!selected) {
          response.end('<p>请将此 App 的仓库范围改为“Only select repositories”，只选择 Skyrocket-Private-Feedback，然后重新打开本机授权页。</p>'
            + '<a href="https://github.com/settings/installations/' + id + '">调整仓库授权</a>'); return;
        }
        state.installation_id = id;
        state.repository = REPOSITORY;
        const configPath = join(directory, 'configuration.json');
        await privateFile(statePath, JSON.stringify(state));
        await privateFile(configPath, JSON.stringify({ app_id: state.app_id, installation_id: id,
          key_path: state.key_path, repository: REPOSITORY, repository_id: repo.id }, null, 2));
        response.end('<!doctype html><meta charset="utf-8"><h2>专用 GitHub 授权已完成</h2><p>仅允许访问私有反馈仓库。可以关闭此页，部署和验证将继续自动进行。</p>');
        complete({ config_path: configPath, app_id: state.app_id, installation_id: id });
        server.close();
      } else { response.writeHead(404); response.end('Not found'); }
    } catch (error) {
      response.writeHead(400);
      response.end('<p>授权尚未完成：' + html(error.message) + '</p>');
      console.log(JSON.stringify({ state: 'authorization_incomplete', error: error.message }));
    }
  });
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(port, '127.0.0.1', resolve); });
  origin = 'http://localhost:' + server.address().port;
  state.origin = origin;
  await privateFile(statePath, JSON.stringify(state));
  const url = origin + '/?state=' + state.nonce;
  if (openBrowser) execFile('/usr/bin/open', [url], () => {});
  console.log(JSON.stringify({ state: 'awaiting_github_approval', url, repository_only: REPOSITORY }));
  const timer = setTimeout(() => { server.close(); complete({ status: 'authorization_timeout' }); }, 15 * 60 * 1000);
  timer.unref();
  completion.finally(() => clearTimeout(timer));
  return { server, completion, url };
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  const { values } = parseArgs({ options: { directory: { type: 'string' }, 'worker-url': { type: 'string' },
    port: { type: 'string', default: '8863' }, gh: { type: 'string', default: 'gh' } } });
  if (!values.directory || !values['worker-url']) throw new Error('--directory and --worker-url are required');
  const setup = await startSetup({ directory: values.directory, workerURL: values['worker-url'], port: Number(values.port), gh: values.gh });
  console.log(JSON.stringify(await setup.completion));
}
