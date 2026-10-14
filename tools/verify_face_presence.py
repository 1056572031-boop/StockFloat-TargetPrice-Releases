# -*- coding: utf-8 -*-
"""v1.4.40 人脸在场守卫的实机核验工具 —— 真开一次摄像头，把计数打出来。

用法（开发机与 iMac 实机通用）：
    ./.venv/bin/python tools/verify_face_presence.py                 # 默认 12 秒
    ./.venv/bin/python tools/verify_face_presence.py --seconds 30
    ./.venv/bin/python tools/verify_face_presence.py --image         # 静态图核验，不碰摄像头
    ./.venv/bin/python tools/verify_face_presence.py --self-test     # 不碰摄像头

它证明的是回归用例证明不了的那一段：AVFoundation 真起会话 → CMSampleBuffer 真能
喂给 VNImageRequestHandler → 计数真能随人数变化。用例里这些全是假类。

⚠ 首次运行会弹系统摄像头授权框（对象是**这个 python 进程**，不是已安装的 app）。
   产物侧的授权要在装机版里右键开启「自动隐藏窗口」（文案唯一来源
   `sf_constants.FACE_GUARD_MENU_TEXT`）后单独授予一次。

单人自测：把 outputs/人脸守卫自测素材-两人照.png 显示在手机上举到摄像头前，
Vision 会把照片里的两张脸当真脸检出 —— 本机实测如此，这也是误触发方向的来源。
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import sf_presence                                       # noqa: E402


def _report(count: int) -> str:
    return "%d 张脸 %s" % (count, "→ 应触发收起" if count >= 2 else "")


# 默认素材：仓库里那张两人照。为什么用它就能核验 —— 本机实测 Vision 会把照片里的
# 脸当真脸检出（这也是误触发方向的来源），所以它是"不需要摄像头的正样本"。
DEFAULT_IMAGE = str(ROOT / "outputs/人脸守卫自测素材-两人照.png")


def judge(count: int, expect_min: int) -> str:
    """把"数到几张脸"翻译成核验结论。纯函数，自检里拿它做变异判定。"""
    if count >= expect_min:
        return "pass"
    return "fail"


def run_image(path: str = DEFAULT_IMAGE, expect_min: int = 2) -> int:
    """静态图跑一遍 Vision 链，并把**每一步的返回形状**打出来。

    为什么单独有这一档（v1.4.50，台账 E02 复盘）：`--seconds` 那一档要摄像头 +
    TCC 授权，开发机上裸 python 永远拿不到帧，于是这条链从 v1.4.40 上线以来
    一次都没被自动核验过 —— 界面写"运行中"、日志一个字没有，谁也分不清
    "会话没发帧"和"发帧了但检测失败"。Vision 这半段不依赖授权，静态图就能跑。
    """
    cls, why = sf_presence._load_frameworks()
    if cls is None:
        print("!! 系统框架不可用：%s" % why)
        print("   这一档在没有 macOS 系统框架的机器上是**跳过**，不是通过。")
        return 2
    img = pathlib.Path(path)
    if not img.exists():
        print("!! 素材图不存在：%s" % img)
        return 2
    try:
        from Foundation import NSURL
    except Exception as exc:
        print("!! PyObjC/Foundation 不可用：%r" % (exc,))
        return 2

    print("== Vision 静态图核验（不碰摄像头、不需要授权）==")
    print("   素材：%s（%d 字节）" % (img, img.stat().st_size))
    req = cls["VNDetectFaceRectanglesRequest"].alloc().init()
    print("   请求类：%s" % type(req).__name__)
    url = NSURL.fileURLWithPath_(str(img))
    handler = cls["VNImageRequestHandler"].alloc().initWithURL_options_(url, None)
    print("   handler：%s（initWithURL:options: 返回形状=%s）"
          % (type(handler).__name__, "单个对象" if handler is not None else "None"))
    if handler is None:
        print("!! handler 构造失败：界面会显示为『检测失败』而不是『运行中』")
        return 1
    ret = handler.performRequests_error_([req], None)
    print("   performRequests:error: 返回 %r（类型 %s）" % (ret, type(ret).__name__))
    if not sf_presence.vision_request_succeeded(ret):
        print("!! Vision 拒绝处理图片，不能用空 results 冒充成功")
        return 1
    raw = req.results()
    obs = list(raw or [])
    print("   results()：%s，%d 个框" % (type(raw).__name__, len(obs)))
    for i, ob in enumerate(obs):
        try:
            print("      #%d confidence=%.3f" % (i, float(ob.confidence())))
        except Exception as exc:
            print("      #%d 读不到 confidence：%r（产品侧按『计 1 张』处理）" % (i, exc))
    count = sf_presence.count_faces(obs)
    verdict = judge(count, expect_min)
    print("== 结论：%s（数到 %d 张，阈值 conf>=%.2f，要求 ≥%d 张）=="
          % ("通过" if verdict == "pass" else "不通过", count,
             sf_presence.FACE_GUARD_CONF_MIN, expect_min))
    if verdict != "pass":
        print("   这一条不通过意味着 Vision 侧就是坏的：装机版里摄像头即使正常发帧，"
              "也数不出第二张脸，界面现在会写成『画面已采到但系统认不出脸』。")
    return 0 if verdict == "pass" else 1


def run(seconds: float) -> int:
    mon = sf_presence.FacePresenceMonitor()
    print("== 装载系统框架并请求采集 ==")
    print("   授权状态:", sf_presence.camera_access_status())
    state = mon.start()
    print("   start() ->", state)

    if state == sf_presence.STATE_STARTING:
        print("   等待你在系统弹窗里点「允许」（最多 30 秒）…")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and mon.state != sf_presence.STATE_RUNNING:
            if not mon.is_starting():
                state = mon.start()
            time.sleep(0.3)
        print("   最终状态:", mon.state)

    if mon.state != sf_presence.STATE_RUNNING:
        print("!! 没能起来，界面会显示：" , mon.last_error or mon.state)
        print("   这一条本身就是验收点：保护没在跑时必须看得见，不能装作有。")
        return 1

    print("== 采集运行中，%d 秒内每秒报一次计数 ==" % seconds)
    print("   （对摄像头挥一下手挡住、或把两人照片举进去，看计数会不会跳到 2）")
    end = time.monotonic() + seconds
    last = None
    seen = []
    while time.monotonic() < end:
        count, ts = mon.snapshot()
        if ts and count != last:
            print("   +%.1fs  %s" % (end - time.monotonic(), _report(count)))
            last = count
            seen.append(count)
        time.sleep(0.2)
    mon.stop()
    print("== 结束：采样后计数 %r，状态 %s ==" % (seen, mon.state))
    print("   停表后应立刻没有帧：绿灯熄灭即为通过。")
    return 0


def self_test() -> int:
    """不碰摄像头的结构自检：每个版本都必须能跑到这里。

    守的是三件会说谎的事：框架取不到类、阈值表被改坏、状态机不再迟滞。
    """
    fails = []

    cls, why = sf_presence._load_frameworks()             # 取类不需要授权
    if cls is None:
        fails.append("系统框架装载失败：%s" % why)
    else:
        missing = [k for k, v in cls.items() if v is None]
        if missing:
            fails.append("类缺失：%s" % missing)

    a = sf_presence.PresenceArbiter()
    if a.feed(2, now=0.0) != sf_presence.EVENT_NONE:
        fails.append("单次命中就触发收起（迟滞失效）")
    if a.feed(2, now=1.0) != sf_presence.EVENT_HIDE:
        fails.append("连续命中没有触发收起")
    if a.feed(0, now=2.0) != sf_presence.EVENT_NONE:
        fails.append("空房间被当成了「只剩一个人」，会在无人时暴露屏幕")

    if sf_presence.count_faces([_Fake(0.9), _Fake(0.1)]) != 1:
        fails.append("低置信度框没被过滤")

    # v1.4.50 新增：`--image` 这一档自己必须能失败。它存在的理由是"以前没有不依赖
    # 摄像头的核验"，如果判据本身永真，就只是把谎换到了一个更好跑的地方说。
    if judge(2, 2) != "pass" or judge(3, 2) != "pass":
        fails.append("judge 把达标数成了不达标（核验器会假红，下一轮就没人跑它）")
    for low in (0, 1):
        if judge(low, 2) != "fail":
            fails.append("judge 把 %d 张脸判成达标：一张脸都数不出来时它也会说通过" % low)
            break
    missing_rc = run_image(str(pathlib.Path(__file__).resolve().parent / "没有这个文件.png"))
    if missing_rc == 0:
        fails.append("素材图不存在时 run_image 仍返回 0 —— 这是一道永远过的关")

    # ---- 2026-10-12 新增：边脸放宽、距离闸门、三条判据本身都要能失败 ----
    cf = sf_presence.count_faces
    if cf([_FakeBox(0.57, 0.70, 0.25, 0.30)]) != 1:
        fails.append("贴边且把握分 %.2f 的框没被计入：用户拍板的「被切一半的边脸」又回到不收窗"
                     % 0.57)
    if cf([_FakeBox(0.57, 0.30, 0.30, 0.30)]) != 0:
        fails.append("放宽漏到了画面中间的脸 —— 贴边这条豁免不认边界，等于全局降到 0.55")
    if cf([_FakeBox(0.40, 0.70, 0.25, 0.30)]) != 0:
        fails.append("贴边但把握分只有 0.40 也被计入：放宽没了下限，噪点框就能收窗")
    if cf([_FakeBox(0.80, 0.30, 0.30, 0.03)]) != 0:
        fails.append("占画面只有 3% 的远脸被计入：1.5 米闸门没生效")
    if cf([_FakeBox(0.80, 0.30, 0.30, 0.20)]) != 1:
        fails.append("闸门之内的近脸被滤掉了：尺寸闸门比检测能力还严，会漏掉该收的窗")
    if cf([_FakeBox(0.80, 0.96, 0.25, 0.05)]) != 1:
        fails.append("贴边的脸也被尺寸闸门滤掉：被边界切短的框尺寸不可信，这条豁免必须有")
    if cf([_NoGeo(0.57)]) != 0:
        fails.append("读不到几何的框获得了放宽资格 —— 桥接形状一变就自动放行")

    if judge_edge([(0.0, 2), (0.5, 1)]) != "fail":
        fails.append("judge_edge 在「半张脸出画没算上」时仍说通过：这一档永远不会红")
    if judge_edge([(0.0, 2), (0.5, 2), (0.8, 1)]) != "pass":
        fails.append("judge_edge 把系统根本不返回框的档位（切 80%）判成失败：每轮必红的关")
    if judge_near([(0.30, 2), (0.08, 1)]) != "fail":
        fails.append("judge_near 对闸门之内的漏检视而不见：尺寸闸门与检测能力打架时它不说")
    if judge_near([(0.30, 2), (0.02, 0)]) != "pass":
        fails.append("judge_near 把闸门之外的远脸判成失败：那正是「远处的人不该收窗」")
    if judge_lowlight([("σ=25", 3, 3)]) != "fail":
        fails.append("judge_lowlight 放过了「噪点多造出一张脸」：后果是天一黑行情自己消失")
    if judge_lowlight([("σ=25", 2, 1)]) != "fail":
        fails.append("judge_lowlight 对暗光下漏掉第二个人视而不见")
    ok_pair = [(160, 2), (90, 2), (32, 1), (26, 1)]
    if judge_edge_floor(ok_pair) != "pass":
        fails.append("judge_edge_floor 把正确结果判成失败：这一档会每轮必红")
    if judge_edge_floor([(90, 1)]) != "fail":
        fails.append("judge_edge_floor 放过「近处半张脸不算」：R1 被改坏时它不说")
    if judge_edge_floor([(18, 2)]) != "fail":
        fails.append("judge_edge_floor 放过「远处半张脸也算」：R2 被改坏时它不说")
    # 亮度这一头要真解码像素，而人脸素材按隐私决定不入库（.gitignore 里那条
    # `outputs/人脸守卫自测素材-*.png`）—— 换一棵工作树、换一台机器就没有。
    # 缺它时**如实跳过并打印理由**，不许记成失败（否则全量在任何没有素材的树上恒红），
    # 也不许悄悄当通过（否则"压暗其实没压暗"这类事又会活过一整轮）。
    if pathlib.Path(DEFAULT_IMAGE).exists():
        for msg in _check_darkness():
            fails.append(msg)
    else:
        print("   [跳过] 亮度核验：素材不在（人脸照不入库），这一条本轮没被核验")

    print("== 自检结论 ==" if not fails else "!! 自检失败")
    for f in fails:
        print("   -", f)
    print("   阈值：tick=%gs hide=%d restore=%d conf>=%.2f idle=%gs"
          % (sf_presence.FACE_GUARD_TICK_SEC, sf_presence.FACE_GUARD_HIDE_HITS,
             sf_presence.FACE_GUARD_RESTORE_HITS, sf_presence.FACE_GUARD_CONF_MIN,
             sf_presence.FACE_GUARD_MAX_IDLE_SEC))
    return 1 if fails else 0


class _FakeBox:
    """带几何的假框：(把握分, x, 高, 宽)，坐标是画面归一化、原点在左下。

    只用来验 `count_faces` 的三条口径（把握分 / 贴边放宽 / 尺寸闸门），
    真 Vision 那一头由 `--edge`/`--near`/`--lowlight` 三档负责。
    """

    def __init__(self, conf, x, h, w):
        self._c, self._x, self._h, self._w = conf, x, h, w

    def confidence(self):
        return self._c

    def boundingBox(self):
        return ((self._x, 0.2), (self._w, self._h))


class _NoGeo:
    """读不到 boundingBox 的框：一律走最严判定，不许白拿放宽资格。"""

    def __init__(self, conf):
        self._c = conf

    def confidence(self):
        return self._c

    def boundingBox(self):
        raise RuntimeError("这个框读不出几何")


class _Fake:
    def __init__(self, conf):
        self._c = conf

    def confidence(self):
        return self._c


def run_curve(path: str = DEFAULT_IMAGE) -> int:
    """把同一张两人照逐级缩小，打「脸有多少像素 → 置信度多少」的曲线。

    为什么值得做成一档（v1.4.56）：`FACE_GUARD_CONF_MIN=0.6` 这个阈值该不该降
    是个能被**量**出来的问题，不该靠印象。本机实测：脸宽从 427px 一路缩到 69px，
    置信度始终 0.81~0.83 —— 远处的脸不是被阈值挡掉的，Vision 本来就给高分；
    把阈值降下来只会放进海报/屏幕里的人像，换不到真检出。这条曲线就是那句结论的
    可复现证据（需要 `sips` 生成缩放图，没有素材或没有系统框架时返回 2 = 跳过）。
    """
    import shutil
    import subprocess
    import tempfile
    import time

    cls, why = sf_presence._load_frameworks()
    if cls is None or not pathlib.Path(path).exists():
        print("!! 跳过：框架或素材不可用（%s）" % (why or path))
        return 2
    if shutil.which("sips") is None:
        print("!! 跳过：这台机器没有 sips，做不了缩放")
        return 2
    from Foundation import NSURL
    H, RQ = cls["VNImageRequestHandler"], cls["VNDetectFaceRectanglesRequest"]
    src = pathlib.Path(path)
    print("== 置信度对脸尺寸曲线（素材 %s）==" % src.name)
    print("   %-7s %-5s %-24s %-9s %s" % ("图宽", "张数", "置信度", "框宽px", "ms/帧"))
    worst = 99.0
    with tempfile.TemporaryDirectory() as td:
        for w in (1024, 640, 448, 320, 256, 192):
            img = pathlib.Path(td) / ("w%d.jpg" % w)
            subprocess.run(["sips", "-z", str(int(w * 0.75)), str(w), str(src),
                            "--out", str(img), "--setProperty", "format", "jpeg"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=False)
            if not img.exists():
                print("   %-7s 缩放失败" % w)
                continue
            confs, boxes, ms = [], [], []
            for _ in range(4):
                req = RQ.alloc().init()
                h = H.alloc().initWithURL_options_(
                    NSURL.fileURLWithPath_(str(img)), None)
                t0 = time.perf_counter()
                ok = h.performRequests_error_([req], None)
                ms.append((time.perf_counter() - t0) * 1000)
                obs = list(req.results() or []) if ok else []
                confs = [round(float(o.confidence()), 3) for o in obs]
                boxes = [round(float(o.boundingBox()[1][0]) * w) for o in obs]
            if confs:
                worst = min(worst, min(confs))
            print("   %-7s %-5d %-24s %-9s %.1f"
                  % (w, len(confs), str(sorted(confs)), str(boxes),
                     sum(ms[1:]) / max(1, len(ms) - 1)))
    print("== 最低置信度 %.3f vs 阈值 %.2f：%s ==" % (
        worst, sf_presence.FACE_GUARD_CONF_MIN,
        "曲线全程在阈值之上 → 降阈值换不到真检出，只会放进假框（不要降）"
        if worst >= sf_presence.FACE_GUARD_CONF_MIN else
        "有小脸掉到阈值之下 → 这才是该讨论阈值的证据"))
    return 0


# ============ 三档合成帧核验：贴边 / 距离 / 暗光（都不碰摄像头）============
#
# 为什么做成三档而不是留在会话里：这三条曲线是"边脸放宽""1.5 米闸门""暗光还守不守得住"
# 这些决定的唯一依据，而每一次都要现场重跑 20 秒才能核对 —— 只活在对话里的实测，
# 下一轮就等于没有。判据一律走**产品同一个** `sf_presence.count_faces`，
# 工具自己绝不重算一遍阈值（否则改了产品口径而工具还绿，正是这个仓反复栽过的
# "闸门自己说假话"那一族）。
#
# 合成帧只写进临时目录、随进程退出删除：素材本身在仓库里，派生图同样不许外传，
# 这一条与隐私硬约束第 1 条同向（不落地、不上传）。

CANVAS_W, CANVAS_H = 640, 480        # 装机版实际采集档（AVCaptureSessionPreset640x480）
FACE_REAL_W = 0.15                   # 成年人脸宽（米），换算"几成 = 几米"用


_TMP_HOLDER: list = []      # 保住 TemporaryDirectory 的引用，进程退出时自动删干净


def _tmpdir(tag):
    """开一个**会自动回收**的临时目录。

    为什么专门写一个：这三档核验会把真人脸的派生图写到磁盘上。第一版用
    `tempfile.mkdtemp` 且从不删，实测跑几轮就在系统临时目录里攒了 113MB 人脸图
    （2026-10-12 二次复核 F3）。本仓的隐私硬约束是"画面内容不落地留档"，
    临时图也必须随进程消失 —— 用 TemporaryDirectory 而不是手写 rmtree，
    这样中途抛异常也不会留下半个目录。
    """
    import tempfile
    d = tempfile.TemporaryDirectory(prefix="sf-fg-%s-" % tag)
    _TMP_HOLDER.append(d)
    return pathlib.Path(d.name)


def _png_read(path):
    """8bit PNG → (宽, 高, RGBA 字节表)。只支持非交错，支持灰度/灰+Alpha/RGB/RGBA。"""
    import struct
    import zlib
    d = pathlib.Path(path).read_bytes()
    if d[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("不是 PNG 文件")
    pos, w, h, bd, ct, idat = 8, 0, 0, 8, 6, b""
    while pos + 8 <= len(d):
        ln = struct.unpack(">I", d[pos:pos + 4])[0]
        typ = d[pos + 4:pos + 8]
        body = d[pos + 8:pos + 8 + ln]
        if typ == b"IHDR":
            w, h, bd, ct = struct.unpack(">IIBB", body[:10])
        elif typ == b"IDAT":
            idat += body
        elif typ == b"IEND":
            break
        pos += 12 + ln
    if bd != 8 or ct not in (0, 2, 4, 6):
        raise ValueError("只支持 8bit 非交错 PNG，实际 bit=%d 类型=%d" % (bd, ct))
    raw = zlib.decompress(idat)
    ch = {0: 1, 2: 3, 4: 2, 6: 4}[ct]
    stride = w * ch
    out = bytearray(w * h * 4)
    prev = bytearray(stride)
    i = 0
    for y in range(h):
        f = raw[i]
        line = bytearray(raw[i + 1:i + 1 + stride])
        i += 1 + stride
        if f == 1:
            for x in range(ch, stride):
                line[x] = (line[x] + line[x - ch]) & 255
        elif f == 2:
            for x in range(stride):
                line[x] = (line[x] + prev[x]) & 255
        elif f == 3:
            for x in range(stride):
                a = line[x - ch] if x >= ch else 0
                line[x] = (line[x] + ((a + prev[x]) >> 1)) & 255
        elif f == 4:
            for x in range(stride):
                a = line[x - ch] if x >= ch else 0
                b = prev[x]
                c = prev[x - ch] if x >= ch else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[x] = (line[x] + pr) & 255
        elif f != 0:
            raise ValueError("不支持的 PNG 滤波类型 %d" % f)
        for x in range(w):
            s = x * ch
            if ch == 4:
                r, g, bl, a = line[s], line[s + 1], line[s + 2], line[s + 3]
            elif ch == 3:
                r, g, bl, a = line[s], line[s + 1], line[s + 2], 255
            elif ch == 2:
                r = g = bl = line[s]
                a = line[s + 1]
            else:
                r = g = bl = line[s]
                a = 255
            o = (y * w + x) * 4
            out[o], out[o + 1], out[o + 2], out[o + 3] = r, g, bl, a
        prev = line
    return w, h, out


def _png_write(path, w, h, rgba):
    import struct
    import zlib
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        raw += rgba[y * w * 4:(y + 1) * w * 4]
    comp = zlib.compress(bytes(raw), 6)

    def chunk(t, b):
        return (struct.pack(">I", len(b)) + t + b
                + struct.pack(">I", zlib.crc32(t + b) & 0xFFFFFFFF))
    p = pathlib.Path(path)
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(
        ">IIBBBBB", w, h, 8, 6, 0, 0, 0)) + chunk(b"IDAT", comp)
        + chunk(b"IEND", b""))
    return p


def _mean(px):
    n = max(1, len(px) // 4)
    return sum(px[i] for i in range(0, len(px), 4)) / n


def _noise(px, sigma, seed=7):
    """亮度噪点：同一个偏移量加到 R/G/B（真实传感器的亮度噪声就是这样）。"""
    if sigma <= 0:
        return bytearray(px)
    import random
    rnd = random.Random(seed)
    gauss = rnd.gauss
    out = bytearray(len(px))
    for i in range(0, len(px), 4):
        n = int(gauss(0.0, sigma))
        for j in range(3):
            out[i + j] = min(255, max(0, px[i + j] + n))
        out[i + 3] = 255
    return out


def _box_blur(px, w, h, r):
    """可分离盒模糊：暗光下曝光时间被拉长，人一动就是这样糊。"""
    if r <= 0:
        return bytearray(px)
    tmp = bytearray(len(px))
    win = 2 * r + 1
    for y in range(h):
        row = y * w * 4
        acc = [0, 0, 0]
        for x in range(-r, r + 1):
            o = row + min(w - 1, max(0, x)) * 4
            for c in range(3):
                acc[c] += px[o + c]
        for x in range(w):
            o = row + x * 4
            for c in range(3):
                tmp[o + c] = acc[c] // win
            tmp[o + 3] = 255
            ol = row + min(w - 1, max(0, x - r)) * 4
            orr = row + min(w - 1, max(0, x + r + 1)) * 4
            for c in range(3):
                acc[c] += px[orr + c] - px[ol + c]
    out = bytearray(len(px))
    for x in range(w):
        col = x * 4
        acc = [0, 0, 0]
        for y in range(-r, r + 1):
            o = min(h - 1, max(0, y)) * w * 4 + col
            for c in range(3):
                acc[c] += tmp[o + c]
        for y in range(h):
            o = y * w * 4 + col
            for c in range(3):
                out[o + c] = acc[c] // win
            out[o + 3] = 255
            ol = min(h - 1, max(0, y - r)) * w * 4 + col
            orr = min(h - 1, max(0, y + r + 1)) * w * 4 + col
            for c in range(3):
                acc[c] += tmp[orr + c] - tmp[ol + c]
    return out


def _prepare(path: str = DEFAULT_IMAGE):
    """取框架 + 素材 + 两张脸的框；任何一样缺就返回 None 和原因（不假装通过）。"""
    cls, why = sf_presence._load_frameworks()
    if cls is None:
        return None, "系统框架不可用：%s" % why
    img = pathlib.Path(path)
    if not img.exists():
        return None, "素材图不存在：%s" % img
    try:
        from Foundation import NSURL
    except Exception as exc:
        return None, "PyObjC/Foundation 不可用：%r" % (exc,)
    req = cls["VNDetectFaceRectanglesRequest"].alloc().init()
    h = cls["VNImageRequestHandler"].alloc().initWithURL_options_(
        NSURL.fileURLWithPath_(str(img)), None)
    if h is None or not h.performRequests_error_([req], None):
        return None, "素材图本身检不出脸：Vision 这半段是坏的"
    boxes = []
    for ob in (req.results() or []):
        b = ob.boundingBox()
        boxes.append((float(ob.confidence()), float(b[0][0]), float(b[1][0])))
    if len(boxes) < 2:
        return None, "素材图上只数出 %d 张脸，做不了「第二个人」的实验" % len(boxes)
    return {"cls": cls, "src": img, "boxes": sorted(boxes, key=lambda t: t[1])}, ""


def _detect(cls, path):
    """真 Vision + **产品同一个** count_faces，返回 (原始框数, 计入张数, 各框把握分)。"""
    from Foundation import NSURL
    req = cls["VNDetectFaceRectanglesRequest"].alloc().init()
    h = cls["VNImageRequestHandler"].alloc().initWithURL_options_(
        NSURL.fileURLWithPath_(str(path)), None)
    if h is None or not h.performRequests_error_([req], None):
        return 0, 0, []
    raw = list(req.results() or [])
    confs = []
    for ob in raw:
        try:
            confs.append(round(float(ob.confidence()), 3))
        except Exception:
            confs.append(-1.0)
    return len(raw), sf_presence.count_faces(raw), sorted(confs)


def _draw(ctx_env, tag, scale=1.0, face_center=None, dark=1.0, td=None):
    """画一帧 640×480：整幅素材缩放/平移到指定位置，并按 `dark` 压暗。

    压暗走的是 `drawInRect:...fraction:` 的 alpha —— 在黑底上 source-over 一个
    alpha=dark 的图层，结果精确等于每个通道乘 dark。为什么不用 AppKit 的 Multiply
    合成：2026-10-12 实测那条路把均值**抬高**了（0.386→0.574），拿它当压暗就是
    在用亮图冒充暗图自证通过（自检里 `_check_darkness` 专门钉这一条）。
    """
    import tempfile
    from AppKit import (NSBezierPath, NSBitmapImageRep, NSCalibratedRGBColorSpace,
                        NSColor, NSGraphicsContext, NSImage)
    td = td or _tmpdir("draw")
    img = NSImage.alloc().initWithContentsOfFile_(str(ctx_env["src"]))
    sw, sh = img.size().width, img.size().height
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, CANVAS_W, CANVAS_H, 8, 4, True, False, NSCalibratedRGBColorSpace, 0, 0)
    g = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    NSGraphicsContext.setCurrentContext_(g)
    NSColor.blackColor().set()
    NSBezierPath.bezierPathWithRect_(((0, 0), (CANVAS_W, CANVAS_H))).fill()
    w, hh = CANVAS_W * scale, CANVAS_H * scale
    if face_center is None:
        x = (CANVAS_W - w) / 2.0
    else:
        cx_r = ctx_env["rface_cx"]
        x = CANVAS_W * face_center - cx_r * w
    # 合成方式必须是 source-over(2)：用 copy(1) 时 fraction 会被**整个忽略**
    # （copy 就是无视 alpha 直接覆盖），"压暗"就变成了拿亮图冒充暗图 —— 这句不是
    # 推理，是 `_check_darkness` 第一次跑就抓到的事实（均值纹丝不动 109.8）。
    img.drawInRect_fromRect_operation_fraction_(
        ((x, (CANVAS_H - hh) / 2.0), (w, hh)), ((0.0, 0.0), (sw, sh)), 2, dark)
    NSGraphicsContext.setCurrentContext_(None)
    p = pathlib.Path(td) / ("%s.png" % tag)
    rep.representationUsingType_properties_(4, None).writeToFile_atomically_(str(p), True)
    return p


def _draw_two(ctx_env, td, owner_px, second_px, second_cx=0.74, tag="",
              sigma=0.0, blur=0):
    """一近一远：主人那张脸固定画大，第二个人那张脸按不同像素宽画在旁边。

    为什么不能用"整幅一起缩小"代替：那会让两张脸同时变小，回答不了
    「主人坐得近、第二个人站在 1.5 米外」这个真实姿势。
    """
    from AppKit import (NSBezierPath, NSBitmapImageRep, NSCalibratedRGBColorSpace,
                        NSColor, NSGraphicsContext, NSImage)
    img = NSImage.alloc().initWithContentsOfFile_(str(ctx_env["src"]))
    sw, sh = img.size().width, img.size().height
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, CANVAS_W, CANVAS_H, 8, 4, True, False, NSCalibratedRGBColorSpace, 0, 0)
    g = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    NSGraphicsContext.setCurrentContext_(g)
    NSColor.blackColor().set()
    NSBezierPath.bezierPathWithRect_(((0, 0), (CANVAS_W, CANVAS_H))).fill()
    for px, box, cx in ((owner_px, ctx_env["boxes"][0], 0.26),
                        (second_px, ctx_env["boxes"][1], second_cx)):
        if px <= 0:
            continue
        pad = 0.75 * box[2]
        x0 = max(0.0, box[1] - pad / 2.0)
        x1 = min(1.0, box[1] + box[2] + pad / 2.0)
        span = x1 - x0
        src = ((x0 * sw, 0.0), (span * sw, sh))
        w = float(px) * span / box[2]
        hh = w * sh / (span * sw)
        img.drawInRect_fromRect_operation_fraction_(
            ((cx * CANVAS_W - w / 2.0, (CANVAS_H - hh) / 2.0), (w, hh)), src, 2, 1.0)
    NSGraphicsContext.setCurrentContext_(None)
    p = pathlib.Path(td) / ("two_%d_%d.png" % (owner_px, second_px))
    rep.representationUsingType_properties_(4, None).writeToFile_atomically_(str(p), True)
    if sigma or blur:
        w, h, px = _png_read(p)
        if blur:
            px = _box_blur(px, w, h, blur)
        px = _noise(px, sigma)
        p = pathlib.Path(td) / ("two_%d_%d_s%d_b%d.png" % (owner_px, second_px,
                                                            sigma, blur))
        _png_write(p, w, h, px)
    return p


def judge_edge(cut_counts, cover_to: float = 0.5) -> str:
    """贴边这一档的结论：被画面边界切掉不超过 `cover_to` 的，每一档都必须算两张。

    口径来自用户 2026-10-12 的拍板「被切一半的边脸也要算第二个人」。
    切更多的档位不参与判定 —— 实测切到 60% 时系统根本不返回那张脸，
    阈值救不了，把不可能写成判据就是一道每轮必红的关。
    """
    for cut, counted in cut_counts:
        if cut <= cover_to and counted < 2:
            return "fail"
    return "pass"


def judge_near(pairs, w_min: float = sf_presence.FACE_GUARD_NEAR_FACE_W) -> str:
    """距离这一档：闸门之上（占画面宽 ≥ w_min，≈1.5 米内）的每一档都必须算两张。

    低于闸门的档位不算失败 —— 那正是"站得远的人不该把窗收掉"这条规矩要的 behavior。
    """
    for ratio, counted in pairs:
        if ratio >= w_min and counted < 2:
            return "fail"
    return "pass"


def judge_lowlight(rows, expect_max_boxes: int = 2) -> str:
    """暗光这一档管两头：暗与糊都不许把第二个人弄没，也不许多造出一张脸。

    后半条是这档真正的价值：噪点一旦凭空多出第三个框，用户看到的就是
    "天一黑行情就自己消失"，那是比漏检更招烦的失效方向。
    """
    for _label, n_boxes, counted in rows:
        if n_boxes > expect_max_boxes:
            return "fail"
        if counted < 2:
            return "fail"
    return "pass"


def judge_edge_floor(pairs, w_min: float = sf_presence.FACE_GUARD_NEAR_FACE_W,
                     shrink: float = sf_presence.FACE_GUARD_EDGE_SHRINK) -> str:
    """贴边的弱尺寸闸门：两头都要判，缺一头就是假门。

    pairs = [(脸的**真实**像素宽, 计入张数)]，这些帧都是"脸中心压在右边界"
    = 被切掉一半，所以框只剩 shrink 倍宽。
      · 真实宽度在闸门之上（近处的人，半张脸出画）→ 必须算第二个人（用户 R1）
      · 真实宽度在闸门之下（远处的人，同样半张脸出画）→ 必须不算（用户 R2）
    只判其中一侧的闸门，另一侧改坏了也不会红。
    """
    near = [n for w, n in pairs if w / sf_PX >= w_min * shrink and w >= 60]
    far = [n for w, n in pairs if w / sf_PX < w_min * shrink]
    if any(n < 2 for n in near):
        return "fail"
    if any(n >= 2 for n in far):
        return "fail"
    return "pass"


sf_PX = CANVAS_W


def _skip(env, why) -> int:
    print("!! 跳过这一档：%s" % why)
    print("   跳过不是通过。框架不可用（非 macOS / 系统框架被裁剪）或素材缺失时，"
          "这一条就没有结论。")
    return 2


def run_edge(path: str = DEFAULT_IMAGE) -> int:
    """贴边曲线：第二个人的脸被画面右边界切掉多少，还算不算第二个人。"""
    import tempfile
    env, why = _prepare(path)
    if env is None:
        return _skip(env, why)
    _, cx, cw = env["boxes"][1]
    env["rface_cx"] = cx + cw / 2.0
    hw = cw / 2.0
    print("== 贴边核验（真 Vision，640×480，判据走产品 count_faces）==")
    print("   被切 50% = 脸中心正好压在画面右边界；切更多就是大半张脸在画外")
    print("   %-10s %-9s %-26s %-26s" % ("切掉", "把握分", "正常光", "暗光 σ=25"))
    td = _tmpdir("edge")
    cut_counts = []
    for cut in (0.0, 0.2, 0.4, 0.5, 0.6, 0.8):
        face_center = 1.0 - hw + 2 * hw * cut
        row = []
        for sigma in (0, 25):
            p = _draw(env, "e%03d_%02d" % (int(cut * 100), sigma),
                      face_center=face_center, td=td)
            w, h, px = _png_read(p)
            q = _png_write(pathlib.Path(td) / ("e%03d_%02d_n.png" % (int(cut * 100),
                                                                      sigma)),
                           w, h, _noise(px, sigma))
            row.append(_detect(env["cls"], q))
        cut_counts.append((cut, row[0][1]))
        cut_counts.append((cut, row[1][1]))
        print("   %3.0f%%      %-9s 正常 %d 张 %-16s 暗 %d 张 %s"
              % (cut * 100, str(row[0][2]), row[0][1], "", row[1][1], str(row[1][2])))
    verdict = judge_edge(cut_counts)
    print("== 结论：%s ==" % ("通过 —— 被切一半的边脸仍算第二个人" if verdict == "pass"
                             else "不通过 —— 半张脸出画的人没被算上，贴边放宽这条没生效"))
    if verdict != "pass":
        print("   先看 sf_presence.count_faces 的贴边分支与 FACE_GUARD_EDGE_CONF_MIN；"
              "这条失败的后果是「有人站在镜头边缘半张脸出画，行情不收」。")
    # 第二幕：贴边 × 距离。被切一半的近脸要算，被切一半的远脸不算 ——
    # 这是"贴边豁免尺寸"唯一能被证伪的形式（2026-10-12 二次复核 F2 的修法）。
    print("\n== 贴边 × 距离：脸中心压在右边界（都被切掉一半），真实宽度由近到远 ==")
    floor_px = sf_presence.FACE_GUARD_NEAR_FACE_W * sf_presence.FACE_GUARD_EDGE_SHRINK \
        * CANVAS_W
    pairs = []
    for px in (160, 90, 60, 45, 32, 26):
        q = _draw_two(env, td, 218, px, second_cx=1.0, tag="ef%03d_" % px)
        n_box, counted, confs = _detect(env["cls"], q)
        pairs.append((px, counted))
        # 标注说清楚"这一档我们要求什么"：只有真实宽度 ≥60px 的近脸才做正断言
        # （再小的脸被切一半后系统根本不返回框，那是检出能力的地板，不是阈值的锅）；
        # 弱闸门之下（<25px 可见宽）做反断言：必须不算第二个人。
        if px >= 60:
            want = "要求算"
        elif px < floor_px:
            want = "要求不算"
        else:
            want = "不要求（切半后已低于检出地板）"
        print("   真实脸宽 %3dpx（占 %4.1f%%）→ 框 %d 个 计入 %d 张 conf=%-22s %s"
              % (px, 100.0 * px / CANVAS_W, n_box, counted, str(confs), want))
    v2 = judge_edge_floor(pairs)
    print("== 贴边弱闸门结论：%s ==" % ("通过 —— 近处半张脸算、远处半张脸不算"
                                     if v2 == "pass" else "不通过 —— 见上面哪一行反了"))
    if verdict != "pass" or v2 != "pass":
        return 1
    return 0


def run_near(path: str = DEFAULT_IMAGE) -> int:
    """距离曲线：主人坐在正常距离，第二个人的脸由近到远，几成开始不算第二个人。"""
    import tempfile
    env, why = _prepare(path)
    if env is None:
        return _skip(env, why)
    td = _tmpdir("near")
    print("== 一近一远核验：主人固定占画面 34%，第二个人由近到远 ==")
    print("   闸门 FACE_GUARD_NEAR_FACE_W=%.3f（≈1.5 米内才算第二个人；"
          "换成米要靠镜头视角，本机实测读不到）"
          % sf_presence.FACE_GUARD_NEAR_FACE_W)
    pairs = []
    for px in (240, 160, 128, 96, 72, 56, 42, 32, 26, 22, 18):
        p = _draw_two(env, td, 218, px)
        n_box, counted, confs = _detect(env["cls"], p)
        ratio = px / CANVAS_W
        pairs.append((ratio, counted))
        print("   脸宽 %3dpx 占 %5.1f%% → 框 %d 个 计入 %d 张 conf=%s"
              % (px, ratio * 100, n_box, counted, str(confs)))
    verdict = judge_near(pairs)
    print("== 距离结论：%s ==" % ("通过 —— 闸门之内每一档都算两张" if verdict == "pass"
                                else "不通过 —— 闸门之内有档位漏人，尺寸闸门与检测能力打架"))
    print("   换算参考：占比 f 对应的距离 = 脸宽 %.2fm / (2·f·tan(视角/2))；"
          "1.5 米处 f≈8.7%%(视角60°) / 7.1%%(70°) / 6.5%%(75°)" % FACE_REAL_W)
    print("   标定办法：量好坐姿距离，读诊断包 face_guard.json 的 max_face_width，"
          "反解视角后重算 FACE_GUARD_NEAR_FACE_W")
    return 0 if verdict == "pass" else 1


def run_lowlight(path: str = DEFAULT_IMAGE) -> int:
    """暗光曲线：自动增益后的成品帧长什么样 —— 噪点与运动模糊，而不是单纯的暗。"""
    import tempfile
    env, why = _prepare(path)
    if env is None:
        return _skip(env, why)
    td = _tmpdir("dark")
    base = _draw(env, "base", td=td)
    w, h, px = _png_read(base)
    print("== 暗光核验（亮度已归一到原图水平，差别只在噪点与糊）==")
    rows = []
    for sigma in (0, 10, 25, 40, 55):
        q = _png_write(pathlib.Path(td) / ("n%03d.png" % sigma), w, h, _noise(px, sigma))
        n_box, counted, confs = _detect(env["cls"], q)
        rows.append(("σ=%d" % sigma, n_box, counted))
        print("   噪点 σ=%-4d → 框 %d 个 计入 %d 张 conf=%s"
              % (sigma, n_box, counted, str(confs)))
    for r in (3, 8):
        q = _png_write(pathlib.Path(td) / ("b%02d.png" % r), w, h, _box_blur(px, w, h, r))
        n_box, counted, confs = _detect(env["cls"], q)
        rows.append(("模糊 %dpx" % r, n_box, counted))
        print("   运动模糊 %-3dpx → 框 %d 个 计入 %d 张 conf=%s"
              % (r, n_box, counted, str(confs)))
    verdict = judge_lowlight(rows)
    print("== 暗光结论：%s ==" % ("通过 —— 关灯后这一档仍然守得住，也没有多造出脸"
                                if verdict == "pass" else "不通过 —— 见上面哪一行先失守"))
    print("   已知边界（只报不判）：σ=90 仍可数出两张，σ=120 起第二个人就没了 —— "
          "那是接近雪花屏的极暗场景，不在判据内")
    return 0 if verdict == "pass" else 1


def _check_darkness() -> list:
    """压暗必须真的把均值按比例降下来。

    这一条是给上面三档兜底的：2026-10-12 我先用 AppKit 的 Multiply 合成压暗，
    实测均值反而从 0.386 升到 0.574 —— 那种核验器会拿亮图当暗图自证"暗光通过"。
    所以判据不能是"函数调用了"，只能是"像素真的变暗了"。
    """
    import tempfile
    bad = []
    env, why = _prepare()
    if env is None:
        return ["贴不了暗：框架或素材不可用（%s）" % why]
    td = _tmpdir("dk")
    ref = _draw(env, "dk_ref", td=td)
    _, _, px_ref = _png_read(ref)
    m_ref = _mean(px_ref)
    for dark in (0.5, 0.25, 0.06):
        p = _draw(env, "dk_%03d" % int(dark * 100), dark=dark, td=td)
        _, _, px = _png_read(p)
        got = _mean(px)
        if not (m_ref * dark * 0.8 <= got <= m_ref * dark * 1.25):
            bad.append("压暗到 ×%.2f 后实测均值 %.1f，不在期望 %.1f 的 ±25%% 内 —— "
                       "亮度类核验会拿亮图冒充暗图" % (dark, got, m_ref * dark))
    return bad


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="人脸在场守卫实机核验")
    ap.add_argument("--curve", action="store_true",
                    help="打「脸像素数 → 置信度」曲线，用来判阈值该不该动")
    ap.add_argument("--edge", action="store_true",
                    help="打「脸被画面边界切掉多少 → 还算几张」曲线（不碰摄像头）")
    ap.add_argument("--near", action="store_true",
                    help="打「一近一远：第二个人的脸占画面宽几成 → 还算几张」曲线")
    ap.add_argument("--lowlight", action="store_true",
                    help="打「噪点/运动模糊 → 还算几张」曲线（暗光的真实样子）")
    ap.add_argument("--seconds", type=float, default=12.0, help="采集观察时长")
    ap.add_argument("--image", nargs="?", const=DEFAULT_IMAGE, default=None,
                    metavar="PNG", help="用静态图核验 Vision 链（不碰摄像头、不要授权）")
    ap.add_argument("--expect-min", type=int, default=2,
                    help="静态图至少要数出几张脸才算通过（默认 2）")
    ap.add_argument("--self-test", action="store_true", help="不碰摄像头的结构自检")
    args = ap.parse_args(argv)
    if args.self_test:
        return self_test()
    if args.curve:
        return run_curve()
    if args.edge:
        return run_edge()
    if args.near:
        return run_near()
    if args.lowlight:
        return run_lowlight()
    if args.image:
        return run_image(args.image, args.expect_min)
    return run(args.seconds)


if __name__ == "__main__":
    sys.exit(main())
