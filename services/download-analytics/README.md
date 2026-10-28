# Download analytics

A standalone Cloudflare Pages project that counts public download starts and redirects
to the official GitHub release assets. It is intentionally separate from the feedback
receiver so a quota or deployment problem in one service cannot take down the other.

## What it records

- Version, asset name, route (`public` or `release-check`) and UTC time. The daily
  bucket shown in statistics uses the Beijing calendar.
- A random identifier stored in a first-party cookie for 30 days. It is written only by the
  `/download` response, i.e. by a click on a download link; opening the landing page or
  `/privacy` sets no cookie. Only its SHA-256 hash is stored.
- No IP address, account, device information, raw User-Agent or browsing history.

Rows have a 30-day retention target. Cleanup runs opportunistically on writes; deployment also installs
`skyrocket-downloads-cleanup`, a route-less daily cron Worker bound to the same D1 database. Expired
rows are deleted in bounded batches, so a backlog or failed task can delay deletion. The deploy script
reads the registered cron schedule back before it can report success.

## Routes

| Route | Purpose |
| --- | --- |
| `GET /` | Dark landing page: download button (version and DMG size injected at build time), three-step install, requirements, FAQ, ZIP / checksum links |
| `GET /assets/*` | Same-origin static files (CSS, script, icons). Served by Pages directly; the Worker only falls back to the `ASSETS` binding |
| `GET /health` | Readiness, `database` reachability and the injected `build_version` |
| `GET /download?version=X.Y.Z&asset=...` | Validate the asset, record one event, 302 to GitHub |
| `GET /download?...&route=release-check` | Same, but requires the `x-skyrocket-check` marker or the owner cookie |
| `GET /owner` | Basic-auth gate that hands the browser a 30-day owner cookie, then returns to `/` |
| `GET /stats?days=N` | Owner-only HTML summary (HTTP Basic, user `owner`) |

The release-check marker is generated once and stored in the macOS keychain under
service `SkyrocketDownloadCheck`. Tools read it through the keychain or the
`SF_DOWNLOAD_CHECK_TOKEN` environment variable. It is installed as a Cloudflare Pages server-side
Secret and is never returned to the public page, browser client, or download redirect target.

Browsing the landing page in a browser cannot send a custom header, so the owner opens
`https://skyrocket-downloads.pages.dev/owner` once (user `owner`, keychain password) and
gets an `HttpOnly` cookie. Manual clicks on that device are then counted as
`release-check` instead of inflating the public total. The cookie holds a SHA-256 value derived
from the marker (`skyrocket-owner-cookie:<marker>`), never the marker itself, so a leaked cookie
does not leak the marker; the header path used by the release tooling still takes the real
marker. Cookies issued before this change stop working: visit `/owner` once again.

The redirect target is fixed to the official release repository. Only the four asset
names for the stated version are accepted, so the entry cannot act as an open redirect.
Statistics failures never block the download.

## Application icon

The icon is not redrawn for the web. `build-pages.mjs` runs `export-icon.py`, which calls
`sf_icons.render_app_icon("light", 128)` — the same function that draws `icon.icns` — and
writes it to `dist/pages/assets/app-icon.png`. The favicon, the header mark and the install
steps all reference that one file, so the site, the Dock and the Finder show the same mark.
The site is dark-only; only the light-plate icon is used.

## Static assets and CSP

`public/` is copied into `dist/pages/` on every build (the previous `assets/` is removed
first, so a deleted file cannot linger). `_routes.json` excludes `/assets/*`, so Pages serves
those files without invoking the Worker; `_headers` adds `nosniff`, `no-referrer` and a one-year
`immutable` cache. `build-pages.mjs` appends a ten-character SHA-256 content fingerprint to every
asset URL (including the app icon URL inside the stylesheet), so the long cache cannot pin an
old file after a replacement. The deployment readback reads the fingerprinted URLs from the live
page and checks the returned bytes against the fingerprint.

The default CSP is `default-src 'none'; img-src 'self' data:; media-src 'self';
style-src 'self' 'unsafe-inline'; script-src 'self'`. `media-src 'self'` is there for the hero
video; without it `default-src 'none'` blocks the video silently and only the poster shows.
`script-src` is the part that matters: no inline script, no
`unsafe-inline`, so the page's only script is `/assets/landing.js`. `/stats`, `/privacy` and
the 400 page keep `'unsafe-inline'` for their inline `<style>`; the landing page alone is
tightened to `style-src 'self'` because it has no inline styles (a test fails if one appears).
`/assets/atmo.svg` is additionally served with a sandbox policy, since an SVG opened directly
can carry script. If a static file is missing or the CSP drops `'self'`, Chrome silently paints
nothing while the page still returns 200, so both the worker tests and the deploy-time live
check assert the header, the `script-src` value exactly, and fetch every asset with its type.

## Hardening and failure behaviour

- Every Worker response carries `x-content-type-options`, `referrer-policy`, `x-frame-options`,
  the CSP, a `permissions-policy` that turns off device capabilities, `cross-origin-opener-policy`
  and `cross-origin-resource-policy` (`same-origin`), and a one-year `strict-transport-security`
  without `includeSubDomains` or `preload`. The deploy readback checks them on the live page.
- `/` uses `public, max-age=60, s-maxage=300, stale-while-revalidate=60`: a browser may reuse the
  landing page for one minute and Cloudflare may serve it for five minutes while checking freshness.
  The page sets no cookie, and every static asset has a content fingerprint plus one-year
  `immutable` caching. `/privacy` remains `no-cache`; stateful routes, errors and redirects remain
  `no-store`.
- Statistics now count one download start per browser, route, version and asset per 60-second
  window. The D1 primary key makes this server-side and atomic, so retry storms cannot inflate the
  count. Known bots, prefetchers and command-line probes receive the same official redirect but no
  cookie and no event; unrecognised clients are deliberately not rejected or suppressed. No
  User-Agent, bot label or IP address is stored.
- The statistics write runs after the redirect (`ctx.waitUntil`); without a context (local script,
  tests) it is awaited. A failed or duplicate write is logged and swallowed. A version newer than the latest
  known one (cache first, then one forced manifest read) is not counted; an unreadable manifest
  lets the event through, so a real download is never dropped for lack of an answer.
- Known read-only paths answer other methods with `405` and `Allow: GET, HEAD`; unknown paths stay `404`.
- The lowest maintained Safari baseline is **Safari 15.4 / iOS 15.4**: that is where container
  query units and `:focus-visible` are supported. The page retains a `cqw` fallback and a normal
  `:focus` outline; `-webkit-backdrop-filter` is kept beside the standard property for the older
  WebKit rendering path.
- `fetch` has a top-level guard. A valid public download address still redirects if statistics or
  auth code throws; the landing page degrades to a small page linking the official release; other
  routes return a JSON 500. The release-check route never pretends to succeed.
- `landing.js` ignores a second click on the download link within 1.5 s (one intent, one file),
  moves focus to the folded backup section when it opens, reacts to `#alt` hash changes, and
  marks a failed image so the scene keeps its backdrop. Without scripting a `<noscript>` line
  links the backup release page.
- Rules that only a browser can show (320 px no longer scrolls sideways, `@supports` fallback for
  `cqw`, `forced-colors`, `prefers-contrast`) are checked in Chrome by hand; the test suite only
  guards that the rules exist and come after the rules they override.

Not decided here, left for the owner: Cloudflare-side WAF rules if the service later moves to a
custom domain. The current shared `pages.dev` hostname has no project-scoped WAF policy surface;
statistics deliberately retain their documented definition instead of guessing at a local throttle.

## Version and size injection

`build-pages.mjs` injects `APP_VERSION` and the byte size of that version's DMG
(`SF_DMG_PATH`, then `~/Desktop/Skyrocket/`, then `build/`). The size is shown only when the
displayed version is the built one; if the live manifest has moved on, or no DMG is found,
the page shows the version alone rather than an old size. `src/landing.mjs` is inlined into
`_worker.js` at build time because Pages takes a single worker file.

## Media that is here without clearance

Since 2026-10-28 the page uses the B25 mock-up media as designed: the hero is a looping video of
real 2026-10-27 minute-line data of one A-share stock rendered by the product (its name and code
replaced by the placeholders 股票名称 / 038492; the price path is still real) (WebM for Chrome, HEVC MOV for
Safari, a still poster for reduced motion), and the four desktop scenes are rendered images with
the Apple menu bar and Dock glass (its icons are pale flat blocks with the product's own icon
in one slot, no Apple app icons) over four CC0 wallpaper photos from Wikimedia Commons (the CC0
tag is read from the Commons file pages, not re-checked at the original sites; see `ASSETS.md`).
The wallpapers are no longer Apple's and the Sintel (CC BY 3.0) frame is gone. The video, the
menu bar, the Dock glass and the SF Symbols still have no verified
licence for redistribution, and `services/` is part of the public GPL source snapshot, so
**replace them before a public snapshot or a production deploy**. `ASSETS.md` lists every file
with its origin and clearance status, and a test fails if a public file is missing from it or a
restored file loses its "未取得授权" mark. The previous cleared media (fictional stock hero,
Unsplash wallpapers, HTML/CSS scenes) is in commit `e79022a`.

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
After each production deploy it verifies all four accepted download assets (DMG, macOS ZIP,
source ZIP and checksums) through Pages' 302 and GitHub's signed release-asset redirect using
HEAD only; it never downloads a release asset as part of the check.

The deployment script does production publish-and-readback only. It is intentionally not used for
preview or rollback: both are separate, explicit operator actions.

```sh
# Deploy the current clean committed tree to an isolated Pages preview branch and verify it.
npm run preview

# Roll production back only to a specific, previously successful production deployment ID.
npm run rollback -- <deployment-id>
```

`pages-control.mjs preview` provisions and binds a separate D1 database for preview on first use,
so preview clicks cannot alter production counts. It verifies the preview URL and confirms the
production canonical deployment did not change. `rollback` rejects any current, preview, failed or
unknown deployment; after the explicit Pages API rollback it reads the production health route back.
Neither command is automatic, and neither rolls back on a single failed probe.

The release-check marker is generated once and stored in the macOS keychain under
service `SkyrocketDownloadCheck`. Tools read it through the keychain or the
`SF_DOWNLOAD_CHECK_TOKEN` environment variable. It is installed as a Cloudflare Pages server-side
Secret and is never returned to the public page, browser client, or download redirect target.

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
