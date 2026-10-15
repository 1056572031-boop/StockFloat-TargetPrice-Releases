# Popup Material Unification

Date: 2026-10-15. Source version: v1.4.92. Status: validated source changes,
not an installed or published material fix.

## Cause

The main card used native glass, while the stock editor and target-price panel
painted a separate gray-blue translucent surface. Restoring popup transparency
did not make those different rendering paths equivalent.

## Implementation

- Both popups call the main card's actual background renderer. Native dimming,
  system-appearance behavior, corner geometry, rim and non-native fallback are
  therefore shared, not independently approximated.
- Each popup follows the main card's actual glass backend. A main card running
  legacy glass does not accidentally get a Liquid Glass popup.
- Failed native creation records its reason and uses the shared software
  fallback. Exact native appearance is not claimed for that degraded state.
- The native effect view is inserted below Qt's content view in the same native
  window. No independent popup background NSWindow is allocated, no Qt content
  view is replaced, and Qt's native window is never manually closed or released.
- The effect view is detached before releasing Python references on hide, close,
  direct deleteLater, or failed setup. Hidden popups do not create late surfaces.
- Destroyed-signal arguments cannot replace the captured popup identity used to
  clear the main window's reference. Space retries discard deleted Qt wrappers.
- Diagnostics report embedded versus separate-window attachment, actual backend,
  fallback reason and the currently open editor/target surfaces.

## Acceptance

Unit coverage compares real editor and target widgets against the main background
renderer in Liquid Glass, legacy glass, translucent and solid modes, with light
and dark appearances at 1x and 2x. Restoring the old tinted paint must fail.

The native probe uses production popup entrances and checks:

1. Same backend as the main card, one NSWindow per popup, material below Qt.
2. Real compositor screenshots against light, dark, warm and cool backdrops.
3. Background RGB samples within 8 levels per channel, visible material and text,
   and a verified backdrop so blank or wrong-region screenshots cannot pass.
4. Movement, five hide/show cycles per popup, twenty create/destroy cycles per
   popup, autorelease-pool draining, detached views and no native-window growth.
5. Destroyed popups clear their owner references.
6. Optional actual full-screen Space entry/exit and popup visibility there.

Color sampling temporarily isolates the four scene windows above other GUI
probes, then restores their original levels, focus-hiding policy and watchdog.
That restoration is asserted. Movement, lifecycle and full-screen checks use
production levels; the sampling stage does not claim to prove those policies.

`tools/verify_card_corners.py --native` now invokes popup checks for each available
backend. Its bundle check also verifies the embedded attachment, teardown,
injection and shared fallback alpha, so an old packaged popup cannot pass just
because the checkout's screenshots look correct.

## Evidence And Limits

- Final complete gate: 3,875 tests, zero failures/errors, zero flaky results,
  five environment/artifact-dependent skips. Product coverage 91.33%; chain-tool
  coverage 79.86%. User config, securities cache and debug log were unchanged.
  The content-sensitive gate stamp was written and independently checked current.
- Initial native runs: Liquid Glass maximum sampled RGB difference 0; legacy
  glass maximum sampled RGB difference 1, across both popups and four backdrops.
- Old tinted-paint mutation: 32 pixel-comparison failures, zero test errors.
- Final serial native runs: 147 checks per backend, all passed, including actual
  full-screen Space entry/exit, popup content, repeated teardown and restored
  capture levels. Both popups are checked to remain in that full-screen Space.
  Neither native log contains SWALLOWED, UNCAUGHT or Traceback.
- Native resizing: editor and target panel, both backends, original size,
  expanded size and restored size all matched the native view; all four views
  detached on destruction. Evidence: `build/popup-materials-resize-20261015.log`.
- Final native results and screenshots: `build/popup-materials-release-proof-20261015/`.
- Full-regression logs: `build/popup-materials-regression-20261015.log` and the
  subsequent `build/popup-materials-regression-verified-20261015.log`.
  An interrupted or failed run is not acceptance.
- The first full run exposed a hideEvent fault-injection abort. Cleanup failures
  now remain inside the Qt event boundary and are logged; that run is not green.
- Native GUI probes must run serially. Another real-window test or a full-screen
  transition can alter compositor screenshots and window-list ordering.
- A background diagnostics test raced with its worker clearing the thread field.
  It now waits for completion and asserts the injected failure was reached;
  thirty consecutive runs passed with no failures or errors.
- Native checks use this machine and simulated Qt input. Running the legacy
  backend here is not a claim that every older macOS version has been tested.
- The installed app advanced from v1.4.90 to v1.4.91 during parallel work. Its
  bytecode still lacks PopupGlassSurface and the current material implementation;
  this source task has not installed or published these changes.
  Installing this fix still requires signing, build, bundle, local-install and
  installed-startup gates. Public distribution additionally requires the OTA
  and public-source stages; a local-only install must not claim public release.
