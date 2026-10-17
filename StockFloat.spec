# -*- mode: python ; coding: utf-8 -*-

# 20.3 v1.2.22：版本号单一来源 —— 从 sf_constants.APP_VERSION 读取，
# 打包版本与代码版本不再分叉维护（改版本只需改 sf_constants.py 一处）。
import re as _re
from importlib.metadata import distribution as _distribution
from pathlib import PurePosixPath as _PurePosixPath
with open('sf_constants.py', encoding='utf-8') as _f:
    _SRC = _f.read()


def _const(name, fallback):
    # 20.3 v1.2.22 起版本号从这里读；v1.4.64（全方位复核）把**包名与包标识**也纳进来：
    # 此前 name/bundle_identifier/CFBundleName 五处各写死一份 'Skyrocket' 字面量，
    # 而 AGENTS 声称"身份字段的唯一来源是 sf_constants" —— 对包名这句话并不成立，
    # 第三次改名要手改六处且没有任何判据兜住。取不到值时照旧退 0.0.0 那种"响亮失败"
    # 不够：身份取不到就直接抛，宁可打包失败也不要出一个身份错乱的包。
    m = _re.search(name + r"\s*=\s*['\"]([^'\"]+)['\"]", _SRC)
    if not m:
        raise SystemExit("StockFloat.spec 读不到 sf_constants.%s —— 拒绝打包" % name)
    return m.group(1)


APP_VERSION = _const('APP_VERSION', None)
APP_DISPLAY_NAME = _const('APP_DISPLAY_NAME', None)
BUNDLE_ID = _const('BUNDLE_ID', None)


def _keep_qt_translation(entry):
    path = _PurePosixPath(entry[0])
    # Keep both Chinese variants and English; leave other resource types untouched.
    return (path.parts[:3] != ('PyQt6', 'Qt6', 'translations')
            or path.suffix != '.qm'
            or path.name.endswith(('_zh_CN.qm', '_zh_TW.qm', '_en.qm')))


_license_data = [('NOTICE.md', 'licenses'), ('LICENSE', 'licenses'),
                 ('README.md', 'docs'), ('SECURITY.md', 'docs'),
                 ('docs/INSTALL.md', 'docs')]
for _package in ('PyQt6', 'PyQt6-Qt6', 'PyQt6-sip', 'PyNaCl', 'cffi',
                 'pyobjc-core', 'pyobjc-framework-Cocoa', 'pyobjc-framework-Quartz',
                 'pyobjc-framework-UserNotifications', 'pyobjc-framework-AVFoundation',
                 'pyobjc-framework-CoreMedia', 'pyobjc-framework-CoreAudio'):
    _dist = _distribution(_package)
    for _entry in _dist.files or ():
        if _entry.name.upper().startswith(('LICENSE', 'COPYING')):
            _license_data.append((str(_dist.locate_file(_entry)), 'licenses/' + _package))


a = Analysis(
    ['stock_float_window.py'],
    pathex=[],
    binaries=[],
    # v1.2.24：内置数字字体随包分发（解包后位于 _MEIPASS/fonts/）。
    # 本版起换成 Datto 官方 D-DIN（SIL OFL 1.1，许可原文同包），字体资源含 otf 与 txt，
    # 由 sf_ui_render.load_bundled_fonts 在启动时 addApplicationFont 注册，
    # 任意 Mac 数字等宽显示效果一致，不依赖目标机是否安装该字体。
    datas=[('fonts/*.otf', 'fonts'), ('fonts/*.txt', 'fonts'),
           ('updater/update_helper.sh', 'updater')] + _license_data,
    # v6：目标价功能惰性导入的模块必须显式声明（PyInstaller 静态分析看不到惰性 import）；
    # pyobjc-framework-UserNotifications 同理（sf_notify 的 UN 主路线）。
    # v1.4.23：sf_temp_sweep 由主窗后台线程里的 `import sf_temp_sweep` 惰性加载
    # （临时目录旧副本自愈清扫），同样显式登记，避免产物缺模块导致线程静默失败。
    hiddenimports=['targets', 'sf_calendar', 'sf_notify', 'sf_temp_sweep',
                   'sf_credentials', 'Security', 'sf_diagnostics', 'sf_camera_check',
                   'sf_http', 'sf_instance', 'sf_macos_menu', 'sf_privacy',
                   'sf_quote_delivery', 'sf_storage_tasks', 'sf_maintenance',
                   'sf_feedback', 'sf_feedback_outbox', 'sf_feedback_receiver',
                   'sf_feedback_analyzer',
                   # 诊断包自动采集层：导出那一下才在嵌套函数里 import（sf_diag.collect），
                   # 与上面同一族处置 —— 缺它装机版导出诊断时只会留一行
                   # "state.json: No module named 'sf_diag'"，功能静默缺一整份文件。
                   'sf_diag',
                   'UserNotifications', 'pyobjc-framework-UserNotifications',
                   # v1.4.40 人脸在场守卫：sf_presence 在函数里惰性 import AVFoundation
                   # （只为拿到 block 方法元数据），静态分析看不到；缺它编译期不报错，
                   # 装机版第一次开摄像头就弹不出授权框。CoreMedia/CoreAudio 是它的依赖。
                   'AVFoundation', 'CoreMedia', 'CoreAudio',
                   # OTA 校验模块由右键菜单的后台线程惰性导入，静态分析不可见；
                   # PyNaCl 的 Ed25519 验签还要加载 CFFI 原生桥接。缺任一个都会让
                   # 已安装程序在“检查更新”时中断，无法读取线上清单。
                   'sf_update', 'sf_system_alert', 'AppKit', 'nacl', 'nacl.exceptions', 'nacl.signing',
                   'nacl.bindings', 'nacl._sodium', '_cffi_backend'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 14.1 打包瘦身：排除本应用未使用的 Qt 模块（QtSvg 被主窗口/绘制引用，必须保留）
    excludes=['PyQt6.QtPdf', 'PyQt6.QtPdfWidgets', 'PyQt6.QtDBus', 'PyQt6.QtOpenGL', 'PyQt6.QtNetwork'],
    noarchive=False,
    optimize=0,
)
# 14.1 打包瘦身：hook 会将整个 PyQt6/Qt6/lib 全量收集，这里过滤掉本应用
# 不使用的孤立 Qt 库（QtPdf/QtNetwork 不被 QtCore/QtGui/QtWidgets/QtSvg 链接；
# QtDBus 被 QtGui.framework 运行时依赖，必须保留）。
# 同时剔除引用 QtPdf/QtNetwork 的两个插件（PDF 图片格式插件 / 触摸 UI 插件）。
a.binaries = [
    t for t in a.binaries
    if ('QtPdf' not in t[0]) and ('QtNetwork' not in t[0])
    and ('libqpdf' not in t[0]) and ('uiotouch' not in t[0])
]
# 同步过滤 framework 骨架；Qt 翻译只保留简体中文、繁体中文与英文。
a.datas = [
    t for t in a.datas
    if ('QtPdf' not in t[0]) and ('QtNetwork' not in t[0])
    and _keep_qt_translation(t)
]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_DISPLAY_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icon.icns'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name=APP_DISPLAY_NAME,
)
app = BUNDLE(
    coll,
    name=APP_DISPLAY_NAME + '.app',
    icon='icon.icns',
    bundle_identifier=BUNDLE_ID,
    info_plist={
        'CFBundleName': APP_DISPLAY_NAME,
        'CFBundleDisplayName': APP_DISPLAY_NAME,
        'CFBundleShortVersionString': APP_VERSION,
        'CFBundleVersion': APP_VERSION,
        'CFBundleInfoDictionaryVersion': '6.0',
        # Qt 6.11 binaries require macOS 13; the release gate verifies every arm64 image.
        'LSMinimumSystemVersion': '13.0',
        'LSArchitecturePriority': ['arm64'],
        'NSHighResolutionCapable': True,
        # v1.4.40 人脸在场守卫：缺这个键，程序一旦伸手要摄像头就会被系统当场终止
        # （NSCameraUsageDescription 是 TCC 授权框的正文，不是可选项）。
        # 文案按"用来做什么 + 数据去哪了"写：用户有权在点「允许」之前知道
        # 帧不会离开这台机器，而不是只看到一句"需要摄像头权限"。
        'NSCameraUsageDescription': (
            '检测画面里是否出现第二张脸，出现时自动把行情窗口收起到 Dock。'
            '视频帧只在本机内存中转瞬处理，不保存、不上传、不做人脸识别。'),
    },
)
