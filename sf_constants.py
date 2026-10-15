# -*- coding: utf-8 -*-
"""模块化拆分 v1.2.13：sf_constants —— 由 stock_float_window.py 搬移生成，行为零变化。"""

import os
import sys
import tempfile
from PyQt6.QtGui import QColor

# ---------------------------------------------------------------- 测试隔离
# v1.3.52：只要检测到"正在跑 unittest"，就把所有**落盘路径**自动重定向到临时目录。
# 为什么必须做成自动的：v1.3.50 只在 run_tests.sh 里设了环境变量，工程师直接敲
# `python -m unittest tests.test_x` 就绕过去了 —— 结果一次单测又把用户真实配置的
# bg 写成 translucent（毛玻璃消失），而症状看起来像渲染问题，排查成本极高。
# 这里在**常量层**兜底：任何调用方式都躲不掉。
# 注意用 setdefault：run_tests.sh 显式设的值优先，不被覆盖。
# 冻结产物（sys.frozen）一律不走这条 —— 打包后绝不能把用户配置挪到临时目录。
if "unittest" in sys.modules and not getattr(sys, "frozen", False):
    _SF_TMP = tempfile.gettempdir()
    os.environ.setdefault("SF_CONFIG_PATH",
                          os.path.join(_SF_TMP, "sf_config_test.json"))
    os.environ.setdefault("SF_LAST_QUOTE_PATH",
                          os.path.join(_SF_TMP, "sf_last_quote_test.json"))
    os.environ.setdefault("SF_SEED_FIRST_SCREEN", "0")
    # v1.3.66：本地股票字典同样落盘，测试期一并隔离（字典会污染打桩用例的结果）
    os.environ.setdefault("SF_UNIVERSE_PATH",
                          os.path.join(_SF_TMP, "sf_securities_test.json"))
    os.environ.setdefault("SF_UNIVERSE_DISABLE", "1")
    # v1.3.67：日志与单实例 socket 同样落盘，此前遗漏 —— 实测一次全量回归往
    # 用户真实 debug.log 灌了 1.3 万条 mock 失败（"RuntimeError('down')" 这类
    # 只有测试才有的假异常），真机日志从此不再可用于诊断数据源健康度。
    # socket 更危险：测试会去连/绑真机的单实例锁，与正在运行的悬浮窗抢实例。
    os.environ.setdefault("SF_LOG_PATH",
                          os.path.join(_SF_TMP, "sf_debug_test.log"))
    os.environ.setdefault("SF_SINGLE_SOCK",
                          os.path.join(_SF_TMP, "sf_single_test.sock"))

QUOTE_URL = 'https://qt.gtimg.cn/q=%s'
# The old minute host returns Tencent WAF HTTP 501; this Tencent route has the same schema.
MINUTE_URL = 'https://proxy.finance.qq.com/ifzqgtimg/appstock/app/minute/query?code=%s'
SEARCH_URL = 'https://smartbox.gtimg.cn/s3/?v=2&q=%s&t=all'
EASTMONEY_SEARCH_URL = 'https://searchapi.eastmoney.com/api/suggest/get?input=%s&type=14&token=%s&count=%s'
EASTMONEY_TOKEN_DEFAULT = 'D43BF722C8E33BDC906FB84D85E326E8'
APP_VERSION = '1.4.91'
# v1.3.52：测试隔离改为**自动生效**（不再只靠 run_tests.sh 设环境变量）
#   实测：工程师直接敲 `python -m unittest tests.test_xxx` 会绕过 run_tests.sh 的
#   隔离，一次单测又把用户真实配置的 bg 写成 translucent（毛玻璃消失）。
#   现改为在常量层检测 "unittest" in sys.modules（且非 sys.frozen）即自动把
#   config.json / last_quote.json 重定向到临时目录，任何调用方式都躲不掉。
# v1.3.51：三项内部技术债清理（纯性能/健壮性，界面语义不变）
#   1. 分时均价线每帧重建 QPainterPath（240 次 moveTo/lineTo）→ 加路径缓存，
#      实测省 0.226ms/帧（-64%）；折线缓存键同步补 lo/hi（昨收变了也要重算）
#   2. 图带过矮时整幅分时图不画（新增 CHART_MIN_DRAW_H）—— 消除展开动画
#      头几帧那条 1~2px 被压扁的怪线；高度常量一律未改
#   3. 8 个"看似无用"的再导出符号固化为死代码扫描器白名单 + 守护用例，
#      不再靠归档脚本碰巧兜底；顺带修 _seed_first_screen 未失效折线缓存
#      （切股后可能残留上一只股票的图）
# v1.3.50：测试隔离 —— 回归测试不得再写用户真实配置
#   CONFIG_PATH 支持 SF_CONFIG_PATH 环境变量覆盖，run_tests.sh 已重定向到临时目录。
#   实测：一次全量回归就把线上 bg 从 glass 写成 translucent（毛玻璃消失），
#   并且误导排障方向。不设该变量时行为完全不变。
# v1.3.49：三处"数字骗人/取不到数"的缺陷修复
#   1. 主源缺涨跌幅字段时整条行情被丢弃 → 改为宽容解析：优先用 pct，
#      缺失则用昨收自算，都算不出标记为「未知」显示 "--"（价格照常显示）
#   2. 新浪备源昨收为 0 时涨跌幅恒算成 0.00% → 改为置空串（未知）并记 WARNING，
#      不再用假数字冒充平盘
#   3. _secid("bj8307") 未补足 6 位 → 东财备源恒取空；与 _tencent_code 同口径右侧补 0
# v1.3.48：配置并发保存撞名 → 配置被写坏（线上毛玻璃消失的根因）
#   补一条保存侧告警：写入非默认背景模式时留痕，定位是哪个实例写进去的
#   1. _atomic_write_json 临时文件名改为「pid + 纳秒时间戳」唯一化 ——
#      原固定名在并发保存时会互相截断/rename 失败，实测 120 次并发失败 117 次
#   2. load_config 读到非默认背景模式时留 WARNING 日志（界面上无切换入口，
#      出现非默认值即说明配置被异常写入过，便于下次排查）
# v1.3.46：全项目瘦身重构（纯清理，零行为变化）
#   1. renderer.py：两处逐字相同的超椭圆 arc 闭包收敛为唯一实现
#   2. 死代码：删死绑定（elided / pts / btn_data / app）、孤儿函数 const_strings、
#      重复赋值 is_up；死常量 CHART_FILL_ALPHA / ICON_PENCIL_WIDTH 接线到绘制点
#   3. 全项目补中文注释、收敛多余空行；死代码扫描器固化为 tools/deadcode_scan.py
# v1.3.45：冷启动首帧稳定性 + 启动提速
#   1. 主窗几何「先落最终尺寸、再上 min/max 约束」—— 消除 184×420 钳制中间态
#   2. 首屏数据快照 last_quote.json —— 消除「占位符 → 真实数据」的内容跳变
#   3. 字体度量预热前移到 show() 之前 —— 首帧不再"先画一版再重排"
#   4. pyobjc / 崩溃报告检查挪出启动关键路径
VERSION_TEXT = 'v' + APP_VERSION
# 发版阶段标签（单一来源，唯一真相）：'beta' | 'stable'。
# 长期策略：每次改动照旧 OTA 发布到唯一 feed（所有装机用户照常自动更新，不分流），
# 新版本默认按 Beta 标记；只有经用户口头确认，才把该版本的 RELEASE_STAGE 改成 'stable'。
# 这是"标签/口径"，不是更新通道开关 —— channel 仍是 stable，客户端照常收更新。
# v1.4.27（2026-09-23）：用户口头确认"将此版本标记为稳定"，自 v1.4.18 之后首次转正。
# v1.4.62（2026-10-12）：同法转正（用户在 iMac 实机一键安装成功之后确认）。**下一版开工
# 时必须翻回 'beta'** —— v1.4.38 那次就是忘了翻，把新版本当稳定版发了出去。
# 注意：本值会编进产物（右键菜单"版本 v…"行、导出诊断包里的版本标签），改它必须
# 重新打包，否则线上清单说是稳定、用户机器上的菜单仍显示 Beta。
# v1.4.63（2026-10-12）：按上面那句执行，已翻回 'beta'。稳定基线仍是 v1.4.62。
# v1.4.64（2026-10-12）：用户明确指定"本轮复核修复版直接标稳定"，那一版是 stable（不是漏改，
#   别当惯例去纠正它）。当前稳定基线 = v1.4.64。
# v1.4.65（2026-10-12）：分时恢复三处修复，按默认口径回到 'beta'。
# v1.4.67（2026-10-12）：菜单首点即反馈 + 卷内一键安装隐藏 .command 后缀，默认 'beta'。
# v1.4.68（2026-10-13）：复核批六件（含两处用户可见的"点了没反应"），默认 'beta'。
#   稳定基线仍是 v1.4.64 —— 上面那句"下一版开工时必须翻回 'beta'"这轮照旧执行。
RELEASE_STAGE = 'beta'
# Beta 后缀的唯一来源（2026-10-12 收口）。此前"加什么字"这条规则写在两处
# （这里与 stock_float_window 的右键菜单版本行），改一处会留下另一处继续说 Beta。
# 收成常量的另一个理由：产物层查得到名字，而裸字面量在 co_names 里是隐形的
# （发版链 3.6 那道特征闸门只能查符号，查不了字符串）。
BETA_SUFFIX = ' Beta'
# 面向用户显示的版本标签：Beta 版带 " Beta" 后缀，稳定版为裸版本号。
VERSION_LABEL = VERSION_TEXT + (BETA_SUFFIX if RELEASE_STAGE == 'beta' else '')
HTTP_TIMEOUT = 4
# 4.2 v1.2.27：HTTP 重试次数 1→2（单请求最多 3 次尝试），配合 sf_network 每次
# 失败 sleep(0.5) 退避，提升弱网/瞬时抖动的成功率；重试总耗时上限≈超时×3+1s，
# 仍受 4s 超时与轮询熔断状态机保护，不破坏弱网保护与省电策略。
HTTP_RETRIES = 2
# 分时（分钟走势图）这一路的网络预算单独收紧，不复用上面那两个默认值。
# 为什么必须单独定：分时是**主源→备用源串行**，一次 fetch_minute 的失败代价是
# 2 × (HTTP_TIMEOUT×(HTTP_RETRIES+1) + 0.5×HTTP_RETRIES + 兜底 urllib 一次)
# = 本机实测 **34.3 秒**。而分时线程正是"数据暂缺 · 重试中"唯一的那条恢复路径，
# 一次尝试卡 34 秒 = 用户至少多盯 34 秒的降级提示。
# 阈值来源（2026-10-12 盘中本机实测，各 20 次 + 冷连接 8 次，直连两个真实源站）：
#   主源 web.ifzq.gtimg.cn    p50 44ms / p90 63ms / max 82ms；冷启动含 TLS 握手 max 114ms
#   备源 push2his.eastmoney   p50 35ms / p90 65ms / max 99ms；冷启动 max 94ms
#   整链 fetch_minute 健康态  p50 46ms / max 194ms
# 取 2.0s ≈ 健康态最坏值的 10 倍，够挡住"慢但活着"，又把一次尝试的上限从 34.3s 压到
# 2×(2s+2s)=8s（pooled 一次 + http_get 自带的 urllib 兜底一次）。
MINUTE_HTTP_TIMEOUT = 2.0
# retries=0 不会丢掉"连接被服务端悄悄关掉"那次重建：sf_network.http_get 在连接池
# 路径抛异常后**本来就会**换 urllib 重发一次（fb_retries=0 if pool_attempted），
# 所以每个源实际仍有两次机会，只是第二次不再额外退避 0.5s。
MINUTE_HTTP_RETRIES = 0
# v1.4.37：窗口位移是否交给 macOS 系统拖拽（默认关，理由见下面字典里那段）。
# 单独命名成一个顶层布尔，而不是直接写字面量，是为了让**发版核验能读到产物里的
# 实际值**：`tools/verify_bundle_consts.py <app> sf_constants SYSTEM_DRAG_DEFAULT`
# 只对 `NAME = <字面量>` 给得出值，写在字典字面量里的值会被报成"推导值"——
# 那等于这一轮最关键的一处改动在产物上无法核验（本轮改的就是这个值）。
SYSTEM_DRAG_DEFAULT = False
_FEATURE_DEFAULTS = {'private_spaces': True, 'conn_pool': True, 'net_monitor': True, 'poll_backoff': True,
                   'anim_params': True, 'toggle_chart': True, 'drop_flash': True,
                   'targets': True,
                   # v1.4.40 人脸守卫：**这是 kill switch，不是"功能开没开"**。
                   # True 只表示"这条代码路径存在、可以在界面上被用户打开"；
                   # 用户实际有没有开，看 config.json 的 face_guard 字段（默认关，
                   # 因为要摄像头权限，绝不能默认替用户打开）。
                   # 留着这个开关的意义：真机上如果发现取帧/授权有未预期问题，
                   # features.ini 写 face_guard=0 就能把菜单项和采集一并撤掉。
                   'face_guard': True,
                   # v1.4.37：默认**关回**自绘拖拽。原因不是"系统拖拽不跟手"，而是它
                   # 与独立毛玻璃背景层原理上不可兼得：位移交给 WindowServer 后，内容窗
                   # 当帧就被挪走，背景层要等本进程问一次真实位置、再写一次自己的位置，
                   # **永远晚一帧**。掉队量 = 一帧 × 拖动速度，装机版 v1.4.36 真鼠标逐帧
                   # 实测：常速 550px/s → 10px（看不见）；快速 3000px/s → **55px**；
                   # 甩动 7300px/s → **88px**（卡片才 184px 宽，接近半张卡），
                   # 用户看到的就是"主界面元素与背景层分开"的重影。
                   # 同一批手势关掉本开关后实测 **0px**（两层在同一次提交里一起动），
                   # 所以老板 2026-09-24 拍板：宁可回到偶发一帧的停顿感，也不留重影。
                   # 两条已验证走不通的路，别再试：① 用拖拽期间的鼠标事件驱动背景层 ——
                   # 系统拖拽的模态循环里 Qt 几乎收不到 mouseMove（1.5s 只有 4 次）；
                   # ② 光标增量插值/速度前馈 —— v1.4.32 实测 p95 无改善（本文件下方注释
                   # 与 project-input-latency-probe 都有记录），且急停会先冲过头再弹回。
                   # 想要系统拖拽：features.ini 写 system_drag=1，或
                   # STOCKFLOAT_FEATURES=system_drag=1（代价就是上面那组重影数字）。
                   'system_drag': SYSTEM_DRAG_DEFAULT}
# 「目标价」功能：方向常量（targets.py / sf_config.py 共用，单一来源）
TARGET_DIR_ABOVE = "above"   # 涨到目标价位
TARGET_DIR_BELOW = "below"   # 跌到目标价位
# v1.3.43 起按老板要求提速：端到端延迟构成（交易时段）为
#   等下一次拉取(0~100ms) + 请求往返(12ms) + 等 UI 取队列(0~16ms) + 数字滚动
# v1.3.44 手感回调：v1.3.43 的 80/130ms 实测偏急，老板要求调回
#   常规 100ms / 大幅 140ms —— 仍明显快于 v1.3.42 的 130/200ms，但不再跳。
ANIM_TINY_PRICE = 0.01
ANIM_NORMAL_MS = 100
ANIM_LARGE_MS = 140
ANIM_LARGE_PCT = 3
POLL_NORMAL = 0
POLL_DEGRADED = 1
POLL_OPEN = 2
# 熔断阈值按「连续失败累计次数」判定，且**跨状态累计**：累计 3 次进 DEGRADED、
# 累计 5 次进 OPEN，不是进 DEGRADED 之后再重新数 5 次。
POLL_FAIL_TO_DEGRADED = 3
POLL_FAIL_TO_OPEN = 5
POLL_INTERVAL_NORMAL = 0.1
POLL_INTERVAL_DEGRADED = 1
POLL_INTERVAL_OPEN = 5
POLL_INTERVAL_MINIMIZED = 2.0   # v1.2.95：缩小到 Dock 的后台刷新间隔（秒）。原 5.0 偏慢，目标价通知易滞后；提速到 2.0 兼顾灵敏与省电
# v1.4.10 功耗：UI 定时器从"每 16ms 主动去排队列"退成**兜底看门狗**。
# 数据交付改由跨线程 Qt 信号驱动（FloatWindow.ui_wakeup）：入队即唤醒，
# 不再需要定时器高频轮询。实测原 16ms 定时器每秒醒 62.3 次，其中约 84%
# 队列是空的；现在唤醒次数与数据条数同级（≈10/秒），端到端等待窗口反而
# 从 0~16ms 压到 0~2ms。本定时器只保留两件事：① 万一某次信号没送达，
# 最迟 1 秒把积压数据全部消化；② 按秒复核交易时段/App Nap 状态。
UI_WATCHDOG_INTERVAL_MS = 1000
# ---- v1.4.40 人脸在场守卫（默认关，需用户在右键菜单里主动开启并授权摄像头）----
# 数值来源与生效规则显示在诊断包 status_text 与日志里（v1.4.51/52 起界面不再重复展示）：
#   · 取样节拍现为 0.25s（v1.4.54 由 1.0s 减半到 0.5s，2026-10-12 再减半，用户指定）。
#     Vision 单帧实测 12.1~25ms（640x480 两人照），节拍减半的代价是占空比约 4% → 8%。
#   · 连续 2 次命中才收窗：单人通过、屏幕里的人像、视频里的脸都会被检出，
#     单次命中就收窗会让人反复看到"行情自己消失"，几次之后用户直接关掉这个功能。
#     2026-10-12 起这"两次"是**两次独立复检**：主窗按样本时刻去重，同一份快照不再
#     被喂两次（此前 250ms 轮询快于采样，第二击读的常是同一份快照，等于没复检）。
#   · 连续 5 次只剩一张脸才恢复：恢复比收起要更保守 —— 侧过头、光一下暗
#     都会让主人自己被漏检，漏检时把行情亮出来正好把"保护"变成"暴露"。
# v1.4.56 事件驱动加急复检：计数**发生变化**时不等满一个节拍就再数一次。
# ⚠ 这一条的旧说法"把收起延迟从 ~0.75 秒压到 ~0.4 秒"是**错的**，2026-10-12 实测更正：
#   在"主线程轮询判定"下开关加急，收窗中位都是 0.77 秒（一模一样）。原因是第二击
#   由 250ms 轮询给出、不需要第二次真的数脸。只有判定也改成事件驱动（本轮做了），
#   加急才真的把第二次复检提前到 ~0.08 秒后 —— 它买的是"变化后尽快有第二个样本"，
#   不是"看起来更快"。预算上限防的是「两个人来回走动」把占空比顶满：每秒最多 3 次。
FACE_GUARD_EXPEDITE_SEC = 0.08
FACE_GUARD_EXPEDITE_BUDGET = 3
# v1.4.56 省电：屏幕锁定或主显示器休眠时，屏幕上没有任何可暴露的内容，
# 摄像头可以先停；唤醒后自动继续看守。探测节流 2 秒一次（CoreGraphics 查询
# 本身很便宜，但守卫那张表是采样的二分之一一拍（现 125ms），没必要每拍都问系统）。
# ⚠ 这条只在「明确读到锁了/睡了」时才停；查询抛错或读不到键一律当作没锁 ——
#   保护静默消失比多耗电严重得多（与 features.ini 的 fail-open 同一个方向）。
FACE_GUARD_SCREEN_PROBE_SEC = 2.0
# v1.4.57 授权弹窗可以重问的冷却窗口（修"点了没反应"）。
# 实测依据（本机 debug.log，16:36:34 那一次）：第一下点击真的请求了权限
# （requested），但系统弹窗没被作答，此后四下点击的间隔是 5.4 / 6.6 / 2.9 / 2.4 秒，
# 而旧实现是**进程级永久布尔** —— 于是这五下里后四下全是空操作，菜单勾号也不会动，
# 用户看到的就是"自动隐藏窗口点了没反应"，唯一出路是重启程序。
# 取 3 秒的判据：小于它的连点（手快双击）仍然只弹一次框，守住 v1.4.45 那条
# "一次点击不许叠三个框"的原意；隔了几秒再点就是用户在明确表达"我要开"，
# 这时必须把系统框再叫出来，而不是继续装死。
FACE_GUARD_REASK_COOLDOWN_SEC = 3.0
# 采样节拍：0.5s → **0.25s**（用户 2026-10-12 指定"把采样压到 0.25 秒 + 判定改事件驱动"
# 那一格）。代价算得很清楚：单帧 Vision 实测 20~25ms，节拍减半 = 每秒 2 次 → 4 次，
# 占空比从约 4% 涨到约 8%（v1.4.54 那套省电两件件套不动：640×480 + 15fps 仍然给得出
# 每 0.25 秒 3~4 帧可挑）。换来的是"数到新快照立刻判"，收窗延迟不再等轮询对齐。
# ⚠ 真正的下限是下面的 FACE_GUARD_DWELL_SEC：驻留 0.38 秒摆在那儿，收窗就到不了 0.24 秒。
#   2026-10-12 用本轮真实代码量（真 _on_frame + 真迟滞 + 真去重，帧按 15fps 喂，16 次）：
#   驻留 0.38s → 收窗中位 **0.57 秒**（0.44~0.63）；把驻留关掉同一套管线是 **0.30 秒**。
#   对照改动前（0.5s 采样 + 轮询判定 + 无驻留）是 0.74 秒。三个数都记，别只报好看的。
FACE_GUARD_TICK_SEC = 0.25
FACE_GUARD_HIDE_HITS = 2
FACE_GUARD_RESTORE_HITS = 5
# v1.4.54 相机功耗两件套（用户问"一直开着摄像头会不会很耗电"，先实测再动）：
#   · 采集分辨率压到 640x480。实测依据：两人照在 480x360 下置信度仍 0.82/0.81、
#     640x480 下 0.82/0.81，与 1024x768 的 0.82/0.81 完全持平 —— Vision 内部自己
#     会做缩放，分辨率不是检测质量的瓶颈；而像素吞吐从 1080p(High) 降约 4.4 倍。
#   · 传感器帧率上限 15fps。实测依据：本机相机只支持 15.00~30.00fps，1/10s 会被
#     系统当场拒绝（NSInvalidArgumentException，实测）—— 15 是支持区间的下限，
#     把传感器/ISP 的帧率从默认 30 砍半。检测节拍 0.25s（2026-10-12 起），
#     每拍仍有 3~4 帧可挑；再往下压就要动 15fps 这条省电底线，不动。
#   两条都是 fail-open：设备不支持就记 WARNING 照旧用默认值，绝不能因为省电把
#     会话弄挂（那就是"保护没了"，本末倒置）。
FACE_GUARD_CAMERA_FPS = 15
FACE_GUARD_CAMERA_PRESET = "AVCaptureSessionPreset640x480"
# 菜单项的**唯一**文案来源。为什么提成常量（v1.4.50）：状态行与 monitor 的
# `last_error` 都要指着这一项告诉用户"点哪里"，而它们各自硬写了一遍名字，
# 其中一处写的是「人脸在场守卫」—— 屏幕上从来没有这个菜单项（真实文案是
# 真实文案见本常量）。用户照着提示去找一个不存在的项，"未授权"就成了死路。
# v1.4.53 文案改为「自动隐藏窗口」（用户看图要求）：名字只说"会发生什么"，
# 不再假设用户知道触发条件 —— 触发条件本来就不在界面上讲了（v1.4.51/52 撤掉
# 菜单灰字与说明框），留在诊断包 status_text 与日志里。
FACE_GUARD_MENU_TEXT = "自动隐藏窗口"
# 置信度下限：Vision 给的 confidence，实测清晰正脸 0.82；远处/边缘的假框通常低于此。
FACE_GUARD_CONF_MIN = 0.6
# ---- 贴边的脸单独放宽（用户 2026-10-12 拍板：「被切一半的边脸也要算第二个人」）----
# 为什么 0.6 这一刀对贴在画面边界的人是吃亏的：检测框被画面边界切短之后，系统给的
# 把握分会**系统性下降**，同一个人在画面中间和压在边界上不是一个分数。
# 实测（2026-10-12，真 Vision + 仓库那张两人照，640×480 = 装机采集档，
# 复现入口 `tools/verify_face_presence.py --edge`）：
#   右脸被画面右边界切掉 0% / 30% / 40%  → 0.82 / 0.79 / 0.68，正常光与暗光都稳定算两张
#   切 50%（脸中心正好压在边界上）      → 正常光 0.638（只剩 0.038 的余量）
#                                         噪点 σ=10 时 0.587、σ=25 时 0.590 → 掉线不收窗
#   切 ≥60%                            → 系统**根本不返回这张脸**（一张框都没有），
#                                         任何阈值都救不了；这条只能写进对外说明
# 取 0.55 的依据：比实测最差那档（0.587）再留 0.037 的余量，同时仍高于"抛硬币"的 0.5。
# 误报侧也量过（同一次实验，入口 `--self-test` 里种错必红）：仓库里七张无脸素材
# （界面截图、图标、字体对比图）、纯灰/渐变/色块/类文字条纹、以及 σ=20~120 的噪点场
# —— 把握分 ≥0.5 的框 **0 个**。所以放宽的是"被边界切短的真脸"，不是"任何模糊的东西"。
# 仍然存在的代价，如实记在这里：海报或屏幕里**被切一半的人像**从此也算第二个人，
# 这一条机制上无法与真人区分（照片里的脸一直被当成真脸，见对外说明第 3 条）。
FACE_GUARD_EDGE_CONF_MIN = 0.55
# 检测框的任何一条边离画面边缘不到这个距离（归一化，占画面宽/高的比例）就算"贴边"。
# 取 0.02 是因为边界处的框是被**裁到 1.0** 的（实测切 50% 那张框的右缘正好等于 1.0），
# 留 2% 是给低分辨率下的取整，不是为了把画面中间的脸也拉进来放宽。
FACE_GUARD_EDGE_TOUCH = 0.02
# ---- 距离闸门：第二个人要站在 1.5 米内才算数（用户 2026-10-12 拍板）----
# 摄像头看不出"米"，只能看"这张脸占画面宽度几成"。换算要除以一个镜头水平视角：
#   占比 ≈ 脸宽 / (2 × 距离 × tan(视角/2))，脸宽取成年人 0.15 米
#     视角 60° → 1.5 米处占 8.7%；视角 70° → 7.1%；视角 75° → 6.5%
#   ⚠ 这台机器的真实视角**读不到**（实测 `activeFormat.videoFieldOfView` 返回 0.0，
#     CoreMedia 也没导出取视角的函数），所以米数只能给区间，不能给准数。
# 取 0.065 = 按最宽那档视角算的 1.5 米，方向是「宁松不紧」：
#   在窄视角（60°）的机器上它相当于 2.0 米，会把 1.5~2.0 米的人也算进来（多收一次窗，
#   烦但不危险）；反过来取 0.087 就会把 1.5~2.0 米内真想看清屏幕的人漏掉。
# 与把握分不打架：实测占 6.2%（40px）那张脸仍有 0.78~0.83 的把握分，远在 0.6 之上；
# 而系统本身在 3~4%（20~26px）就**根本不返回框**了 —— 所以这条闸门的真实作用区间是
# "1.5~2.5 米这一段本来勉强能检到的人，从此明确不算"，不是把功能收紧到看不见。
# 复现入口：`tools/verify_face_presence.py --near`（一近一远的合成帧逐级推远）。
# 想把这个数换成真正的米，需要一次实机标定：量好坐姿距离，读诊断包 face_guard.json 里
# 的 `max_face_width`（画面里最大那张达标脸占画面宽的比例），反解视角后按上式重算。
FACE_GUARD_NEAR_FACE_W = 0.065
# 贴边的框用"打折后的尺寸闸门"，不是完全免检（2026-10-12 二次复核 F2 的修法）。
# 为什么必须打折：脸被画面边界切掉时**框的宽和高一起缩**，切一半只剩 0.61 倍
# （实测 233px → 144px），拿原始尺寸要求它，就会把"站在旁边但半张脸出画的人"
# 当成很远的人滤掉 —— 那正好推翻用户另一条拍板「被切一半的边脸也要算第二个人」。
# 所以 0.61 这个系数是"R1 与 R2 同时成立"的唯一自洽取值：它等于实测最坏档（切 50%）
# 的缩水比例，再小就救不回 R1，再大就等于免检。
# 残差如实写在这里：在**窄视角**（≈60°）的机器上，3 米外且正好被切一半的人
# 折算后仍可能过这道弱闸门（4.3% vs 下限 4.0%）。这是两条口径同时成立时的必然残差，
# 不是漏改；要彻底关掉只能牺牲 R1（贴边的不算）或做实机标定。
FACE_GUARD_EDGE_SHRINK = 0.61
# ---- 驻留时长：第二个人必须在半径内待够这么久才收窗（用户 2026-10-12 原话）----
# 原话是「第二人物的人脸需停留在该半径内的时长＞0.38 秒才会自动隐藏窗口，
# 恢复窗口同理反推」。落地成两条对称的闸门：
#   · 收窗：从**第一次真的数到两张近脸**那一刻起算，持续 >0.38 秒才动手；
#   · 恢复：从**第一次只剩一张近脸**起算，同样要 >0.38 秒 —— 这条与既有的
#     "连续 5 次"叠加取更严的一侧（5 次 × 0.25 秒 = 1.25 秒 > 0.38 秒），
#     所以恢复的实际节奏没变：漏检主人时把行情亮出来，比多等一秒危险得多。
# ⚠ 这条本身就构成延迟下限：实测中位 0.57 秒（关掉驻留同一套管线是 0.30 秒）。
#   要更快只能动驻留或动采样节拍，不是动代码技巧。三个数都写在 FACE_GUARD_TICK_SEC 旁边。
FACE_GUARD_DWELL_SEC = 0.38

# 自动收起后最多再"看守"10 分钟。超时即停摄像头（绿灯灭、不再耗电），
# 此后只能由用户点 Dock 图标手动恢复 —— 目的是给"忘了自己在等自动恢复"封顶。
FACE_GUARD_MAX_IDLE_SEC = 600.0
# v1.4.45 零帧判定：会话 startRunning 之后这么久还**一帧都没来过**，就直接判中断。
# 为什么和 60 秒分开：60 秒是给"来过帧、后来停了"的会话的宽容期（偶发卡顿不该立刻
# 掀桌）；而"从来没来过帧"基本只有一个解释 —— 设备被别的程序占用或通道坏了，
# 拖满 60 秒只是让绿灯白亮一分钟、期间第二个人入镜也不会收窗（复核 A2）。
# 10 秒 = 冷启动首帧实测 76ms 的两个数量级余量，再短就开始误伤慢设备了。
FACE_GUARD_FIRST_FRAME_SEC = 10.0
# v1.4.41 复核 F2：连续 60 秒一帧都没来（多半是摄像头被别的程序抢走）就判定
# "保护已中断"，停摄像头并如实显示，而不是继续挂着"运行中"骗人。
# 为什么是 60 秒而不是更短：Zoom/会议软件接管镜头常有几秒的重连抖动，太短会在
# 人家只是切个窗的时候就把守卫拆掉；60 秒足够区分"抖动"和"真被占用"。
FACE_GUARD_STALE_SEC = 60.0
# 判定中断之后每隔 30 秒重试一次拉起，别的程序松开镜头就能自愈；
# 不设这个退避就会变成"每半秒抢一次摄像头"，两边都卡。
FACE_GUARD_RETRY_STALLED_SEC = 30.0
# v1.2.23：非交易时段（收盘后/周末/节假日）行情轮询间隔 60 分钟。
# 仅保留能显示最新收盘价的最低限度刷新；盘中仍走上方熔断状态机，不受影响。
POLL_INTERVAL_OFF_MARKET = 3600.0
# v1.4.10 功耗：后台线程"按需休眠"的最长单次睡眠（秒）。
# 原实现无论轮询间隔多长都固定睡 0.1s 再问一次"到点没"，实测盘后两条线程
# 各 9.7 次/秒的唤醒里 99.7% 是空转 —— 单次 CPU 只有几十微秒，但每秒近 20 次
# 唤醒让 CPU 始终进不了深度 idle，这才是能耗大头。
# 现在睡眠长度按"距下次真正该做事"计算，本值只是兜底上限：即便出现未登记的
# 状态变化，最迟 5 秒也会重新评估一次决策。交易时段内到期间隔本就 ≤0.1s，
# 因此本上限**不会**改变盘中拉取节奏（实测拉取时刻逐次不变）。
LOOP_TICK_CAP = 5.0
CONN_POOL_MAX = 5
# 空闲连接存活上限（秒）：盘中轮询最快 0.1s，不会触发；非交易时段间隔 3600s，
# 下次请求时连接必定已超时重建 —— 30s 只为避免长时间白占着服务端连接。
CONN_POOL_IDLE_TIMEOUT = 30
# 网络埋点只服务于诊断导出与下面 60s 窗口的聚合日志，不参与任何业务/熔断判定；
# 环形缓冲写满即丢弃最旧记录，所以统计量只代表"最近若干次请求"。
MONITOR_RING_CAPACITY = 1000
MONITOR_WINDOW_SECONDS = 60
LOG_MAX_BYTES = 5242880
LOG_MAX_AGE_DAYS = 7
# ---- 程序身份（2026-10-09 用户拍板：全局改名 Skyrocket，bundle id 一并改）----
# 唯一来源：窗口标题、Dock/关于名、临时副本清扫、更新清单校验都从这里读。
APP_DISPLAY_NAME = 'Skyrocket'
BUNDLE_ID = 'com.skyrocket.app'
# 旧名字必须继续认得：/Applications 与 /tmp 里还有 ≤v1.4.54 的旧包，
# 清扫与「正式版在运行」的检测漏掉它，旧副本就会被 LaunchServices 重新拉起
# （v1.4.5 那次 SIGSEGV 事故的根源），卸载残留也会漏扫。
LEGACY_BUNDLE_IDS = ('com.stockfloat.targetprice',)
LEGACY_APP_DISPLAY_NAMES = ('实时股价-目标价版',)
# 旧**可执行文件名**也要单独成代：崩溃记录的文件名、pgrep 的进程名、
# 「正式版在运行」那条守卫用的路径，认的都是它而不是中文显示名。
# 此前它只散在 sf_crashreport / sf_logging / run_tests.sh 的字面量里，
# 第三次改名时没有任何东西会提醒改这几处（判据见 tests/test_identity_literals.py）。
LEGACY_EXECUTABLE_NAMES = ('StockFloatTargetPrice',)
# 数据目录**故意不改名**（用户选的是「改 id、数据不动」）：目标价、窗口位置、
# 证券字典缓存都在这里，换目录等于让用户重录一遍。
APP_SUPPORT_DIR = os.path.expanduser('~/Library/Application Support/StockFloatTargetPrice')
# 自动更新：公开 GitHub Release 分发；清单必须经内置公钥验签。
# 检查调度：启动15秒首检；失败后5分钟补一次，继续失败则30分钟；稳定运行每6小时复查。
UPDATE_INITIAL_DELAY_MS = 15000
UPDATE_RETRY_FIRST_MS = 5 * 60 * 1000
UPDATE_RETRY_LATER_MS = 30 * 60 * 1000
UPDATE_PERIODIC_MS = 6 * 60 * 60 * 1000
# v1.4.43 清单请求的超时与重试。**数字来源**（本机 2026-10-08 对
# UPDATE_MANIFEST_URL 实测 6 次）：首字节 7.38 / 7.70 / 7.94 / 8.04 / 8.25 秒，
# 另有 1 次 12 秒无响应。原预算是硬编码 5 秒 —— 在这条链路上**每次检查都必失败**，
# 自动更新等于不存在。12 秒覆盖住 8 秒量级并留有余量；重试一次只用来吃掉
# "单条连接被 CDN 掐断"这类瞬时故障，持续失败仍照实上抛给界面汇报。
UPDATE_MANIFEST_TIMEOUT_SEC = 12
UPDATE_MANIFEST_ATTEMPTS = 2
# v1.4.44 在飞检查的过期线。为什么需要：回包是有界队列（32 格，满时丢最旧）里的
# 一次性事件，一旦被丢掉，`_update_checking` / `_update_transition_pending` 就**永久
# 锁真** —— 之后每一次检查（含用户手动点的）都起不来，那张「正在检查更新」也永远
# 关不掉，只能重启程序。取 45 秒：12 秒 × 2 次重试 = 24 秒的预算上限，再加 20 秒
# 余量给 DNS/TLS 抖动，仍远小于一次 6 小时复查的间隔，误判"卡死"的代价只是多问一次。
UPDATE_CHECK_STALE_SEC = 45
# v1.4.43 切回本程序触发的补查节流。原实现是"每次变为活跃都查一次"，alt-tab
# 一天几十次就是几十次请求，且每次都占住在飞标记最长一个网络往返 —— 正好把用户
# 随后点的"检查更新"撞进在飞窗口里（撞上了就什么都不弹）。
# **必须用墙钟**：time.monotonic 在 macOS 睡眠期间不前进，用它的话一小时睡眠
# 醒来只会算"过了几秒"，把真正的唤醒补查误吞掉。
UPDATE_RESUME_MIN_GAP_SEC = 60.0
# v1.4.23 临时目录旧副本自愈清扫：启动 45 秒后在后台跑一次，避开首帧、首个行情
# 轮询与 15 秒的更新自检，三者都在同一台机器上抢磁盘/CPU。详见 sf_temp_sweep。
TEMP_SWEEP_DELAY_MS = 45000
# 更新 staging 残留的自愈清扫（诊断包第一次真跑就读出本机 updates/ 里 80,382,786 字节
# 残留，含一份 26MB 的下载半包 —— v1.4.59 的剪枝只做在回退包上，半包从没被扫过）。
# 排在临时清扫之后：同一台机器上两件事都抢磁盘，而"旧副本被 LaunchServices 拉起来崩"
# 比"updates/ 里多占 80MB"更急。
STAGING_PRUNE_DELAY_MS = 90000
# 判定"这已经不是本次在写的东西"的年龄门槛（秒）。本机实测一次完整下载在上行
# ~57KB/s 的最坏档也要不了 20 分钟，所以一小时之外还躺在 updates/ 里的分片/断点状态，
# 只可能是没有人在写的孤儿。
STAGING_PRUNE_GRACE_SEC = 3600
# 一小时内改过的任何文件都当作"**可能正在写**"，一律不动：本机实测一次完整下载
# 在上行 ~57KB/s 的最坏档要几分钟，正常档远短于此；一小时之后还躺在 staging 里的
# 分片/断点状态，只可能是没有主人的孤儿。
STAGING_PRUNE_GRACE_SEC = 3600
# V1.3.96 起新客户端从 releases 仓库 main 分支的固定文件读取签名清单。
# 旧客户端仍读取固定 Release 资产，发布时不删除该资产以保持兼容。
# v1.4.55 改名后走**新的清单文件**。为什么不能沿用旧清单：清单里写着 bundle_id
# 与 app_name，客户端拿自己那两个值比对（sf_update 的清单校验），一份清单喂不了
# 两代程序；而旧版内置的更新助手只认旧 id，硬发过去就是一次注定失败的更新。
# 所以旧清单冻结在 1.4.54，改名后的这一版需要手动装一次 DMG。
UPDATE_MANIFEST_URL = ('https://raw.githubusercontent.com/1056572031-boop/'
                       'StockFloat-TargetPrice-Releases/main/'
                       'Skyrocket-update.json')
# v1.4.60（复核 F10）：原先这里还有一条 LEGACY_UPDATE_MANIFEST_URL（旧清单
# StockFloatTargetPrice-update.json 的地址），注释说"诊断包与文档要说清已装旧版为什么
# 收不到新版"，但全仓（含 tests/tools）没有任何消费点 —— 一个没人读的常量只会让人以为
# 程序还会去读那份旧清单。那句话的真正落点是 AGENTS.md §3「OTA 通道随改名分叉」与
# DELIVERY_CHECKLIST 的 v1.4.55 交付记录，删掉常量、把指向留在这行注释里。
UPDATE_PUBLIC_KEY_B64 = '3heAx8bOzfbvlJegpzuhf3uOut/cwwk3fNVvXIBM3x4='
# 取得正式 Developer ID 证书后，必须同时填写 Team ID 与叶证书 SHA-1（大写、无冒号）。
# 留空仅适用于当前 ad-hoc 内测签名；正式发版构建脚本将拒绝仅填一项的配置。
UPDATE_SIGNING_TEAM_ID = ''
UPDATE_SIGNING_CERT_SHA1 = ''
UPDATE_MANIFEST_MAX_BYTES = 65536
UPDATE_ARCHIVE_MAX_BYTES = 536870912
UPDATE_STAGING_DIR = os.path.join(APP_SUPPORT_DIR, 'updates')
# v1.3.50：支持 SF_CONFIG_PATH 覆盖 —— 跑测试时必须重定向到临时目录。
# 不重定向的话，回归测试会往**用户真实配置**里写东西：本次实测一次全量回归就把
# 线上 bg 从 glass 写成 translucent，直接表现为"毛玻璃消失"，且误导排障方向
# （以为是渲染/并发问题）。口径与 SF_LAST_QUOTE_PATH 一致：不设时行为完全不变。
CONFIG_PATH = os.environ.get('SF_CONFIG_PATH') or os.path.join(APP_SUPPORT_DIR, 'config.json')
# v1.2.23：配置备份快照路径。每次 save_config 前把旧配置备份一份到这里
# （仅保留最近 1 份），主配置损坏/解析失败时用于自动恢复。
CONFIG_BACKUP_PATH = CONFIG_PATH + '.bak'
LEGACY_CONFIG_PATH = os.path.expanduser('~/.stock_float_config_targetprice.json')
ICON_PATH = os.path.join(APP_SUPPORT_DIR, 'icon.png')
# v1.3.45：首屏数据快照（上次退出前的最后一条行情 + 分时点）。
# 独立于 config.json —— 行情每秒都在变，绝不能和「目标价/自定义接口」这类
# 用户配置混进同一个文件，否则一次高频写回就可能把配置覆盖成半成品。
LAST_QUOTE_PATH = os.path.join(APP_SUPPORT_DIR, 'last_quote.json')
LAST_QUOTE_MIN_WRITE_INTERVAL = 20.0   # 快照落盘节流（秒）：行情 8 次/秒，不能每次都写盘
# v1.3.67：支持 SF_LOG_PATH 覆盖 —— 与 config / last_quote 同款测试隔离。
# 不隔离的代价已实测：一份真实 debug.log 里 1.3 万条是测试写的 mock 失败，
# 真机数据源健康度从此无法从日志判断（本次排查"降级是否集中在开盘/午休"
# 就是被这批噪声带偏，结论完全不可用）。不设该变量时行为完全不变。
LOG_PATH = os.environ.get('SF_LOG_PATH') or os.path.join(APP_SUPPORT_DIR, 'debug.log')
# v1.3.66：本地股票字典（全 A 代码/名称表，约 5900 条 / 135KB）。
# 独立于 config.json / last_quote.json —— 它是**可重建的缓存**（丢了最多退回
# 联网搜索），绝不能和用户配置混在一个文件里。
UNIVERSE_PATH = os.environ.get('SF_UNIVERSE_PATH') or os.path.join(APP_SUPPORT_DIR, 'securities.json')
# 刷新周期按"天"计即可：字典陈旧只影响速度不影响正确性（本地没命中会退联网）。
UNIVERSE_TTL = 259200.0           # 字典有效期（秒，3 天）
UNIVERSE_MIN_INTERVAL = 3600.0    # 后台刷新最小间隔（秒）：失败重试也不至于刷屏
UNIVERSE_PAGE_SIZE = 100          # 东财 clist 单页上限（实测 pz 最大 100，写死）
UNIVERSE_MAX_PAGES = 60           # 分页上限：5913 条 / 100 ≈ 60 页，防死循环
# 并发度是这个接口的命门（2026-09-14 实测 20 页对照）：
#   并发5+连接池 0/20 成功（62.8s 全断连）｜ 并发3+独立连接 6/20 ｜ 并发3+间隔 4/20
#   并发2+连接池+页间 0.15s  20/20 成功，3.7s。
# 东财对并发极度敏感，早先并发 5 那次能通纯属运气。宁可慢（全量约 11s）也要稳。
UNIVERSE_WORKERS = 2
UNIVERSE_PAGE_GAP = 0.15          # 每页取完后的间隔（秒）
UNIVERSE_TIMEOUT = 6.0            # 单页超时（秒）
UNIVERSE_PAGE_RETRIES = 2         # 单页失败重试次数
UNIVERSE_MIN_ROWS = 5000          # 少于此条数视为残缺不落盘（正常约 5900）
# v1.3.67：支持 SF_SINGLE_SOCK 覆盖 —— 同属落盘物，测试期必须挪走。
# 不隔离时测试会去连接/绑定真机的单实例锁，与正在运行的悬浮窗抢实例
# （表现为"跑测试时界面莫名退出"或"app 起不来"）。不设时行为完全不变。
SINGLE_SOCK = os.environ.get('SF_SINGLE_SOCK') or os.path.join(APP_SUPPORT_DIR, 'stock_float.sock')
DEFAULT_STOCK = 'sh600519'
R = 30  # 主窗与两个编辑弹窗统一圆角；文字与数字样式不变
W, H = 184, 192  # 主界面宽度为原始 200px 的 92%；高度保持不变
MIN_H = 224
MAX_H = 420
# v1.4.59（走查 S8）：涨跌业务色收拢到这一处。此前主卡用 #ff5252、目标价面板用
# #ff6b6b，而面板注释写的却是"与主界面涨色一致" —— 同屏确实出现了两种红，注释还在说谎。
# 统一取主卡值（两档实测都过 WCAG AA 小字 4.5:1：对面板底 #1d1e20 分别 6.01:1 / 5.23:1，
# 对纯黑 7.57:1 / 6.58:1，所以合并不会把对比度换掉）。
# v1.4.59（走查 S8）：右上三按钮图标的出图口径。
# 旧写法只有 button_images() 里一个裸 20（注释说"20px@2x"），而绘制框是 12pt：
# Retina 上 12pt 需要 24 个设备像素，20 的源图因此被**上采样 1.2 倍** —— 图标发虚。
# 本机离屏逐像素实测：20px 无 DPR 画进 DPR=2 的帧，与 24px 直渲最大通道差 255、
# 平均差 34.9；改成 24px + setDevicePixelRatio(2) 之后差 0（同一个绘制调用、同一张参考图）。
BTN_ICON_PT = 12                 # 逻辑边长（pt），sf_ui_render.BTN_ICON_SZ 由它派生
BTN_ICON_DEVICE_RATIO = 2.0      # 出图倍率：与 Retina 的 1pt=2px 对齐
# 右键菜单行首图标的出图口径（2026-10-13，用户看图要求给菜单图标略缩小）。
# 保留 16pt 透明画布，在画布内居中缩小图形；只缩源图尺寸会被 Qt 重新适配图标槽。
# 实帧核验四行图标的墨迹高度均略低于文字，菜单文字、行高与图标列位置不变。
MENU_ICON_PT = 16
MENU_ICON_CONTENT_SCALE = 0.65
MENU_ICON_DEVICE_RATIO = 2.0
UP_RED_HEX = '#ff5252'
DOWN_GREEN_HEX = '#4caf50'
# 次要文字灰：旧值 #6b7280 对纯黑只有 4.34:1（报告按真渲染取到 4.1:1），低于小字 4.5:1。
# 新值实测对面板底 #1d1e20 = 4.98:1、对纯黑 = 6.27:1，两条底都过 AA；
# 判据由 tests/test_v1459_color_tokens.py 算式复核（改这几个色值会被它拦下）。
# FG_DIM_HEX（v1.4.60，复核 F6）：同一个值的字符串形态，给 Qt 样式表用 ——
# 菜单里「点不动的那几行」原先还写着旧值 #6b7280，就是这一族提亮漏的最后一处。
# 唯一来源仍是这两行，样式表里不许再手写 hex。
FG_DIM_HEX = '#848d9c'
FG_DIM = QColor(FG_DIM_HEX)
# ---- 文字与控件的灰阶（v1.4.64，全方位复核 F6）----
# 为什么现在收：AGENTS §5.2-6 那句"次要文字灰只有一个来源、样式表里不许再手写 hex"
# 此前只做到了主窗与菜单两处，实测活代码里还剩 **15 处**裸 hex 写在样式表字符串里
# （target_panel / sf_editor / sf_glass 各若干），其中两处禁用态灰
# #5a6072（2.66:1）与 #68718a（3.43:1）**比这一轮被判不达标并删掉的 #6b7280 还暗**。
# 现在每个用途一个具名 token，值全部沿用原样（唯一改动视觉的是下面 FG_DISABLED_HEX），
# 判据在 tests/test_style_hex_tokens.py：样式表字符串里出现裸 hex 直接判红（硬零，
# 不留基线 —— 留基线等于承认还可以有下一处）。
FG_BRIGHT_HEX = '#ffffff'    # 标题与彩色按钮上的白字
FG_TEXT_HEX = '#e5e9f0'      # 常规正文 / 次级按钮文字
FG_FIELD_HEX = '#d8dce5'     # 输入框与可点条目的正常文字
FG_HINT_HEX = '#9aa3b5'      # 提示小字（10~11px）
FG_INFO_HEX = '#9aa2b8'      # 面板 info 行；与主界面代码行 FG_CODE 同值，别再各写一份
FG_ERROR_HEX = '#d3d7e0'     # 面板错误行文字
FG_WARN_HEX = '#b7becd'      # 面板警告行文字
# 禁用态：旧值 #5a6072 对面板底只有 2.66:1、#68718a 只有 3.43:1 —— 都低于本仓
# 自己定的小字 4.5:1（WCAG 1.4.3 对"非活动组件"是豁免的，所以这不是无障碍违规，
# 违规的是"同一族灰被提亮、这两处漏着且比被删掉的旧值还暗"）。
# 新值 #80899b 实测对面板底 #1d1e20 = 4.74:1（留 0.24 的余量，不是踩线过关），
# 相对亮度是启用提示灰 #9aa3b5 的 0.683 倍 —— 仍明显更暗，"点不动"这件事照样看得出来。
FG_DISABLED_HEX = '#80899b'
ACCENT_BLUE_HEX = '#4f7cff'   # 主操作蓝：菜单选中行与弹窗「确定」钮同一个值，此前各写一份
MENU_SEPARATOR_HEX = '#3b4256'
DANGER_BG_HEX = '#e5484d'    # 「再点一次确认清空」那颗红钮（三态一组，不拆散）
DANGER_HOVER_HEX = '#f2555a'
DANGER_PRESSED_HEX = '#cc3d42'
BG_MODE_DEFAULT = 'glass'
# v1.2.35：毛玻璃暗化加深 —— 用户反馈太透光。调大玻璃层上叠加的黑色 alpha
# （数值越大越暗、透过桌面内容越少；暗色系统 150、浅色系统 185 兼顾白字对比度）
BG_GLASS_DIM = 150
BG_GLASS_DIM_LIGHT = 185
GLASS_INSET = 3
BG_LIQUID_GLASS_DIM = 164
LIQUID_GLASS_OPACITY = 0.95
LIQUID_GLASS_INSET = 2
# v1.2.20：玻璃层圆角 mask 重建节流间隔（秒）。拉伸时窗口高度逐帧变化，
# 若每帧都重建 squircle CGPath + setPath_（CoreAnimation 强制重绘）会严重拖慢 resize，
# 节流到 60ms 一次（约 16fps），拉伸结束后再强制重建一次对齐，视觉无感知。
GLASS_MASK_THROTTLE = 0.06
QUOTE_URL_FALLBACK = 'https://hq.sinajs.cn/list=%s'
MINUTE_URL_FALLBACK = 'https://push2his.eastmoney.com/api/qt/stock/trends2/get?secid=%s&fields1=f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13&fields2=f51,f52,f53,f54,f55,f56,f57,f58&ndays=1&iscr=0'
SINA_REFERER = 'https://finance.sina.com.cn/'
API_URLS_DEFAULT = {'quote': QUOTE_URL, 'quote_fallback': QUOTE_URL_FALLBACK, 'minute': MINUTE_URL, 'minute_fallback': MINUTE_URL_FALLBACK, 'search': SEARCH_URL, 'eastmoney_search': EASTMONEY_SEARCH_URL, 'sina_referer': SINA_REFERER}
CONFIG_SCHEMA_VERSION = 1
# v1.2.71 第4轮走查：LOG_RETENTION_DAYS 从未被引用（日志清理实际读的是
# LOG_MAX_AGE_DAYS），同名不同义的死常量会误导后续维护者，已删除。
# 日志轮转保留的世代数量（debug.log.1 ~ .N）：只留一代时，一次长时间异常
# 刷屏就会把更早的可用日志挤掉，排障时拿不到故障前的现场。
LOG_ROTATE_KEEP = 3
# 轮转检查的最小间隔（秒）：log_line 每次调用都会做一次 stat + listdir，
# 高频日志路径下这是纯粹的重复系统调用，节流不影响轮转正确性。
LOG_ROTATE_CHECK_INTERVAL = 10.0
# 搜索结果收口上限（去重后最多展示条数，避免腾讯返回大量结果时卡顿）
SEARCH_MAX_RESULTS = 50
# v1.3.65 搜索提速：
# - 腾讯主搜索与东财（北交所兜底）改为并行，总耗时由 sum 变 max；
# - 腾讯结果已达该条数时，东财回包直接丢弃（沪深热门词不必等北交所补充）；
# - 交互场景收紧超时/重试，最坏耗时从 ~13s 降到 ~6s；
# - 结果缓存：同一关键词短期重复搜索直接命中，不再发请求。
SEARCH_BJ_FALLBACK_MIN = 50     # 腾讯结果少于此值才并入东财（北交所）候选；
                                # 默认等同旧行为（= SEARCH_MAX_RESULTS），兜底能力不变
SEARCH_TIMEOUT = 3.0            # 腾讯主搜索超时（秒）
SEARCH_RETRIES = 1              # 腾讯主搜索重试次数（原 HTTP_RETRIES=2）
SEARCH_BJ_TIMEOUT = 3.0         # 东财北交所兜底超时（秒，原 6）
SEARCH_CACHE_TTL = 300.0        # 搜索结果缓存有效期（秒）
SEARCH_CACHE_MAX = 64           # 缓存条目上限（LRU）
# 2026 A 股休市日期（非周末的法定节假日；来源：沪深北交易所 2025-12-22 公告）。
# 仅覆盖当年，跨年需补充下一年度。周末已由 _is_trading_time 自动排除，此处只列非周末休市日。
TRADING_HOLIDAYS_2026 = {
    "2026-01-01", "2026-01-02", "2026-01-03",
    "2026-02-15", "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19",
    "2026-02-20", "2026-02-21", "2026-02-22", "2026-02-23",
    "2026-04-04", "2026-04-05", "2026-04-06",
    "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",
    "2026-06-19", "2026-06-20", "2026-06-21",
    "2026-09-25", "2026-09-26", "2026-09-27",
    "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04",
    "2026-10-05", "2026-10-06", "2026-10-07",
}
# 统一别名：跨年补上下一年度休市日后，只需把这里指向新的年度集合。
TRADING_HOLIDAYS = TRADING_HOLIDAYS_2026
