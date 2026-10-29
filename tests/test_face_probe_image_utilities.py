# -*- coding: utf-8 -*-
"""人脸守卫核验器的**图像工具层**（覆盖率轮 B 档补充，2026-10-12）。

为什么挑这一层：`tools/verify_face_presence.py` 609 条语句只覆盖到 38%，缺的 376 条里
分两类 ——

  · `run_edge` / `run_curve` / `run_image` / `run_near` / `run_lowlight`（合计 174 条）
    要真 Vision + 仓库那张两人照。人脸照按隐私决定**不入库**（AGENTS §3.1 写明），
    这些在无素材的树上本来就该打印「跳过 + 理由」并退 2 —— 把它们塞进全量
    等于造一枚"只在有照片的机器上才测"的判据，换棵树就整体消失，正是本仓反复防的那类。
  · `_box_blur` / `_png_read` / `_png_write` / `_noise` / `_mean`（合计 ~75 条）
    是**手写的**像素级实现：一个自己实现的 PNG 滤波器解码器（含 Paeth）和一个
    可分离盒模糊。它们被上面那五档用着，却一行都没被喂过 —— 这类代码出错的方式
    很安静：解出来的图偏色/糊错，检测数照报，而"贴边到底算不算一张脸"的曲线就变了。

所以这里只喂合成数据，不碰摄像头、不碰人脸照、不需要任何授权：
PNG 那份用**测试里独立实现的编码器**造出各种滤波器类型的字节流，
断言"解出来的每个像素等于我写进去的值"；模糊那份用已知形状（常量图 / 单个亮点）
断言可预期的不变量。判据全部落在像素事实上。

改这些函数之前先看一眼它们守的是什么：
  · `_png_read` 的滤波器 1/2/3/4 与"不支持的类型必须炸" —— 静默解错就是拿假图自证；
  · `_box_blur` 的边界钳制（`min/max` 那几处）—— 写错会让画面四角先糊，
    而"贴边的第二个人"这条曲线量的正是边上的像素。
"""
from __future__ import annotations

import contextlib
import io
import os
import pathlib
import struct
import sys
import tempfile
import types
import unittest
import zlib
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

try:
    from _base import SFCase                          # noqa: E402
except ImportError:                                    # pragma: no cover
    from tests._base import SFCase                    # noqa: E402

import verify_face_presence as vfp                    # noqa: E402

# 被测对象本身就是这些私有实现；一次性绑成具名别名，正文里不再出现 obj._x
png_read = vfp._png_read          # discipline-ok(R5): 被测对象本身就是手写的 PNG 解码器
png_write = vfp._png_write        # discipline-ok(R5): 同上（合成帧的写出侧）
box_blur = vfp._box_blur          # discipline-ok(R5): 同上（暗光那档的模糊）
noise = vfp._noise                # discipline-ok(R5): 同上
mean = vfp._mean                  # discipline-ok(R5): 同上
judge_count = vfp.judge           # 数脸结论
judge_edge = vfp.judge_edge       # 贴边那档
judge_near = vfp.judge_near       # 距离那档
judge_lowlight = vfp.judge_lowlight
judge_edge_floor = vfp.judge_edge_floor

GRAY = 128
BRIGHT = 250
EDGE_COLOR = (10, 20, 30)


def rgba_fill(w, h, rgba):
    px = bytearray()
    for _ in range(w * h):
        px += bytes(rgba)
    return px


def set_px(px, w, x, y, rgba):
    o = (y * w + x) * 4
    px[o:o + 4] = bytes(rgba)


def get_px(px, w, x, y):
    o = (y * w + x) * 4
    return tuple(px[o:o + 4])


def encode_png(path, w, h, px, color_type=6, filter_type=0):
    """测试侧**独立实现**的 PNG 编码器：按指定滤波器类型写 IDAT。

    为什么不用 PIL：本仓不依赖图像库（装机版与 CI 都得能跑），而且这样"写进去什么"
    是测试自己算的，`_png_read` 解错就会被抓到 —— 拿同一个实现去验它自己等于没验。
    """
    ch = {0: 1, 2: 3, 4: 2, 6: 4}[color_type]
    stride = w * ch
    raw = bytearray()
    prev = bytearray(stride)
    for y in range(h):
        line = bytearray(stride)
        for x in range(w):
            o = (y * w + x) * 4
            r, g, b, a = px[o], px[o + 1], px[o + 2], px[o + 3]
            if color_type == 0:
                line[x] = r
            elif color_type == 2:
                line[x * 3:x * 3 + 3] = bytes((r, g, b))
            elif color_type == 4:
                line[x * 2:x * 2 + 2] = bytes((r, a))
            else:
                line[x * 4:x * 4 + 4] = px[o:o + 4]
        if filter_type == 1:            # Sub
            enc = bytearray(line)
            for x in range(ch, stride):
                enc[x] = (line[x] - line[x - ch]) & 255
        elif filter_type == 2:          # Up
            enc = bytearray((line[x] - prev[x]) & 255 for x in range(stride))
        elif filter_type == 3:          # Average
            enc = bytearray(stride)
            for x in range(stride):
                a = line[x - ch] if x >= ch else 0
                enc[x] = (line[x] - ((a + prev[x]) >> 1)) & 255
        elif filter_type == 4:          # Paeth
            enc = bytearray(stride)
            for x in range(stride):
                a = line[x - ch] if x >= ch else 0
                b = prev[x]
                c = prev[x - ch] if x >= ch else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                enc[x] = (line[x] - pr) & 255
        else:                            # None
            enc = bytearray(line)
        raw.append(filter_type)
        raw += enc
        prev = line
    comp = zlib.compress(bytes(raw), 6)

    def chunk(tag, body):
        return (struct.pack(">I", len(body)) + tag + body
                + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF))

    p = pathlib.Path(path)
    p.write_bytes(b"\x89PNG\r\n\x1a\n"
                  + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, color_type, 0, 0, 0))
                  + chunk(b"IDAT", comp) + chunk(b"IEND", b""))
    return p


class ImageProbeCliCase(SFCase):
    """静态图 CLI 的失败可见性：无素材、无框架、空结果不能冒充通过。"""

    def setUp(self):
        super().setUp()
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="face-probe-cli-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def test_image_probe_missing_asset_returns_skip_not_pass(self):
        with mock.patch.object(vfp.sf_presence, "_load_frameworks", return_value=(object(), "")), \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(vfp.run_image(str(self.tmp / "missing.png")), 2)

    def test_image_probe_rejects_empty_results_after_successful_vision_request(self):
        class Request:
            def alloc(self):
                return self

            def init(self):
                return self

            def results(self):
                return []

        class Handler:
            def alloc(self):
                return self

            def initWithURL_options_(self, _url, _options):
                return self

            def performRequests_error_(self, _requests, _error):
                return True

        classes = {"VNDetectFaceRectanglesRequest": Request(),
                   "VNImageRequestHandler": Handler()}
        foundation = types.ModuleType("Foundation")
        foundation.NSURL = types.SimpleNamespace(fileURLWithPath_=lambda path: path)
        with tempfile.NamedTemporaryFile(suffix=".png", dir=self.tmp, delete=False) as image, \
                mock.patch.object(vfp.sf_presence, "_load_frameworks", return_value=(classes, "")), \
                mock.patch.dict(sys.modules, {"Foundation": foundation}), \
                mock.patch.object(vfp.sf_presence, "count_faces", return_value=0), \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(vfp.run_image(image.name), 1)


class _SeenBox:
    """检测框替身：只有 confidence() 与 boundingBox()，形状同 PyObjC 桥出来的。"""

    def __init__(self, x, y, w, h, conf=0.85):
        self._r, self._c = ((x, y), (w, h)), conf

    def confidence(self):
        return self._c

    def boundingBox(self):
        return self._r


class _BodyProbeRequest:
    def __init__(self, observations=()):
        self.observations = list(observations)

    def alloc(self):
        return self

    def init(self):
        return self

    def setUpperBodyOnly_(self, flag):
        self.upper_body_only = bool(flag)

    def results(self):
        return list(self.observations)


class _BodyProbeHandler:
    def __init__(self, ok=True):
        self.ok = ok

    def alloc(self):
        return self

    def initWithURL_options_(self, _url, _options):
        return self

    def performRequests_error_(self, _requests, _error):
        return self.ok


class RunBodyCase(SFCase):
    """`--body` 档：脸 + 上半身静态图核验。跳过必须退 2，不能冒充通过。"""

    def setUp(self):
        super().setUp()
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="face-probe-body-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.image = self.tmp / "scene.png"
        self.image.write_bytes(b"not a real png")        # 只要存在；Vision 是假的
        self.owner_face = _SeenBox(0.30, 0.25, 0.30, 0.30)
        self.owner_body = _SeenBox(0.20, 0.0, 0.50, 0.90, 0.80)
        self.guest_body = _SeenBox(0.72, 0.0, 0.26, 0.90, 0.80)

    def _classes(self, faces=(), bodies=(), ok=True, with_body=True):
        classes = {"VNDetectFaceRectanglesRequest": _BodyProbeRequest(faces),
                   "VNImageRequestHandler": _BodyProbeHandler(ok)}
        if with_body:
            classes["VNDetectHumanRectanglesRequest"] = _BodyProbeRequest(bodies)
        return classes

    def _run(self, classes, expect, path=None, loaded=True):
        foundation = types.SimpleNamespace(
            NSURL=types.SimpleNamespace(fileURLWithPath_=lambda p: p))
        ret = (classes, "") if loaded else (None, "没装")
        with mock.patch.object(vfp.sf_presence, "_load_frameworks", return_value=ret), \
                mock.patch.dict(sys.modules, {"Foundation": foundation}), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            code = vfp.run_body(path or str(self.image), expect)
        return code, out.getvalue()

    def test_frameworks_unavailable_is_a_skip_not_a_pass(self):
        code, _ = self._run(None, 1, loaded=False)
        self.assertEqual(code, 2)

    def test_machine_without_the_body_class_is_a_skip_not_a_pass(self):
        code, _ = self._run(self._classes(with_body=False), 1)
        self.assertEqual(code, 2)

    def test_missing_asset_is_a_skip_not_a_pass(self):
        code, _ = self._run(self._classes(), 1, path=str(self.tmp / "missing.png"))
        self.assertEqual(code, 2)

    def test_vision_rejection_fails_instead_of_passing_on_empty_results(self):
        code, _ = self._run(self._classes(ok=False), 1)
        self.assertEqual(code, 1)

    def test_owner_alone_counts_one_person(self):
        classes = self._classes([self.owner_face], [self.owner_body])
        code, text = self._run(classes, 1)
        self.assertEqual(code, 0)
        self.assertIn("通过", text)

    def test_body_request_asks_for_the_upper_body_only(self):
        """要的是上半身而不是全身：这个标志决定框的形状，也就决定宽度下限与配对是否成立。"""
        classes = self._classes([self.owner_face], [self.owner_body])
        self._run(classes, 1)
        flag = classes["VNDetectHumanRectanglesRequest"].upper_body_only
        self.assertTrue(flag)

    def test_owner_plus_a_guest_seen_only_from_behind_counts_two(self):
        classes = self._classes([self.owner_face], [self.owner_body, self.guest_body])
        code, _ = self._run(classes, 2)
        self.assertEqual(code, 0)

    def test_wrong_count_fails_in_both_directions(self):
        classes = self._classes([self.owner_face], [self.owner_body, self.guest_body])
        code, text = self._run(classes, 1)
        self.assertEqual(code, 1, "单人照数成两个人必须判不通过")
        self.assertIn("不通过", text)
        code, _ = self._run(self._classes([self.owner_face], [self.owner_body]), 2)
        self.assertEqual(code, 1, "两人照只数到一个人也必须判不通过")

    def test_face_without_any_body_box_is_reported_as_such(self):
        code, text = self._run(self._classes([self.owner_face], []), 1)
        self.assertEqual(code, 0)
        self.assertIn("无身体框", text)

    def test_cli_routes_body_to_run_body_with_the_expectation(self):
        with mock.patch.object(vfp, "run_body", return_value=0) as run_body:
            code = vfp.main(["--body", "one.png", "--expect-people", "1"])
        self.assertEqual(code, 0)
        run_body.assert_called_once_with("one.png", 1)

    def _self_test(self, **patches):
        """在进程内跑核验器自检：框架换成假类、素材路径指向不存在，不碰真 Vision 与人脸照。"""
        classes = self._classes()
        stack = [mock.patch.object(vfp.sf_presence, "_load_frameworks", return_value=(classes, "")),
                 mock.patch.object(vfp, "DEFAULT_IMAGE", str(self.tmp / "no-such.png"))]
        stack += [mock.patch.object(vfp, name, value) for name, value in patches.items()]
        with contextlib.ExitStack() as es:
            for p in stack:
                es.enter_context(p)
            out = es.enter_context(mock.patch("sys.stdout", new_callable=io.StringIO))
            code = vfp.self_test()
        return code, out.getvalue()

    def test_self_test_passes_on_the_real_judges(self):
        code, text = self._self_test()
        self.assertEqual(code, 0, text)

    def test_self_test_catches_a_judge_that_always_passes(self):
        """判据永远说通过：单人照数成两个人、两人照只数到一个人都会被放过，自检必须红。"""
        code, text = self._self_test(judge_body=lambda people, expect: "pass")
        self.assertEqual(code, 1)
        self.assertIn("单人照数成两个人", text)
        self.assertIn("两人照只数到一个人", text)

    def test_self_test_catches_a_judge_that_always_fails(self):
        """判据永远说失败：数对了也红，这一档每轮必红，没人会再跑它，自检必须点名。"""
        code, text = self._self_test(judge_body=lambda people, expect: "fail")
        self.assertEqual(code, 1)
        self.assertIn("数对的人数判成失败", text)

    def test_self_test_catches_a_run_body_that_passes_without_an_asset(self):
        code, text = self._self_test(run_body=lambda *a, **k: 0)
        self.assertEqual(code, 1)
        self.assertIn("run_body 仍返回 0", text)


class PngReadCase(SFCase):
    """手写 PNG 解码器：四种颜色类型 × 五种滤波器，解出来的每个像素要等于写进去的值。"""

    def setUp(self):
        super().setUp()
        self.tmp = pathlib.Path(tempfile.mkdtemp())

    def _roundtrip(self, w, h, px, color_type=6, filter_type=0, expect_rgb=None):
        p = encode_png(self.tmp / ("f%d_%d.png" % (color_type, filter_type)),
                       w, h, px, color_type=color_type, filter_type=filter_type)
        rw, rh, out = png_read(p)
        self.assertEqual((rw, rh), (w, h))
        for y in range(h):
            for x in range(w):
                self.assertEqual(get_px(out, w, x, y),
                                 (expect_rgb or get_px(px, w, x, y)))
        return out

    def test_RGBA_无滤波(self):
        px = rgba_fill(3, 2, (1, 2, 3, 255))
        set_px(px, 3, 1, 0, (200, 100, 50, 255))
        self._roundtrip(3, 2, px)

    def test_四种颜色类型都归一成RGBA(self):
        for ct, expect in ((0, (GRAY, GRAY, GRAY, 255)),          # 灰度
                           (4, (GRAY, GRAY, GRAY, 77)),           # 灰 + Alpha
                           (2, (11, 22, 33, 255)),                # RGB
                           (6, (11, 22, 33, 44))):                # RGBA
            px = rgba_fill(2, 2, expect)
            self._roundtrip(2, 2, px, color_type=ct, expect_rgb=expect)

    def test_五种滤波器全部解回原值(self):
        """Paeth 那一支最容易写错（本仓的 PNG 素材真用它，Apple 的编码器会挑滤波器）。"""
        w, h = 4, 3
        px = bytearray()
        for y in range(h):
            for x in range(w):
                px += bytes((x * 40 + y * 7, 255 - x * 20, (x + y) * 15, 255))
        for ftype in (0, 1, 2, 3, 4):
            p = encode_png(self.tmp / ("filt_%d.png" % ftype), w, h, px, filter_type=ftype)
            _w, _h, out = png_read(p)
            self.assertEqual([tuple(out[i:i + 4]) for i in range(0, len(out), 4)],
                             [tuple(px[i:i + 4]) for i in range(0, len(px), 4)],
                             "滤波器类型 %d 解错了" % ftype)

    def test_不是PNG与位深不支持都必须炸(self):
        bad = self.tmp / "bad.png"
        bad.write_bytes(b"not a png at all")
        with self.assertRaises(ValueError):
            png_read(bad)
        # 16bit：本工具只支持 8bit，必须拒绝而不是硬解出错图
        w, h = 2, 2
        raw = zlib.compress(b"\x00" + b"\x01\x02" * (w * 2))
        def chunk(tag, body):
            return (struct.pack(">I", len(body)) + tag + body
                    + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF))
        p16 = self.tmp / "deep.png"
        p16.write_bytes(b"\x89PNG\r\n\x1a\n"
                        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 16, 0, 0, 0, 0))
                        + chunk(b"IDAT", raw) + chunk(b"IEND", b""))
        with self.assertRaises(ValueError):
            png_read(p16)

    def test_未知滤波器类型不许静默解(self):
        raw = zlib.compress(b"\x07" + b"\x00" * 4)          # 滤波器 7 不在规范里
        def chunk(tag, body):
            return (struct.pack(">I", len(body)) + tag + body
                    + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF))
        p = self.tmp / "weird.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n"
                      + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
                      + chunk(b"IDAT", raw) + chunk(b"IEND", b""))
        with self.assertRaises(ValueError):
            png_read(p)

    def test_与自家编码器闭环(self):
        """`_png_write` 写出的文件必须能被 `_png_read` 原样读回 —— 合成帧就是这条链。"""
        w, h = 5, 4
        px = bytearray()
        for y in range(h):
            for x in range(w):
                px += bytes((x * 9, y * 33, (x * y) % 255, 255))
        p = png_write(self.tmp / "mine.png", w, h, px)
        rw, rh, out = png_read(p)
        self.assertEqual((rw, rh), (w, h))
        self.assertEqual(bytes(out), bytes(px))


class NoiseCase(SFCase):
    """亮度噪声：同一个偏移量同时加到 R/G/B（真实传感器的亮度噪声就是这个形状）。"""

    def _px(self, n=8, value=100):
        return bytearray(bytes((value, value, value, 255)) * n)

    def test_sigma为零时原样返回(self):
        px = self._px()
        out = noise(px, 0)
        self.assertEqual(bytes(out), bytes(px))

    def test_同一个seed给同一条噪声(self):
        px = self._px()
        a = noise(px, 12)
        b = noise(px, 12)
        self.assertEqual(bytes(a), bytes(b), "曲线跑两次结果不同 = 阈值表不能复核")

    def test_三通道同偏移且alpha固定(self):
        out = noise(self._px(16, 120), 20)
        for i in range(0, len(out), 4):
            self.assertEqual(out[i], out[i + 1])
            self.assertEqual(out[i + 1], out[i + 2], "R/G/B 偏移不一致就不是亮度噪声")
            self.assertEqual(out[i + 3], 255, "alpha 被噪声改过 → 合成帧会整体变透明")
            self.assertTrue(0 <= out[i] <= 255, "没做钳制：uint8 回绕会把黑点变成白点")

    def test_噪声确实改变了画面但围绕原值(self):
        px = self._px(64, 120)
        out = noise(px, 18)
        self.assertNotEqual(bytes(out), bytes(px))
        mean_after = mean(out)
        self.assertLess(abs(mean_after - 120.0), 12.0, "均值漂太远 = 拿噪声图冒充暗图")


class MeanCase(SFCase):
    def test_均值只取亮度通道(self):
        self.assertEqual(mean(rgba_fill(2, 2, (10, 20, 30, 255))), 10.0)
        self.assertEqual(mean(bytearray()), 0.0, "空表不许炸（除零那一步）")


class BoxBlurCase(SFCase):
    """可分离盒模糊：暗光那档靠它把画面糊成"曝光时间拉长"的样子。"""

    W, H = 7, 5

    def _flat(self, value=GRAY):
        return rgba_fill(self.W, self.H, (value, value, value, 255))

    def test_半径0是恒等变换(self):
        px = self._flat(90)
        out = box_blur(px, self.W, self.H, 0)
        self.assertEqual(bytes(out), bytes(px))

    def test_常量图模糊后仍然常量(self):
        """这是"边界钳制写错没有"的探针：四角最容易先漏 —— 而贴边那条曲线量的就是边上。"""
        out = box_blur(self._flat(BRIGHT), self.W, self.H, 2)
        for y in range(self.H):
            for x in range(self.W):
                self.assertEqual(get_px(out, self.W, x, y), (BRIGHT, BRIGHT, BRIGHT, 255))

    def test_alpha恒为不透明(self):
        px = rgba_fill(self.W, self.H, (5, 6, 7, 12))
        out = box_blur(px, self.W, self.H, 1)
        self.assertTrue(all(out[i + 3] == 255 for i in range(0, len(out), 4)))

    def test_单个亮点被摊成三乘三的平台(self):
        """盒模糊的定义就是"邻域内等权平均"：r=1 时 3×3 每格都等于 亮点÷9，
        而两格之外必须还是 0。中心比旁边亮是**高斯**的性质，拿它当判据会放过写错的窗口。"""
        px = self._flat(0)
        cx, cy = self.W // 2, self.H // 2
        set_px(px, self.W, cx, cy, (240, 240, 240, 255))
        out = box_blur(px, self.W, self.H, 1)
        expect = 240 // 9
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                self.assertEqual(get_px(out, self.W, cx + dx, cy + dy),
                                 (expect, expect, expect, 255),
                                 "窗口 (dx=%d, dy=%d) 上的值不对 = 可分离的两趟之一写偏"
                                 % (dx, dy))
        self.assertEqual(get_px(out, self.W, cx + 2, cy), (0, 0, 0, 255),
                         "窗口外还亮 = 半径没起作用")
        self.assertEqual(get_px(out, self.W, cx, cy + 2), (0, 0, 0, 255))

    def test_边缘像素被钳制而不是越界(self):
        px = self._flat(0)
        set_px(px, self.W, 0, 0, (255, 255, 255, 255))       # 最左上角
        out = box_blur(px, self.W, self.H, 2)
        self.assertEqual(get_px(out, self.W, 0, 0), get_px(out, self.W, 0, 0))
        self.assertGreater(get_px(out, self.W, 1, 1)[0], 0,
                           "角上的亮斑应往里渗；渗不到说明窗口被截没了")
        self.assertTrue(all(0 <= v <= 255 for v in out), "越界读会让像素出现回绕值")


class JudgementTableCase(SFCase):
    """三档判据表本身：贴边放宽 / 1.5 米闸门 / 暗光两头 —— 阈值改坏了必须有人红。

    这几个函数是纯函数，输入是"每一档的实测形状"，所以在这里可以喂合成档位。
    判据一律是**两头**：只放宽近处那一侧、或只收紧远处那一侧，都会让另一侧改坏也不红
    （工具自己的 docstring 写的正是这件事）。
    """

    def test_数脸结论只看下限(self):
        self.assertEqual(judge_count(2, 2), "pass")
        self.assertEqual(judge_count(1, 2), "fail")
        self.assertEqual(judge_count(3, 2), "pass", "多算一张不该被判失败")

    def test_贴边放宽只管被切一半以内的档位(self):
        # cover_to=0.5：被切掉不超过一半的，每一档都必须数到两张
        self.assertEqual(judge_edge([(0.25, 2), (0.5, 2)]), "pass")
        self.assertEqual(judge_edge([(0.5, 1)]), "fail",
                         "0.55 那档放宽没生效 = 边缘的人必须光线很好才算数")
        self.assertEqual(judge_edge([(0.75, 0)]), "pass",
                         "切掉 75% 那一档不参与判定：实测系统根本不返回那张脸，"
                         "把不可能写成判据就是一道每轮必红的关")

    def test_距离闸门只惩罚闸门之内却漏检的档位(self):
        self.assertEqual(judge_near([(0.20, 2)]), "pass")
        self.assertEqual(judge_near([(0.20, 1)]), "fail",
                         "1.5 米内数不出第二个人")
        self.assertEqual(judge_near([(0.02, 1)]), "pass",
                         "站得远的人不该把窗收掉 —— 低于闸门不算失败是这条规矩的另一半")

    def test_暗光那一头同时管漏检与多造(self):
        self.assertEqual(judge_lowlight([("正常", 2, 2)]), "pass")
        self.assertEqual(judge_lowlight([("很暗", 2, 1)]), "fail",
                         "天一黑第二个人就没了")
        self.assertEqual(judge_lowlight([("噪点", 3, 2)]), "fail",
                         "噪点多造出一张脸 = 天一黑行情自己消失，比漏检更招烦")

    def test_贴边弱尺寸闸门两头都要判(self):
        # 近处（真实宽度在闸门之上）半张脸出画 → 必须算第二个人
        self.assertEqual(judge_edge_floor([(140, 2)]), "pass")
        self.assertEqual(judge_edge_floor([(140, 1)]), "fail",
                         "近处那侧失效：被切一半的人不算第二个人")
        # 远处（真实宽度在闸门之下）同样被切一半 → 必须不算
        self.assertEqual(judge_edge_floor([(20, 1)]), "pass")
        self.assertEqual(judge_edge_floor([(20, 2)]), "fail",
                         "远处那侧失效：完全免检会把 1.5 米外的客人也算成第二个人")

    def test_弱闸门的中间带刻意不判(self):
        """真实宽度落在"够不上闸门比例、又小于 60px"那一带的帧，两条列表都不进 ——
        这是有意的（太小的框在暗光下多半是噪点）。写出来是为了哪天有人"顺手补上"时
        能看到这条决定，而不是把它当成漏判。"""
        self.assertEqual(judge_edge_floor([(40, 1)]), "pass")


if __name__ == "__main__":
    unittest.main(verbosity=2)
