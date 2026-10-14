"""All custom-painted commands have system-visible action equivalents."""
from PyQt6.QtWidgets import QWidget
from sf_macos_menu import install_window_actions
from tests._base import SFCase


class Host(QWidget):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.blocked = False

    def _is_btn_disabled(self, name):
        return self.blocked

    def _show_btn_blocked_feedback(self, name):
        self.calls.append(("blocked", name))

    def _trigger_top_button(self, name):
        self.calls.append(("button", name))

    def _force_fetch(self):
        self.calls.append(("refresh",))

    def _restore_window(self):
        self.calls.append(("restore",))

    def _check_updates_background(self, manual=False):
        self.calls.append(("update", manual))

    def _export_diagnostics(self, background=False):
        self.calls.append(("diagnostics", background))

    def quit(self):
        self.calls.append(("quit",))


class MacActionsQualityTests(SFCase):
    @classmethod
    def setUpClass(cls):
        cls.app = cls.ensure_qapp()

    def test_actions_use_existing_commands_and_preserve_mutual_exclusion(self):
        host = Host()
        self.track_widget(host)
        actions, bar = install_window_actions(host)
        self.assertIsNone(bar)
        actions["replace"].trigger()
        host.blocked = True
        actions["targets"].trigger()
        actions["diagnostics"].trigger()
        self.assertEqual(host.calls, [("button", "edit"), ("blocked", "bell"), ("diagnostics", True)])
        self.assertFalse(actions["replace"].shortcut().isEmpty())

    def test_native_business_edit_window_help_menu_order(self):
        host = Host()
        self.track_widget(host)
        actions, bar = install_window_actions(host, native_menu=True)
        self.assertEqual([action.text() for action in bar.actions()], ["股票", "编辑", "窗口", "帮助"])
        self.assertEqual(actions["quit"].menuRole().name, "QuitRole")
