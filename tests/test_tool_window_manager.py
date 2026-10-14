from stock_float_window import ToolWindowManager
from unittest import main

try:
    from _base import SFCase
except ImportError:
    from tests._base import SFCase


class TestToolWindowManager(SFCase):
    def test_manager_owns_all_tool_window_references(self):
        manager = ToolWindowManager()
        editor = object()
        menu = object()
        panel = object()
        bubble = object()
        manager.editor = editor
        manager.context_menu = menu
        manager.target_panel = panel
        manager.target_bubble = bubble

        self.assertIs(manager.editor, editor)
        self.assertIs(manager.context_menu, menu)
        self.assertIs(manager.target_panel, panel)
        self.assertIs(manager.target_bubble, bubble)

    def test_clear_is_idempotent(self):
        manager = ToolWindowManager()
        manager.editor = object()
        manager.target_panel = object()
        manager.clear()
        manager.clear()

        self.assertIsNone(manager.editor)
        self.assertIsNone(manager.context_menu)
        self.assertIsNone(manager.target_panel)
        self.assertIsNone(manager.target_bubble)


if __name__ == "__main__":
    main()
