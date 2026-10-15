"""Class availability alone does not prove the full native material API exists."""

import types
import unittest

from tests._glass_capabilities import LIQUID_SELECTORS, liquid_api_gaps


class LiquidCapabilityTests(unittest.TestCase):
    def toolkit(self, missing=()):
        return types.SimpleNamespace(
            NSGlassEffectView=types.SimpleNamespace(
                instancesRespondToSelector_=lambda selector: selector not in missing),
            NSGlassEffectViewStyleRegular=0,
            NSAppearanceNameDarkAqua="DarkAqua")

    def test_absent_class_is_unavailable(self):
        self.assertEqual(liquid_api_gaps(types.SimpleNamespace()), ["NSGlassEffectView"])

    def test_complete_interface_is_available(self):
        self.assertEqual(liquid_api_gaps(self.toolkit()), [])

    def test_each_missing_selector_is_reported_even_when_class_exists(self):
        for selector in LIQUID_SELECTORS:
            with self.subTest(selector=selector):
                self.assertEqual(liquid_api_gaps(self.toolkit((selector,))), [selector])

    def test_each_missing_constant_is_reported(self):
        for name in ("NSGlassEffectViewStyleRegular", "NSAppearanceNameDarkAqua"):
            with self.subTest(name=name):
                api = self.toolkit()
                delattr(api, name)
                self.assertEqual(liquid_api_gaps(api), [name])

    def test_all_gaps_are_preserved(self):
        api = self.toolkit(LIQUID_SELECTORS)
        del api.NSGlassEffectViewStyleRegular
        self.assertEqual(liquid_api_gaps(api), ["NSGlassEffectViewStyleRegular", *LIQUID_SELECTORS])

    def test_broken_probe_is_not_silently_reported_as_unsupported(self):
        def fail(selector):
            raise RuntimeError("native preflight failed")

        api = self.toolkit()
        api.NSGlassEffectView.instancesRespondToSelector_ = fail
        with self.assertRaisesRegex(RuntimeError, "native preflight failed"):
            liquid_api_gaps(api)
