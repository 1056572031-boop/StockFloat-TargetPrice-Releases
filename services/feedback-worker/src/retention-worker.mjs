import { GitHubStore, PUBLISH_DEADLINE_MS, installationToken, pruneExpiredCases, reconcileRetentionIntents } from './index.mjs';

export default {
  async scheduled(_event, env, ctx) {
    if (!env.DB || !env.GITHUB_APP_ID || !env.GITHUB_INSTALLATION_ID
        || !env.GITHUB_PRIVATE_KEY_PKCS8 || !/^\d+$/.test(env.GITHUB_RELEASE_ID || '')) {
      console.error(JSON.stringify({ event: 'retention_worker_not_ready' }));
      return;
    }
    ctx.waitUntil((async () => {
      const token = await installationToken(env, fetch, Date.now() + PUBLISH_DEADLINE_MS);
      const store = new GitHubStore(token, env.GITHUB_RELEASE_ID, fetch);
      const reconciled = await reconcileRetentionIntents(env, store);
      const result = await pruneExpiredCases(env, store);
      const review = await env.DB.prepare("SELECT COUNT(*) AS n FROM retention_intents WHERE state='manual_review'").bind().first();
      console.log(JSON.stringify({ event: 'retention_prune_finished', bound: reconciled.bound,
        orphan_cleanup: reconciled.orphanCleanup, orphan_backlog: reconciled.orphanBacklog,
        unresolved: reconciled.unresolved, retryable: reconciled.retryable, reconcile_busy: reconciled.busy,
        manual_review: review.n, deletion_passes: result.deletedCaseIds.length,
        deletion_failed: result.failed, deletion_busy: result.busy, deletion_backlog: result.backlog }));
    })());
  },
};
