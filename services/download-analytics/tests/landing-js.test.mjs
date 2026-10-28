import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

// landing.js 是一段不依赖任何库的浏览器脚本。这里用最小的 DOM 桩在 node:vm 里真跑它，
// 断言可观测的行为（类名、是否阻止默认动作、焦点、展开状态），不读源码字符串。
const source = readFileSync(fileURLToPath(new URL("../public/assets/landing.js", import.meta.url)), "utf8");

function makeClassList() {
  const set = new Set();
  return { add: n => set.add(n), remove: n => set.delete(n), contains: n => set.has(n) };
}

function harness({ hash = "", withAlt = true, withCta = true, withVideo = false, reduced = false, playRejects = false } = {}) {
  const listeners = { window: {}, cta: [], go: [], motion: [] };
  const video = {
    plays: 0, pauses: 0,
    play() { this.plays += 1; return playRejects ? Promise.reject(new Error("blocked")) : Promise.resolve(); },
    pause() { this.pauses += 1; },
  };
  const motion = { matches: reduced, addEventListener: (type, fn) => listeners.motion.push([type, fn]) };
  const timers = [];
  const imgs = [];
  const page = { classList: makeClassList() };
  const summary = { focused: null, focus(options) { this.focused = options || true; } };
  const alt = { open: false, scrolled: null, querySelector: () => summary, scrollIntoView(o) { this.scrolled = o; } };
  const cta = {
    dataset: {},
    hasAttribute: name => name === "data-dl",
    addEventListener: (type, fn) => listeners.cta.push([type, fn]),
  };
  const go = { addEventListener: (type, fn) => listeners.go.push([type, fn]) };
  const registry = { page, cta: withCta ? cta : null, alt: withAlt ? alt : null, goalt: go, mv: withVideo ? video : null };
  const document = {
    getElementById: id => registry[id] || null,
    querySelectorAll: () => imgs,
  };
  const window = {
    addEventListener: (type, fn) => { (listeners.window[type] ||= []).push(fn); },
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
  };
  // 默认不提供 matchMedia：老浏览器和旧用例都要能跑；要测动效偏好时才挂上。
  if (withVideo) window.matchMedia = () => motion;
  const context = { document, window, location: { hash } };
  vm.createContext(context);
  vm.runInContext(source, context);
  const click = () => {
    const event = { defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
    for (const [type, fn] of listeners.cta) if (type === "click") fn(event);
    return event;
  };
  return { page, cta, alt, summary, go, listeners, timers, imgs, click, context, video, motion };
}

test("clicking the download link marks the page as started and does not cancel the download", () => {
  const h = harness();
  const event = h.click();
  assert.equal(h.page.classList.contains("started"), true);
  assert.equal(event.defaultPrevented, false, "第一次点击必须让浏览器照常下载");
});

test("a second click inside the guard window is swallowed so one intent starts one download", () => {
  const h = harness();
  h.click();
  const second = h.click();
  assert.equal(second.defaultPrevented, true, "1.5 秒内的重复点击不再发起第二次下载");
  assert.equal(h.timers.length, 1);
  assert.equal(h.timers[0].ms, 1500);
  h.timers[0].fn();
  const third = h.click();
  assert.equal(third.defaultPrevented, false, "窗口过后再点是一次新的、有意的重试");
});

test("coming back from the back/forward cache clears the started state and the guard", () => {
  const h = harness();
  h.click();
  assert.equal(h.page.classList.contains("started"), true);
  for (const fn of h.listeners.window.pageshow) fn({ persisted: false });
  assert.equal(h.page.classList.contains("started"), true, "不是从缓存恢复就不动");
  for (const fn of h.listeners.window.pageshow) fn({ persisted: true });
  assert.equal(h.page.classList.contains("started"), false);
  assert.equal(h.click().defaultPrevented, false, "从缓存回来后立刻能再点");
});

test("the backup link opens the folded section, moves keyboard focus into it, and scrolls to it", () => {
  const h = harness();
  const event = { defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
  for (const [type, fn] of h.listeners.go) if (type === "click") fn(event);
  assert.equal(event.defaultPrevented, true);
  assert.equal(h.alt.open, true);
  assert.ok(h.summary.focused, "键盘用户的焦点要跟过去");
  // 参数对象是在 vm 沙箱里建的，原型与本进程不同，严格 deepEqual 会因此误报，所以逐字段比。
  assert.equal(h.alt.scrolled?.block, "center");
});

test("opening the page with #alt, or changing the hash to #alt later, expands the section", () => {
  const direct = harness({ hash: "#alt" });
  assert.equal(direct.alt.open, true);
  const later = harness();
  assert.equal(later.alt.open, false);
  later.context.location.hash = "#alt";
  for (const fn of later.listeners.window.hashchange) fn();
  assert.equal(later.alt.open, true, "页面内手动改成 #alt 也要展开");
  later.alt.open = false;
  later.context.location.hash = "#faq";
  for (const fn of later.listeners.window.hashchange) fn();
  assert.equal(later.alt.open, false, "别的锚点不动它");
});

test("a failed image gets a marker class instead of breaking the scene", () => {
  const added = [];
  const h = harness();
  const img = { classList: { add: n => added.push(n) }, addEventListener: (type, fn) => { img.fire = fn; } };
  h.imgs.push(img);
  vm.runInContext(source, h.context);
  img.fire();
  assert.deepEqual(added, ["asset-failed"]);
});

test("with reduced motion on, the hero video is paused and never started", () => {
  const h = harness({ withVideo: true, reduced: true });
  assert.equal(h.video.pauses, 1);
  assert.equal(h.video.plays, 0, "开了减少动态效果就不能再去播");
});

test("with reduced motion off, the hero video is started, and a blocked autoplay does not throw", async () => {
  const ok = harness({ withVideo: true, reduced: false });
  assert.equal(ok.video.plays, 1);
  assert.equal(ok.video.pauses, 0);
  const blocked = harness({ withVideo: true, reduced: false, playRejects: true });
  assert.equal(blocked.video.plays, 1);
  await new Promise(resolve => setImmediate(resolve));
  // 能走到这里就说明被拒的 play() 没有变成未处理的拒绝而中断测试进程。
});

test("changing the system motion setting while the page is open pauses or resumes the video", () => {
  const h = harness({ withVideo: true, reduced: false });
  assert.equal(h.listeners.motion.length, 1, "要监听系统设置的变化");
  h.motion.matches = true;
  for (const [type, fn] of h.listeners.motion) if (type === "change") fn();
  assert.equal(h.video.pauses, 1);
  h.motion.matches = false;
  for (const [type, fn] of h.listeners.motion) if (type === "change") fn();
  assert.equal(h.video.plays, 2, "关掉减少动态效果后接着播");
});

test("a browser without matchMedia, or a page without the video, is left alone", () => {
  assert.doesNotThrow(() => harness({ withVideo: false }));
  const h = harness({ withVideo: true });
  assert.equal(h.video.plays, 1);
});

test("the script tolerates a page without the download link or the folded section", () => {
  assert.doesNotThrow(() => harness({ withCta: false, withAlt: false }));
  const h = harness({ withCta: false, withAlt: false, hash: "#alt" });
  assert.equal(h.page.classList.contains("started"), false);
});
