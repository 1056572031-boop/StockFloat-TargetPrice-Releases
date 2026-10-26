# Download analytics

A standalone Cloudflare Pages project that counts public download starts and redirects
to the official GitHub release assets. It is intentionally separate from the feedback
receiver so a quota or deployment problem in one service cannot take down the other.

## What it records

- Version, asset name, route (`public` or `release-check`) and UTC time. The daily
  bucket shown in statistics uses the Beijing calendar.
- A random anonymous identifier stored in a first-party cookie for 30 days. The landing
  page establishes it before any download; only its SHA-256 hash is stored.
- No IP address, account, device information, raw User-Agent or browsing history.

Rows expire after 30 days. Cleanup runs opportunistically on writes; deployment also installs
`skyrocket-downloads-cleanup`, a route-less daily cron Worker bound to the same D1 database.
The deploy script reads the registered cron schedule back before it can report success.

## Routes

| Route | Purpose |
| --- | --- |
| `GET /` | Landing card: application icon, version chips, DMG / ZIP / checksum actions |
| `GET /health` | Readiness, `database` reachability and the injected `build_version` |
| `GET /download?version=X.Y.Z&asset=...` | Validate the asset, record one event, 302 to GitHub |
| `GET /download?...&route=release-check` | Same, but requires the `x-skyrocket-check` marker or the owner cookie |
| `GET /owner` | Basic-auth gate that hands the browser a 30-day owner cookie, then returns to `/` |
| `GET /stats?days=N` | Owner-only HTML summary (HTTP Basic, user `owner`) |

The release-check marker is generated once and stored in the macOS keychain under
service `SkyrocketDownloadCheck`. Tools read it through the keychain or the
`SF_DOWNLOAD_CHECK_TOKEN` environment variable. The public site never receives it.

Browsing the landing page in a browser cannot send a custom header, so the owner opens
`https://skyrocket-downloads.pages.dev/owner` once (user `owner`, keychain password) and
gets an `HttpOnly` cookie. Manual clicks on that device are then counted as
`release-check` instead of inflating the public total.

The redirect target is fixed to the official release repository. Only the four asset
names for the stated version are accepted, so the entry cannot act as an open redirect.
Statistics failures never block the download.

## Application icon

The icon is not redrawn for the web. `build-pages.mjs` runs `export-icon.py`, which calls
`sf_icons.render_app_icon(mode, 128)` — the same function that draws `icon.icns` — and
injects both appearance tiers as `data:image/png` URIs. That keeps the site, the Dock and
the Finder showing one and the same mark.

Because the icon travels as a data URI, the CSP must keep `img-src data:`. Without it
Chrome silently paints nothing while the page still returns 200 and every test stays
green, so both the worker test and the deploy-time live check assert the header.

## Deployment

The project uses the existing free Cloudflare account and a dedicated D1 database
(`skyrocket-downloads`). No paid plan is required.

```sh
cd services/download-analytics
npm ci --prefix ../feedback-worker   # provides the pinned wrangler binary
node --test tests/*.test.mjs
CLOUDFLARE_ACCOUNT_ID=<account> WRANGLER_SEND_METRICS=false node deploy-pages.mjs
```

The build step also runs `export-icon.py` through the project venv
(`../../.venv/bin/python`, override with `SF_PYTHON`) to render the icon.

`deploy-pages.mjs` rebuilds the landing page, creates or reuses the Pages project, binds
the D1 database, installs the `CHECK_TOKEN` secret, deploys `dist/pages`, and reads the
binding back. It never prints the secret. The build injects
`sf_constants.APP_VERSION` as the fallback. The live page reads the published OTA manifest
version from a short-lived edge cache and refreshes it in the background, so the page
never waits for GitHub and a missed redeploy does not leave the buttons on an old release.

The release-check marker is generated once and stored in the macOS keychain under
service `SkyrocketDownloadCheck`. Tools read it through the keychain or the
`SF_DOWNLOAD_CHECK_TOKEN` environment variable. The public site never receives it.

## Boundaries

- Reachability is not guaranteed for every user. The chain is visitor → Cloudflare
  `pages.dev` → GitHub release asset, and each hop has its own failure mode: the shared
  `pages.dev` domain is routed differently by carriers, and the asset host is already
  unreachable from the development network. The entry only issues a 302, so it cannot
  rescue a blocked GitHub hop — the landing page and README keep a direct
  GitHub release-page link for that case.
- Binding a custom domain would improve first-hop reachability, but the domain itself
  costs money, which the project has ruled out.
- Application OTA downloads still go directly to GitHub. Routing all updates through this
  free entry would make a daily quota or outage break every user's updater.
- Public counts therefore cover web download starts, not in-app OTA traffic.
- Cloudflare still sees requests as the network operator; this service only controls
  what its own statistics table stores.
- The entry is an analytics aid, not a content proxy: the bytes come from GitHub.
