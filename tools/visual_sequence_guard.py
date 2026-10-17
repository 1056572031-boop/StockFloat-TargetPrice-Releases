"""Independent acceptance rules for sampled native presentation evidence.

Screenshots are compositor crops, never QWidget.grab used as the observed result.
The Qt image is only the text template. Limits are fixed here, not derived from
the product constants under test. Sampling is not a claim of frame completeness.
The template must contain left-side price digits, not merely right-side percent text.
"""
from __future__ import annotations

import math

MAX_SAMPLE_GAP_SECONDS = 0.5
MAX_DESYNC_POINTS = 2.0
MIN_OBSERVATION_SECONDS = 0.8
MIN_SAMPLES = 4


def text_match(reference, observed):
    """Match opaque bright foreground; reject blank, white and missing templates."""
    if reference.isNull() or observed.isNull():
        return {"passed": False, "reason": "missing image"}
    if (reference.width(), reference.height()) != (observed.width(), observed.height()):
        return {"passed": False, "reason": "image dimensions differ"}
    candidates = matched = bright = total = 0
    # Left price digits only: right-side percent text must not stand in for price.
    scale = reference.devicePixelRatio()
    for y in range(int(72 * scale), min(reference.height(), int(105 * scale))):
        for x in range(int(12 * scale), min(reference.width(), int(108 * scale))):
            a, b = reference.pixelColor(x, y), observed.pixelColor(x, y)
            total += 1
            bright += min(b.red(), b.green(), b.blue()) > 180
            if a.alpha() >= 245 and max(a.red(), a.green(), a.blue()) > 170:
                candidates += 1
                if max(abs(a.red() - b.red()), abs(a.green() - b.green()),
                       abs(a.blue() - b.blue())) <= 75:
                    matched += 1
    fraction = matched / candidates if candidates else 0
    white = bright / total if total else 1
    return {"passed": candidates >= 150 * scale * scale and fraction >= 0.65 and white < 0.80,
            "template_pixels": candidates, "matched_fraction": fraction,
            "white_fraction": white}


def sequence_problems(samples, *, startup=False):
    problems = []
    if len(samples) < MIN_SAMPLES:
        return [f"insufficient samples: {len(samples)} < {MIN_SAMPLES}"]
    times = [s.get("time") for s in samples]
    if any(not isinstance(t, (int, float)) or not math.isfinite(t) for t in times):
        return ["invalid sample timestamps"]
    intervals = [b - a for a, b in zip(times, times[1:])]
    if not 0 <= times[0] <= MAX_SAMPLE_GAP_SECONDS:
        problems.append("first compositor sample arrives later than 0.5 seconds")
    if any(s.get("capture_seconds", 0) > MAX_SAMPLE_GAP_SECONDS for s in samples):
        problems.append("a compositor capture itself spans more than 0.5 seconds")
    if any(gap <= 0 or gap > MAX_SAMPLE_GAP_SECONDS for gap in intervals):
        problems.append("sampling gap exceeds 0.5 seconds or time is not increasing")
    if times[-1] - times[0] < MIN_OBSERVATION_SECONDS:
        problems.append("observation span < 0.8 seconds")
    sizes = []
    for index, sample in enumerate(samples):
        if sample.get("content_visible") is not True:
            problems.append(f"sample {index}: compositor price text missing")
        pair = sample.get("pair")
        if not pair or pair.get("glass_on_top") is not False:
            problems.append(f"sample {index}: missing or inverted native pair")
        lag = pair.get("desync_px") if pair else None
        if not isinstance(lag, (int, float)) or not math.isfinite(lag) or lag > MAX_DESYNC_POINTS or lag < 0:
            problems.append(f"sample {index}: native pair desync > 2 points or unknown")
        size = sample.get("size")
        if not isinstance(size, (list, tuple)) or len(size) != 2 or any(
                not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0 for v in size):
            problems.append(f"sample {index}: missing native size")
        else:
            sizes.append(tuple(size))
    if startup and len(set(sizes)) != 1:
        problems.append("startup geometry changes between visible samples")
    return problems


def first_paint_problems(paints):
    if not paints:
        return ["no actual paint events recorded"]
    problems = []
    if len({tuple(p["size"]) for p in paints}) != 1:
        problems.append("actual startup paint geometry changed")
    if any(p.get("has_price") is not True for p in paints):
        problems.append("placeholder price in actual startup paint")
    return problems


def drag_problems(samples):
    """WindowServer trajectory gate; deliberately not a compositor frame claim."""
    if len(samples) < 12:
        return ["drag trajectory needs at least 12 samples"]
    problems = []
    positions = [s["position"] for s in samples]
    if max(p[0] for p in positions) - min(p[0] for p in positions) < 40:
        problems.append("drag input did not move the actual window at least 40 points")
    for index, sample in enumerate(samples):
        pair = sample.get("pair")
        lag = pair.get("desync_px") if pair else None
        if not pair or pair.get("glass_on_top") is not False or not isinstance(lag, (int, float)) \
                or not math.isfinite(lag) or not 0 <= lag <= MAX_DESYNC_POINTS:
            problems.append(f"drag {index}: missing/inverted/desynchronized pair")
    times = [s["time"] for s in samples]
    if any(not math.isfinite(t) for t in times) or any(
            not 0 < b - a <= 0.06 for a, b in zip(times, times[1:])):
        problems.append("drag trajectory sampling is not within 60ms")
    return problems
