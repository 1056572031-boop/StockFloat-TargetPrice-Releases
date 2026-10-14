# -*- coding: utf-8 -*-
"""模块化拆分 v1.2.13：sf_utils —— 由 stock_float_window.py 搬移生成，行为零变化。"""

import math


def fmt_pct(v):
    """涨跌幅格式化：涨带+，跌带-，固定两位小数，平为0.00%

    v1.2.66：固定两位小数（不再去末尾 0），如 0.50→"+0.50%"、10.0→"+10.00%"；
    非有限数（inf/nan）统一归零为 "0.00%"，避免渲染出 "+inf%" 畸形文本。
    百分数语义不变：入参 v 已是「百分点」值（v=0.50 表示 0.50%，非 50%）。
    """
    # 边界防御：非有限数（inf/-inf/nan）一律归零，安全优先
    if not math.isfinite(v):
        return "0.00%"
    # v1.3.42 第6轮走查：极端大数（1e308 这类脏数据）用 "%.2f" 会展开成 300 多位
    # 字符，画到 184px 宽的悬浮窗里就是一条撑破界面的横幅。与 targets._fmt_target
    # 的 R41 口径一致：超过千位档改用科学计数法，正常涨跌幅不受影响（|v|<=1000）。
    if abs(v) > 1000.0:
        return "%+.2e%%" % v
    # v1.2.12：微小涨跌幅（|v|<0.005）四舍五入归零前直接判平，
    # 避免出现 "+%"/"-%" 畸形文本（如 +0.002%）
    if abs(v) < 0.005:
        return "0.00%"
    if v > 0:
        return "+%.2f%%" % v
    elif v < 0:
        return "-%.2f%%" % abs(v)
    else:
        return "0.00%"


def _drop_flash_due(pct, threshold, last_flash_at, now, cooldown):
    """v1.2.58 引入；2026-09-03 收窄为「仅下跌触发」：判断涨跌幅档位是否应触发闪烁提醒（纯函数，便于单测）。
    规则：pct 为负且 |pct| >= threshold（即 pct <= -threshold）且该档从未触发过（last_flash_at 为 None）
    或距上次触发已超过冷却期，才触发。上涨（pct>0）与平盘（pct==0）一律不闪。
    任何异常一律返回 False（安全优先，不打断行情刷新）。"""
    try:
        # v1.3.42 第6轮走查：NaN 必须显式挡掉。此前只靠 `pct > -threshold` 比较，
        # 而 NaN 的任何比较恒为 False —— 于是 NaN 一路走到末尾返回 True，
        # 表现为行情出脏数据（接口返回空/除零）时窗口**莫名其妙闪一下红**。
        # 注意 None / "bad" 会因 TypeError 落到 except 返回 False，同样安全。
        if isinstance(pct, bool) or not isinstance(pct, (int, float)):
            return False
        if not math.isfinite(pct):
            return False
        # +1e-9 是浮点容差：跌幅恰好等于阈值时（如 pct=-0.1、threshold=0.1）
        # 二进制误差可能让比较成立，从而漏掉一次该闪的档位。
        if pct > -threshold + 1e-9:  # 仅下跌触发：上涨/平盘/跌幅不足均不闪
            return False
        if last_flash_at is not None and (now - last_flash_at) < cooldown:
            return False
        return True
    except Exception:
        return False


def market_prefix(raw6):
    """6 位 A 股代码 → 交易所前缀（"sh" / "sz" / "bj"）；无法判定时返回空串。

    v1.2.65 第1轮走查：这是「代码 → 交易所」的**唯一实现**。
    此前存在两份且都不完整（normalize_code 的 6 位分支 + sf_quote._tencent_code
    的裸代码兜底），后果是：

    - 5 / 1 / 2 / 7 开头的**基金与 ETF** 代码两边都不认识，裸代码最终被
      _tencent_code 的兜底一律补 "sz"。沪市 ETF（如 510300 沪深300ETF、
      588000 科创50ETF）被请求成 sz510300 → 接口永远返回空 → 界面**永久显示
      "--"**，且错误代码会被写进配置，重启后照旧失败。
    - "920"（北交所新段）的判定排在 "9→sh" 之后，只是侥幸命中（9 压根没进前置
      分支，靠末尾兜底），判定顺序一变即错。

    判定顺序按「特例优先」，前缀越具体越靠前：
        920        → bj  北交所新段（原 4/8 代码迁移后），必须早于 9→sh
        11         → sh  沪市可转债（110/111/113/118）
        12/15/16/18→ sz  深市可转债（123/127/128）、深市基金与 ETF（159915 等）
        6/9/5      → sh  沪市主板与科创板（600/601/688）、沪市B股（900）、
                         沪市基金与 ETF（50/51/56/58：510300、588000…）
        0/3/1/2    → sz  深市主板（000/001/002）、创业板（300/301）、深市B股（200）
        4/8        → bj  北交所（43/83 开头）
    """
    if not raw6 or len(raw6) != 6 or not raw6.isdigit():
        return ""
    if raw6.startswith("920"):
        return "bj"
    if raw6.startswith("11"):
        return "sh"
    if raw6.startswith(("12", "15", "16", "18")):
        return "sz"
    # v1.4.18 建议#2：沪市固收段 —— 132xxx 可交换债(EB)、204xxx 国债通用回购
    # (GC001=204001) 都落在会被下面 "1/2→sz" 泛规则误判成深市的区间，先显式判沪。
    # 只补这两段**高置信**的；其余固收段(01x 国债/10x 企业债/20x 细分等)交易所归属
    # 需一张权威代码段表逐段核对后再加，离线猜测的代价是把真品种路由到错交易所。
    if raw6.startswith(("132", "204")):
        return "sh"
    if raw6.startswith(("6", "9", "5")):
        return "sh"
    if raw6.startswith(("0", "3", "1", "2")):
        return "sz"
    if raw6.startswith(("4", "8")):
        return "bj"
    return ""


def normalize_code(raw):
    """把用户录的各种写法收成一种形态（"sh600519" / "bj920185" 这一类带交易所前缀的形态）。

    输入 → 输出：
        已带 sh/sz/bj 前缀      → 原样返回（只去首尾空白、转小写）
        6 位纯数字              → 交易所判定交给 market_prefix（全仓唯一实现），此处只拼接
        4 位且以 4/8 开头       → 北交所老代码（8307 / 4300 那一族），尾部补 0 到 6 位再走上一条
        其它（含 hk/us 前缀、空串、长度不对、非法输入）→ 原样返回，不做解释

    **只做格式化，不做校验** —— 判定不了就原样交回，要不要报错由调用方决定。
    为什么调用方必须在取数前过这一道：编辑器允许直接敲 6 位数字，而腾讯接口自 2026-08 起
    对裸代码返回 code param error —— 配置里存了裸代码时行情主源必然失败、只能靠新浪兜底
    （sf_quote.fetch_quote 那段注释记着这次事故）。
    """
    raw = raw.strip().lower()
    if raw.startswith(("sh", "sz", "bj")):
        return raw
    if raw.isdigit():
        # 3.2 v1.2.27：北交所 4 位老代码（如 8307/4300）归一化——尾部补 0 到 6 位
        # 标准格式（保留 4/8 板块前缀特征，避免误判为深市 0 开头），再走 6 位规则。
        if len(raw) == 4 and raw.startswith(("4", "8")):
            raw = raw + "00"
        if len(raw) == 6:
            # v1.2.65：交易所判定下沉 market_prefix（唯一实现），此处只负责拼接。
            pfx = market_prefix(raw)
            if pfx:
                return pfx + raw
    # 判定不了就原样返回（含空串/非法输入）：本函数只做格式化，不负责校验，
    # 是否报错由调用方决定。
    return raw
