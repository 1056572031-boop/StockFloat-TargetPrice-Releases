# Code Governance: 2026-10-14

## Scope

This batch starts from commit `8bc6a0e2f2cbd2d20ccb18a9291c11c9556bad20`
with a clean working tree. It changes five files at most and does not publish,
package, install, or change a version.
Two unrelated untracked quality-inspection artifacts appeared under `outputs/`
during verification; they were left untouched and are not part of this batch.

## Changes

- `sf_quote.py`: reuse `_is_finite_str` for both quote sources and add
  `_optional_quote_field` for indexed optional fields. Remove both nested
  `_num` implementations and redundant fallback high/low indexing.
- `sf_quote.py`: rename local `f`, `pre`, and `change_s` bindings to `fields`,
  `pre_close`, and `change_text`. Keep every existing module-level signature.
- `sf_quote.py`: correct field-contract comments, source-specific code forms,
  calculated versus supplied change/pct fields, limit-price display versus input
  validation, market-prefix mapping, and semantic-check failure handling.
- `sf_editor.py`: normalize a candidate code once in `_is_dup`; name local
  candidate buttons and deleted widgets explicitly. Correct the zombie-thread
  soft-limit comment without changing thread retention or search sequencing.
- `tests/test_code_governance.py`: add 16 behavior tests for lexical numeric
  preservation, missing/invalid/nonfinite fields, price precision, required-field
  errors, fallback calculation failure, quote schemas, and code normalization.
- `tests/test_arch_constraints.py`: tighten only the `sf_quote.py` silent-except
  ceiling from 8 to 7 after removing the redundant optional-field exception block.

## Preserved Contracts

- Required quote prices still use strict validation and the same errors.
- Valid optional fields keep their original whitespace, signs, and trailing zeros.
- Invalid optional fields remain empty strings; fallback nonpositive pre-close
  still yields unknown change/pct; calculation failures keep historical zero text.
- Quote keys, source-specific codes, price precision, request arguments, warning
  messages, quote status rules, drawing, timers, animation, and thread sequencing
  are unchanged.
- Public signatures, configuration keys, upstream URL parameters, native bridge
  method names, and reexports are unchanged.
- Release scripts, packaging/dependency/CI configuration, release declarations,
  and static/coverage baseline files are unchanged. Coverage floors remain
  product 89% and tools 78%.

## Evidence

- Before edits: `QT_QPA_PLATFORM=offscreen ./run_tests.sh`, exit 0;
  3464 tests, 0 failures/errors, 0 flaky, user data unchanged.
- Before edits: product coverage 91.52%; tools coverage 84.49%.
- New behavior tests: all 16 pass against both the original and cleaned code.
- Static gate: A-level findings 0, B-level findings 313, unchanged from baseline.
- Test-discipline scan: 2888 findings, unchanged from baseline; no new debt.
- Direct old/new comparison: 3769 cases, all equal. Compared returned dictionaries,
  exception types/messages, HTTP call arguments, and warning logs. Original
  functions were loaded from the starting commit into isolated namespaces.
- AST comparison: all existing quote signatures and all other quote function
  bodies match after excluding docstrings. All editor signatures and all other
  methods match after normalizing the two local button/widget renames.
- Mutation checks: stripping numeric text, removing bounds checks, and skipping
  finite-number validation were all detected (3/3); changes were in-memory only.
- Protected-path Git comparison: no changes to release scripts, packaging,
  dependencies, CI, installer/updater, constants, declarations, or gate baselines.
- Final full gate: `QT_QPA_PLATFORM=offscreen ./run_tests.sh`, exit 0;
  3480 tests, 0 failures/errors, 0 flaky, user data unchanged. Static, native-bridge,
  offscreen pixel, regression, and coverage checks passed; gate stamp written.
- Final product coverage: 91.57% (12264 statements, 1034 uncovered), up from
  91.52% (12271 statements, 1040 uncovered).
- Final tools coverage: 84.49% (6233 statements, 967 uncovered), unchanged.
  Subprocess-executed checks are not included in this coverage accounting;
  uncounted subprocess work is not the same as untested work.

Logs: `/tmp/stockfloat-governance-before-20261014.log` and
`/tmp/stockfloat-governance-after-20261014.log`.

## Deferred Work

This is a bounded cleanup batch, not a claim that the repository contains no
duplication. Similar methods with distinct responsibilities (`enter_dock` versus
`exit_dock`, glass appearance accessors, and Qt event overrides) are retained.
Release-tool duplicate candidates are out of scope. Dynamic names, external
contracts, and UI geometry are not renamed or consolidated.
