"""Positive and negative controls for lexical diagnostic reachability."""

from pathlib import Path
import tempfile

from tests._base import SFCase
from tools import diag_coverage_check as gate


class DiagnosticClosureTests(SFCase):
    def setUp(self):
        super().setUp()
        self.directory = tempfile.TemporaryDirectory(prefix="sf-diagnostic-scope-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def scan(self, helper, invocation="capture(self)", entry_import="from sf_capture import capture",
             extra=None):
        files = {
            "stock_float_window": entry_import + "\nclass FloatWindow:\n"
                "    def _export_diagnostics(self):\n        return " + invocation + "\n",
            "sf_capture": helper,
            "sf_quote": "def read():\n    return {}\n",
        }
        files.update(extra or {})
        for name, body in files.items():
            (self.root / (name + ".py")).write_text(body, encoding="utf-8")
        return gate.curated_modules(self.root)

    def test_imported_aliased_callback_reaches_its_dependencies(self):
        helper = "import sf_quote\ndef capture(window):\n    return sf_quote.read()\n"
        self.assertIn("sf_quote", self.scan(helper, "dict(provider=prepare)",
                                          "from sf_capture import capture as prepare"))
        self.assertNotIn("sf_quote", self.scan("def capture(window):\n    return {}\n"))

    def test_unused_nested_function_is_not_evidence(self):
        self.assertNotIn("sf_quote", self.scan(
            "import sf_quote\ndef capture(window):\n"
            "    def unused():\n        return sf_quote.read()\n    return {}\n"))

    def test_nested_callback_reference_is_evidence(self):
        self.assertIn("sf_quote", self.scan(
            "import sf_quote\ndef capture(window):\n"
            "    def snapshot():\n        return sf_quote.read()\n    return dict(provider=snapshot)\n"))

    def test_parameter_shadows_module_function(self):
        self.assertNotIn("sf_quote", self.scan(
            "import sf_quote\ndef snapshot():\n    return sf_quote.read()\n"
            "def capture(window, snapshot=None):\n    return snapshot\n"))

    def test_assignment_shadows_module_function(self):
        self.assertNotIn("sf_quote", self.scan(
            "import sf_quote\ndef snapshot():\n    return sf_quote.read()\n"
            "def capture(window):\n    snapshot = None\n    return snapshot\n"))

    def test_assignment_shadows_imported_callback(self):
        self.assertNotIn("sf_quote", self.scan(
            "from sf_quote import read\ndef capture(window):\n    read = None\n    return read\n"))

    def test_local_worker_does_not_resolve_an_unrelated_nested_worker(self):
        self.assertNotIn("sf_quote", self.scan(
            "import sf_quote\ndef unrelated():\n"
            "    def worker():\n        return sf_quote.read()\n    return worker\n"
            "def capture(window):\n    worker = object()\n    return worker\n"))

    def test_import_from_an_unrelated_function_does_not_bind_a_name(self):
        self.assertNotIn("sf_quote", self.scan(
            "def unrelated():\n    import sf_quote\n"
            "def capture(window):\n    return sf_quote.read()\n"))

    def test_local_import_is_resolved_in_its_own_scope(self):
        self.assertIn("sf_quote", self.scan(
            "def capture(window):\n    from sf_quote import read as snapshot\n    return snapshot\n"))

    def test_module_attribute_callback_is_followed(self):
        self.assertIn("sf_quote", self.scan(
            "import sf_quote\ndef capture(window):\n    return sf_quote.read()\n",
            "dict(provider=helper.capture)", "import sf_capture as helper"))

    def test_reexported_callback_is_followed(self):
        self.assertIn("sf_quote", self.scan(
            "from sf_bridge import snapshot as capture\n", "dict(provider=capture)",
            extra={"sf_bridge": "import sf_quote\ndef snapshot(window):\n    return sf_quote.read()\n"}))

    def test_nested_free_variable_resolves_its_enclosing_import(self):
        self.assertIn("sf_quote", self.scan(
            "def capture(window):\n    from sf_quote import read\n"
            "    def snapshot():\n        return read()\n    return snapshot\n"))

    def test_global_declaration_resolves_the_module_binding(self):
        self.assertIn("sf_quote", self.scan(
            "from sf_quote import read\ndef capture(window):\n    global read\n    return read\n"))

    def test_lambda_parameter_does_not_resolve_a_shadowed_module_name(self):
        self.assertNotIn("sf_quote", self.scan(
            "from sf_quote import read\ndef capture(window):\n    return dict(provider=lambda read: read)\n"))

    def test_lambda_callback_resolves_outer_import(self):
        self.assertIn("sf_quote", self.scan(
            "from sf_quote import read\ndef capture(window):\n    return dict(provider=lambda: read())\n"))

    def test_unused_lambda_assignment_is_not_evidence(self):
        self.assertNotIn("sf_quote", self.scan(
            "import sf_quote\ndef capture(window):\n    unused = lambda: sf_quote.read()\n    return {}\n"))

    def test_recursive_callbacks_terminate_and_preserve_evidence(self):
        self.assertIn("sf_quote", self.scan(
            "import sf_quote\ndef capture(window):\n    return capture, sf_quote.read()\n"))

    def test_function_default_is_evaluated_even_if_the_body_is_unused(self):
        self.assertIn("sf_quote", self.scan(
            "import sf_quote\ndef capture(window):\n"
            "    def unused(value=sf_quote.read()):\n        return value\n    return {}\n"))

    def test_unrelated_class_method_is_not_a_window_callback(self):
        self.assertNotIn("sf_quote", self.scan(
            "import sf_quote\ndef capture(window):\n    return {}\n",
            "dict(provider=self.snapshot)", extra={"stock_float_window":
                "import sf_quote\nclass Other:\n    def snapshot(self):\n        return sf_quote.read()\n"
                "class FloatWindow:\n    def _export_diagnostics(self):\n        return dict(provider=self.snapshot)\n"}))

    def test_mixin_alias_and_bound_callback_are_followed(self):
        self.assertIn("sf_quote", self.scan("", extra={
            "stock_float_window": "from sf_capture import Mixin as RenderMixin\n"
                "class FloatWindow(RenderMixin):\n"
                "    def _export_diagnostics(self):\n        return dict(provider=self.snapshot)\n",
            "sf_capture": "import sf_quote\nclass Mixin:\n"
                "    def snapshot(self):\n        return sf_quote.read()\n"}))

    def test_current_override_does_not_reach_an_unused_base_method(self):
        self.assertNotIn("sf_quote", self.scan("", extra={
            "stock_float_window": "from sf_capture import Mixin\nclass FloatWindow(Mixin):\n"
                "    def snapshot(self):\n        return {}\n"
                "    def _export_diagnostics(self):\n        return self.snapshot()\n",
            "sf_capture": "import sf_quote\nclass Mixin:\n"
                "    def snapshot(self):\n        return sf_quote.read()\n"}))

    def test_update_snapshot_has_real_coverage_without_version_waivers(self):
        root = Path(__file__).resolve().parent.parent
        self.assertIn("sf_update_flow", gate.curated_modules(root))
        self.assertNotIn("sf_update_flow", gate.parse_declarations(root, gate.head_version(root))[0])
        # A future version must not inherit an earlier skip to become valid.
        result = gate.check(root, version="future-fixture", changed={"sf_update_flow"})
        self.assertTrue(result["ok"], result)
