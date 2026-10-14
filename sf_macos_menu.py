"""Native menu and keyboard actions for the otherwise custom-painted window."""
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QAction, QKeySequence
from PyQt6.QtWidgets import QApplication, QMenuBar


def install_window_actions(owner, native_menu=False):
    def top_button(name):
        if name in ("edit", "bell") and owner._is_btn_disabled(name):
            owner._show_btn_blocked_feedback(name)
            return
        owner._trigger_top_button(name)

    def edit(command):
        widget = QApplication.focusWidget()
        callback = getattr(widget, command, None)
        if callable(callback):
            callback()

    commands = (
        ("replace", "替换股票", "Ctrl+E", lambda: top_button("edit")),
        ("targets", "目标价", "Ctrl+T", lambda: top_button("bell")),
        ("refresh", "立即刷新", "Ctrl+R", owner._force_fetch),
        ("minimize", "收起到 Dock", "Ctrl+W", lambda: top_button("min")),
        ("restore", "显示窗口", "", owner._restore_window),
        ("update", "检查更新", "", lambda: owner._check_updates_background(manual=True)),
        ("diagnostics", "导出诊断包", "Ctrl+Shift+D", lambda: owner._export_diagnostics(background=True)),
        ("quit", "退出", "Ctrl+Q", owner.quit),
        ("cut", "剪切", QKeySequence.StandardKey.Cut, lambda: edit("cut")),
        ("copy", "拷贝", QKeySequence.StandardKey.Copy, lambda: edit("copy")),
        ("paste", "粘贴", QKeySequence.StandardKey.Paste, lambda: edit("paste")),
        ("select_all", "全选", QKeySequence.StandardKey.SelectAll, lambda: edit("selectAll")),
    )
    actions = {}
    for key, title, shortcut, callback in commands:
        action = QAction(title, owner)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        action.triggered.connect(callback)
        owner.addAction(action)
        actions[key] = action
    actions["quit"].setMenuRole(QAction.MenuRole.QuitRole)
    if not native_menu:
        return actions, None
    bar = QMenuBar(owner)
    bar.setNativeMenuBar(True)
    for title, keys in (("股票", ("replace", "targets", "refresh")),
                        ("编辑", ("cut", "copy", "paste", "select_all")),
                        ("窗口", ("minimize", "restore", "quit")),
                        ("帮助", ("update", "diagnostics"))):
        menu = bar.addMenu(title)
        for key in keys:
            menu.addAction(actions[key])  # type: ignore[union-attr]
    return actions, bar
