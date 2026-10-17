# Startup Health Diagnostics

This change is source-only. It does not modify or republish the installed
v1.4.93 application or its immutable release assets.

## Runtime Boundary

- Only a frozen Cocoa process schedules the sample, two seconds after its first
  show event. The parent-owned single-shot timer is not restarted on Dock restore.
- The callback reads Qt visibility and screen intersection, native visibility,
  active-Space membership, collection behavior, glass presence and compositor
  order/alignment, and the existing expected window level and alignment tolerance.
- The check does not activate, move, reorder, repair or show windows. It does not
  enter another Space, start a camera, request permission, perform codesigning,
  start a transport request, install an update or upload diagnostics.
- Missing native answers are `unverified`, not success. A user-minimized window
  is not treated as broken; user-selected non-glass backgrounds are not treated
  as missing glass. An exiting window is not sampled.
- Bundle identity is read from bounded Info.plist bytes and compared with product
  constants. Signature verification is explicitly **not** claimed by this check.
- Ordinary update checks retain a bounded, lock-protected, sanitized result after
  the existing signature/protocol validation. Same-version manifests retain their
  actual remote version. Errors retain only the exception class, not raw URLs or
  exception text. No additional update request or retry is added.

## Diagnostic Output

`startup_health.json` is added through the existing diagnostic archive and privacy
filter. `startup` is the retained one-time sample with its own UTC timestamp;
`current_update` is the latest normal update snapshot at export time. An early
startup sample normally sees `ota_manifest=unverified` because the normal update
check has not yet run. Later success does not rewrite the historical sample.

The checks distinguish `passed`, `failed`, `unverified`, and `not_applicable`.
Their aggregate is not release qualification. A visible native window on the
current Space is not proof of screenshot quality, other Spaces, another machine,
cross-version behavior, camera authorization or the real OTA install/rollback.

## Verification

Focused tests are `tests/test_startup_health.py` and
`tests/test_post_release_record.py`. They cover a real Qt widget, a read-only native
port fixture, actual diagnostic ZIP export, teardown and timer ownership, signature
and transport failure, artifact tampering, and executable source mutations.

`tools/native_glass_probe.py --startup-health --seconds 1 --out <directory>`
samples a real source window; add `--legacy` for the older material. Camera and
network remain disabled by that probe. Its successful native checks must not be
reported as frozen-package integration or complete cross-Space acceptance.

Before the next release, choose a new version, update the current release feature
checks, rebuild and inspect the PYZ, then verify startup sampling and diagnostic
export from the actual new installed application. Never overwrite v1.4.93 assets.
