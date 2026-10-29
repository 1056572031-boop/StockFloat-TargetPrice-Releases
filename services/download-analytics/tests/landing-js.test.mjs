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

const BLANK = "data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==";

// 最小的元素桩：属性表 + hidden 属性，够脚本读写 src / data-* / preload / poster。
function makeEl(attrs = {}) {
  const map = new Map(Object.entries(attrs));
  const handlers = {};
  return {
    hidden: false, map, complete: false, clientWidth: 1040, handlers,
    getAttribute: name => (map.has(name) ? map.get(name) : null),
    setAttribute: (name, value) => { map.set(name, String(value)); },
    removeAttribute: name => { map.delete(name); },
    addEventListener: (type, fn) => { (handlers[type] ||= []).push(fn); },
    // 图片"加载完成/失败"：用例手动触发，模拟网络回来。
    settle(type = "load") { for (const fn of handlers[type] || []) fn({}); },
  };
}

function harness({ hash = "", withAlt = true, withCta = true, withVideo = false, withScene = false, lazies = 0, observer = true,
  reduced = false, playRejects = false, connection = null, readyState = "loading", dpr = 1, width = 1040 } = {}) {
  const listeners = { window: {}, cta: [], go: [], motion: [] };
  const makeVideo = (attrs = {}) => Object.assign(makeEl(attrs), {
    plays: 0, pauses: 0,
    play() { this.plays += 1; return playRejects ? Promise.reject(new Error("blocked")) : Promise.resolve(); },
    pause() { this.pauses += 1; },
  });
  const video = makeVideo();
  const scene = makeVideo({ preload: "none", "data-poster": "/assets/scene-4-hide-poster.webp",
    "data-posterset": "/assets/p-720.webp 720w, /assets/p-1280.webp 1280w, /assets/p-2100.webp 2100w, /assets/p.webp 2940w" });
  scene.hidden = true;
  scene.loads = 0;
  scene.load = () => { scene.loads += 1; };
  const sources = [["s", "/assets/v-1470.mp4"], ["s", "/assets/v-1470.webm"], ["l", "/assets/v.mp4"], ["l", "/assets/v.webm"]]
    .map(([tier, src]) => makeEl({ "data-tier": tier, "data-src": src }));
  scene.querySelectorAll = selector => (selector === "source" ? sources : selector === "source[data-src]" ? sources.filter(source => source.getAttribute("data-src")) : []);
  const fig = makeEl();
  fig.clientWidth = width;
  const still = makeEl({ src: BLANK, "data-still": "/assets/scene-4-hide.webp", "data-stillset": "/assets/s-720.webp 720w, /assets/s.webp 2940w" });
  const lazy = Array.from({ length: lazies }, (_, i) => makeEl({ src: BLANK, "data-src": `/assets/scene-${i + 1}.webp`,
    "data-srcset": `/assets/scene-${i + 1}-720.webp 720w, /assets/scene-${i + 1}.webp 2940w` }));
  // 假的"进入视口"观察器：记下回调、选项和被观察的元素，由用例按"角色"手动喊进来了/出去了。
  const observers = [];
  class FakeObserver {
    constructor(callback, options) { this.callback = callback; this.options = options; this.targets = []; this.unobserved = []; this.disconnected = false; observers.push(this); }
    observe(target) { this.targets.push(target); }
    unobserve(target) { this.unobserved.push(target); }
    disconnect() { this.disconnected = true; }
  }
  const roles = {
    hero: o => o.options.threshold === 0.05,
    lazy: o => o.options.threshold === undefined && o.targets.some(t => lazy.includes(t)),
    near: o => o.targets.includes(fig) && o.options.threshold === undefined && o.options.rootMargin !== "1200px 0px",
    on: o => o.targets.includes(fig) && o.options.threshold === 0.35,
    warm: o => o.options.rootMargin === "1200px 0px",
  };
  // 同一角色可能先后建过几个观察器（放宽预读距离时旧的会被 disconnect），找的永远是当前还活着的那个。
  const all = role => observers.filter(o => roles[role](o));
  const find = role => all(role).filter(o => !o.disconnected).at(-1);
  const fire = (role, isIntersecting, target) => {
    const o = find(role);
    assert.ok(o, `没有找到 ${role} 观察器`);
    o.callback([{ target: target || o.targets[0], isIntersecting }], o);
  };
  const connectionListeners = [];
  const connectionState = connection && { ...connection,
    addEventListener: (type, fn) => connectionListeners.push([type, fn]) };
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
  const registry = { page, cta: withCta ? cta : null, alt: withAlt ? alt : null, goalt: go, mv: withVideo ? video : null,
    s4: withScene ? fig : null, sv: withScene ? scene : null, ss: withScene ? still : null };
  const document = {
    readyState,
    getElementById: id => registry[id] || null,
    querySelectorAll: selector => (selector === "img[data-src]" ? lazy : imgs),
  };
  const frames = [];
  const window = {
    devicePixelRatio: dpr,
    addEventListener: (type, fn) => { (listeners.window[type] ||= []).push(fn); },
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    requestAnimationFrame: fn => { frames.push(fn); return frames.length; },
  };
  // 默认不提供 matchMedia：老浏览器和旧用例都要能跑；要测动效偏好时才挂上。
  if (withVideo || withScene) window.matchMedia = () => motion;
  if (observer) window.IntersectionObserver = FakeObserver;
  const context = { document, window, location: { hash } };
  if (connectionState) context.navigator = { connection: connectionState };
  vm.createContext(context);
  vm.runInContext(source, context);
  const click = () => {
    const event = { defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
    for (const [type, fn] of listeners.cta) if (type === "click") fn(event);
    return event;
  };
  const changeMotion = matches => {
    motion.matches = matches;
    for (const [type, fn] of listeners.motion) if (type === "change") fn();
  };
  const emit = (type, event = {}) => { for (const fn of listeners.window[type] || []) fn(event); };
  const load = () => emit("load");
  // 程序滚动（刷新后恢复位置、点锚点自动滚）只会触发 scroll；访客真的动过页面才会有可信 wheel、touchmove、keydown、pointerdown。
  const scroll = () => emit("scroll");
  const intent = (type = "wheel", trusted = true) => emit(type, { isTrusted: trusted });
  const changeConnection = next => {
    if (!connectionState) throw new Error("没有连接状态可改");
    Object.assign(connectionState, next);
    for (const [type, fn] of connectionListeners) if (type === "change") fn({});
  };
  // 把排队的动画帧跑完（脚本里"等两帧"是嵌套的，所以循环跑到队列空）。
  const flush = () => { for (let i = 0; i < 20 && frames.length; i++) frames.splice(0).forEach(fn => fn()); };
  // 首屏视频的开始：页面加载完成、没有在途的图、等两帧。
  const startHero = () => { load(); flush(); };
  const fallback = () => { const t = timers.find(item => item.ms === 6000); if (t) t.fn(); };
  return { page, cta, alt, summary, go, listeners, timers, imgs, click, context, video, scene, still, fig, lazy, sources, motion, connection: connectionState, connectionListeners, observers, all, find, fire, changeMotion, changeConnection, load, scroll, intent, flush, startHero, fallback };
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

test("with reduced motion off, the hero video starts once the page has loaded, and a blocked play() does not throw", async () => {
  const ok = harness({ withVideo: true });
  assert.equal(ok.video.plays, 0, "页面没加载完不开始读：首屏图片先用满带宽");
  ok.startHero();
  assert.equal(ok.video.plays, 1);
  const blocked = harness({ withVideo: true, playRejects: true });
  blocked.startHero();
  assert.equal(blocked.video.plays, 1);
  await new Promise(resolve => setImmediate(resolve));
  // 能走到这里就说明被拒的 play() 没有变成未处理的拒绝而中断测试进程。
});

test("changing the system motion setting while the page is open pauses or resumes the video", () => {
  const h = harness({ withVideo: true });
  h.startHero();
  assert.equal(h.listeners.motion.length, 1, "要监听系统设置的变化");
  const pausedBefore = h.video.pauses;
  h.changeMotion(true);
  assert.equal(h.video.pauses, pausedBefore + 1);
  h.changeMotion(false);
  assert.equal(h.video.plays, 2, "关掉减少动态效果后接着播");
  const early = harness({ withVideo: true, reduced: true });
  early.changeMotion(false);
  assert.equal(early.video.plays, 0, "还没到开始的时候，改设置不能抢跑");
  early.startHero();
  assert.equal(early.video.plays, 1);
});

test("a page without the video is left alone and sets no timer; with it the video starts normally", () => {
  const none = harness({ withVideo: false });
  assert.equal(none.timers.length, 0, "没有首屏视频就不设兜底计时器");
  const h = harness({ withVideo: true });
  h.startHero();
  assert.equal(h.video.plays, 1);
});

test("the fourth scene does not touch the network on load: no poster, no playback, still picture left as a placeholder", () => {
  const h = harness({ withScene: true });
  assert.equal(h.scene.plays, 0, "没滚到就不能播（preload=none，播了就会开始下载约 5MB）");
  assert.equal(h.scene.getAttribute("preload"), "none");
  assert.equal(h.scene.getAttribute("poster"), null, "封面约 300KB，没接近视野前不装");
  assert.equal(h.scene.hidden, true, "视频在接近视野前是隐藏的，占位由静止图的占位图撑着");
  assert.equal(h.still.getAttribute("src"), BLANK, "静止图在触发前也只是 1 像素占位图");
  assert.equal(h.still.getAttribute("data-still"), "/assets/scene-4-hide.webp");
  assert.deepEqual(h.find("on").targets, [h.fig], "观察的是整张图的容器，不是隐藏中的视频");
  const threshold = h.find("on").options.threshold;
  assert.ok(threshold > 0 && threshold <= 0.5, "露出一部分就开始，不要等整块进来");
});

test("coming near picks a poster and a video tier, shows the video; scrolling into view plays it and scrolling away pauses it", () => {
  const h = harness({ withScene: true });
  assert.ok(h.sources.every(source => source.getAttribute("src") === null), "加载时视频源不写 src，哪一档都不下");
  assert.equal(h.scene.loads, 0);
  h.fire("near", true);
  assert.equal(h.scene.getAttribute("poster"), "/assets/p-1280.webp", "显示宽 1040、密度 1：取不小于 1040 的最小一档");
  assert.equal(h.scene.getAttribute("data-poster"), null);
  assert.equal(h.scene.getAttribute("data-posterset"), null);
  assert.deepEqual(h.sources.map(source => source.getAttribute("src")), ["/assets/v-1470.mp4", "/assets/v-1470.webm", null, null], "宽 1040 乘密度 1 不超过 1600，用小档");
  assert.equal(h.scene.loads, 1, "装完源要让浏览器重新选一次");
  assert.equal(h.scene.hidden, false);
  assert.equal(h.still.hidden, true);
  assert.equal(h.still.getAttribute("src"), BLANK, "有视频时不去下静止图");
  assert.equal(h.scene.plays, 0, "只是接近视野还不能播");
  h.fire("on", true);
  assert.equal(h.scene.plays, 1);
  const pausedBefore = h.scene.pauses;
  h.fire("on", false);
  assert.equal(h.scene.pauses, pausedBefore + 1, "离开屏幕就暂停，省流量和电量");
  h.fire("on", true);
  assert.equal(h.scene.plays, 2, "再滚回来接着播");
  h.fire("near", false);
  h.fire("near", true);
  assert.equal(h.scene.loads, 1, "来回进出视野不重复装源");
});

test("the scene video does not play before it has been armed, whichever of the two observers reports first", () => {
  const h = harness({ withScene: true });
  h.fire("on", true);
  assert.equal(h.scene.plays, 0, "还没接近视野（封面没装、视频还藏着）就不能播");
  assert.equal(h.scene.hidden, true);
  h.fire("near", true);
  assert.equal(h.scene.plays, 1, "两个条件都满足的那一刻才播");
  assert.equal(h.scene.hidden, false);
  assert.equal(h.scene.getAttribute("poster"), "/assets/p-1280.webp");
});

test("with reduced motion on, the scene shows the still picture and the video is never loaded or played", () => {
  const h = harness({ withScene: true, reduced: true });
  h.fire("near", true);
  h.fire("on", true);
  assert.equal(h.scene.plays, 0, "开了减少动态效果就不能播");
  assert.equal(h.scene.getAttribute("poster"), null, "静态模式不装视频封面");
  assert.equal(h.scene.getAttribute("preload"), "none", "视频一个字节都不下");
  assert.equal(h.scene.hidden, true);
  assert.equal(h.still.hidden, false);
  assert.equal(h.still.getAttribute("src"), "/assets/scene-4-hide.webp", "改显静止图");
  assert.equal(h.still.getAttribute("data-still"), null);
  assert.ok(h.scene.pauses >= 1, "进屏时要保证是停着的");
});

test("switching reduced motion while the scene video is on screen pauses and resumes it", () => {
  const h = harness({ withScene: true });
  h.fire("near", true);
  h.fire("on", true);
  assert.equal(h.scene.plays, 1);
  const pausedBefore = h.scene.pauses;
  h.changeMotion(true);
  assert.equal(h.scene.pauses, pausedBefore + 1);
  assert.equal(h.still.hidden, false, "开了减少动态效果当场改显静止图");
  assert.equal(h.scene.hidden, true);
  h.changeMotion(false);
  assert.equal(h.scene.plays, 2, "关掉减少动态效果后，还在屏幕里就接着播");
  assert.equal(h.scene.hidden, false);
  assert.equal(h.still.hidden, true);
  h.fire("on", false);
  const playsAfterLeaving = h.scene.plays;
  h.changeMotion(false);
  assert.equal(h.scene.plays, playsAfterLeaving, "已经滚出屏幕时，改设置不能把它拉起来播");
});

test("flipping the motion setting before the scene video was ever on screen must not start it", () => {
  const h = harness({ withScene: true, reduced: true });
  h.changeMotion(false);
  assert.equal(h.scene.plays, 0, "还没滚到的视频，不能因为系统设置变了就开始下载播放");
  h.changeMotion(true);
  h.changeMotion(false);
  assert.equal(h.scene.plays, 0);
  assert.equal(h.scene.getAttribute("poster"), null, "没接近视野，改设置也不能去拉封面");
  assert.equal(h.still.getAttribute("src"), BLANK, "也不能去拉静止图");
  h.fire("near", true);
  h.fire("on", true);
  assert.equal(h.scene.plays, 1, "滚进屏幕之后才播");
});

test("a blocked play() on the scene video is caught", async () => {
  const blocked = harness({ withScene: true });
  let caught = 0;
  blocked.scene.play = function () { this.plays += 1; return { catch(fn) { caught += 1; fn(new Error("blocked")); } }; };
  blocked.fire("near", true);
  blocked.fire("on", true);
  assert.equal(blocked.scene.plays, 1);
  assert.equal(caught, 1, "必须挂上 catch：被拒的 play() 不能变成未处理的拒绝");
  const hero = harness({ withVideo: true });
  let heroCaught = 0;
  hero.video.play = function () { this.plays += 1; return { catch() { heroCaught += 1; } }; };
  hero.startHero();
  assert.equal(heroCaught, 1, "首屏视频同理");
});

test("a browser without IntersectionObserver shows the still picture, never downloads the video, and loads every lazy image at once", () => {
  const old = harness({ withScene: true, withVideo: true, lazies: 3, observer: false });
  assert.equal(old.observers.length, 0);
  assert.equal(old.scene.plays, 0, "没有观察器就不抢着下载播放");
  assert.equal(old.scene.getAttribute("poster"), null);
  assert.equal(old.scene.loads, 0);
  assert.equal(old.scene.hidden, true);
  assert.equal(old.still.hidden, false);
  assert.equal(old.still.getAttribute("src"), "/assets/scene-4-hide.webp");
  assert.equal(old.still.getAttribute("srcset"), "/assets/s-720.webp 720w, /assets/s.webp 2940w", "静止图也带多档地址");
  assert.deepEqual(old.lazy.map(img => img.getAttribute("src")), ["/assets/scene-1.webp", "/assets/scene-2.webp", "/assets/scene-3.webp"], "宁可一次装上，也不留空白");
  assert.ok(old.lazy.every(img => img.getAttribute("data-src") === null && img.getAttribute("data-srcset") === null));
  assert.equal(old.lazy[0].getAttribute("srcset"), "/assets/scene-1-720.webp 720w, /assets/scene-1.webp 2940w");
  old.startHero();
  assert.equal(old.video.plays, 0, "图还在路上，首屏视频先等");
  for (const img of [...old.lazy, old.still]) img.settle("load");
  old.flush();
  assert.equal(old.video.plays, 1, "图都到了，首屏视频照常开始");
});

test("the hero video and the scene video are controlled independently", () => {
  const h = harness({ withVideo: true, withScene: true });
  h.startHero();
  assert.equal(h.video.plays, 1);
  assert.equal(h.scene.plays, 0);
  h.fire("near", true);
  h.fire("on", true);
  const heroPauses = h.video.pauses;
  assert.equal(h.video.plays, 1, "场景视频进屏不影响首屏视频");
  assert.equal(h.scene.plays, 1);
  const sceneState = [h.scene.plays, h.scene.pauses];
  h.fire("hero", false);
  assert.equal(h.video.pauses, heroPauses + 1);
  assert.deepEqual([h.scene.plays, h.scene.pauses], sceneState, "首屏视频滚走不影响场景视频");
});

test("the hero video pauses when it scrolls off screen and resumes when it comes back", () => {
  const h = harness({ withVideo: true });
  h.startHero();
  assert.equal(h.video.plays, 1);
  assert.equal(h.find("hero").targets[0], h.video);
  const pausedBefore = h.video.pauses;
  h.fire("hero", false);
  assert.equal(h.video.pauses, pausedBefore + 1, "滚出屏幕就停，不白占解码和电量");
  h.fire("hero", true);
  assert.equal(h.video.plays, 2, "滚回来接着播");
});

test("the hero video stays paused when motion is reduced, even if it scrolls back into view", () => {
  const h = harness({ withVideo: true, reduced: true });
  h.startHero();
  h.fire("hero", false);
  h.fire("hero", true);
  assert.equal(h.video.plays, 0);
  h.changeMotion(false);
  assert.equal(h.video.plays, 1);
  h.fire("hero", false);
  h.changeMotion(false);
  assert.equal(h.video.plays, 1, "已经滚出屏幕时，改设置不能把它拉起来播");
});

test("below-the-fold images stay as 1px placeholders until they come near, then load once and stop being watched", () => {
  const h = harness({ lazies: 3 });
  assert.ok(h.lazy.every(img => img.getAttribute("src") === BLANK), "加载时一张都不拉");
  h.load();
  assert.deepEqual(h.find("lazy").targets, h.lazy, "三张都在被观察");
  h.fire("lazy", false, h.lazy[0]);
  assert.equal(h.lazy[0].getAttribute("src"), BLANK, "没进预读范围就不动");
  h.fire("lazy", true, h.lazy[1]);
  assert.equal(h.lazy[1].getAttribute("src"), "/assets/scene-2.webp");
  assert.equal(h.lazy[1].getAttribute("data-src"), null);
  assert.equal(h.lazy[1].getAttribute("srcset"), "/assets/scene-2-720.webp 720w, /assets/scene-2.webp 2940w", "多档地址一起装上，手机和普通屏只下小图");
  assert.equal(h.lazy[1].getAttribute("data-srcset"), null);
  assert.equal(h.lazy[0].getAttribute("srcset"), null, "没装的图也不带 srcset");
  assert.equal(h.lazy[0].getAttribute("src"), BLANK, "别的图不受影响");
  assert.equal(h.lazy[2].getAttribute("src"), BLANK);
  assert.deepEqual(h.find("lazy").unobserved, [h.lazy[1]], "装上之后不再观察");
});

test("until the page has loaded only images already on screen are fetched; the lead distance opens on load or on the first scroll", () => {
  const before = harness({ lazies: 3 });
  assert.equal(before.find("lazy").options.rootMargin, "0px", "加载完成前不预读：屏下的图不能和首屏视频抢带宽");
  before.load();
  assert.equal(before.all("lazy").length, 2);
  assert.equal(before.all("lazy")[0].disconnected, true, "旧的观察器要撤掉，不然两个同时在装图");
  assert.equal(before.find("lazy").options.rootMargin, "800px 0px");
  const scrolled = harness({ lazies: 3 });
  scrolled.scroll();
  assert.equal(scrolled.find("lazy").options.rootMargin, "800px 0px", "加载没完用户先滚了，也要放行");
  scrolled.load();
  assert.equal(scrolled.all("lazy").length, 2, "放行只做一次");
  const done = harness({ lazies: 3, readyState: "complete" });
  assert.equal(done.find("lazy").options.rootMargin, "800px 0px", "脚本跑起来时页面已经加载完，直接放行");
});

test("images revealed before the lead distance opened are not watched again", () => {
  const h = harness({ lazies: 3 });
  h.fire("lazy", true, h.lazy[0]);
  h.load();
  assert.deepEqual(h.find("lazy").targets, [h.lazy[1], h.lazy[2]], "已经装上的不再重复观察");
});

test("the lead distance for lazy images is long on a normal connection and short on a slow one", () => {
  const lead = connection => { const h = harness({ lazies: 1, connection }); h.load(); return h.find("lazy").options.rootMargin; };
  assert.equal(lead(null), "800px 0px", "读不到网络信息按正常档");
  assert.equal(lead({ effectiveType: "4g" }), "800px 0px");
  assert.equal(lead({ effectiveType: "3g" }), "200px 0px");
  assert.equal(lead({ effectiveType: "2g" }), "200px 0px");
  assert.equal(lead({ effectiveType: "4g", saveData: true }), "200px 0px", "开了省流量按慢档");
});

test("with the visitor's data saver on, the scene never loads the video and shows the still picture instead", () => {
  for (const connection of [{ effectiveType: "4g", saveData: true }, { saveData: true }, { effectiveType: "2g", saveData: true }]) {
    const h = harness({ withScene: true, connection });
    h.intent();
    h.fire("near", true);
    h.fire("on", true);
    assert.equal(h.scene.plays, 0, JSON.stringify(connection));
    assert.equal(h.scene.getAttribute("poster"), null);
    assert.equal(h.scene.getAttribute("preload"), "none");
    assert.equal(h.still.getAttribute("src"), "/assets/scene-4-hide.webp");
    assert.equal(h.find("warm"), undefined, "省流量时即使滚动过也不热身");
  }
});

// 2026-10-29 实测：Chrome 没限速、没开省流量，只因服务端首字节慢，effectiveType 被估成 2g，第 4 张视频一个字节都没下。
// effectiveType 是浏览器的估算不是访客的选择，所以 2g / slow-2g 只能降档，不能禁播。
test("a 2G or slow-2G estimate alone does not stop the scene video: it plays on the small tier without warm-up", () => {
  for (const effectiveType of ["2g", "slow-2g"]) {
    const h = harness({ withScene: true, connection: { effectiveType }, width: 1040, dpr: 2 });
    h.intent();
    assert.equal(h.find("warm"), undefined, effectiveType + "：慢网不热身");
    assert.equal(h.find("near").options.rootMargin, "200px 0px", effectiveType + "：预读距离缩短");
    h.fire("near", true);
    h.fire("on", true);
    assert.equal(h.scene.plays, 1, effectiveType + "：视频照常播放");
    assert.equal(h.scene.hidden, false);
    assert.equal(h.still.hidden, true);
    assert.deepEqual(h.sources.filter(source => source.getAttribute("src")).map(source => source.getAttribute("src")),
      ["/assets/v-1470.mp4", "/assets/v-1470.webm"], effectiveType + "：高清屏也只装小档");
    assert.equal(h.scene.getAttribute("preload"), "none", "到了才读，不提前读");
  }
});

test("on a 3G connection the video still plays, but is not warmed up in advance", () => {
  const h = harness({ withScene: true, connection: { effectiveType: "3g" } });
  h.intent();
  assert.equal(h.find("warm"), undefined);
  assert.equal(h.find("near").options.rootMargin, "200px 0px", "预读距离缩短");
  h.fire("near", true);
  h.fire("on", true);
  assert.equal(h.scene.plays, 1);
  assert.equal(h.scene.getAttribute("preload"), "none", "到了才读，不提前读");
});

test("the video is only warmed up after the visitor has really moved, once, and only when it is within reach", () => {
  const h = harness({ withScene: true });
  assert.equal(h.find("warm"), undefined, "没动过页面就不热身：手机上第 4 张离首屏很近，一打开就读等于白耗流量");
  h.load();
  assert.equal(h.find("warm"), undefined, "页面加载完也不算");
  h.scroll();
  assert.equal(h.find("warm"), undefined, "scroll 事件不算：刷新后恢复位置、点锚点自动滚动也会触发它，那不是人在看");
  assert.equal(h.find("near").options.rootMargin, "800px 0px");
  h.intent("wheel");
  assert.equal(h.find("warm").options.rootMargin, "1200px 0px");
  h.fire("warm", false);
  assert.equal(h.scene.getAttribute("preload"), "none", "还远，不读");
  assert.equal(h.scene.loads, 0);
  h.fire("warm", true);
  assert.equal(h.scene.getAttribute("preload"), "auto", "快到了，先读起来");
  assert.equal(h.scene.loads, 1, "源在热身时就装好，不等到接近视野才装");
  assert.deepEqual(h.find("warm").unobserved, [h.fig], "热身只做一次");
  h.fire("near", true);
  assert.equal(h.scene.loads, 1, "接近视野时不重复装");
  h.intent("keydown");
  assert.equal(h.all("warm").length, 1, "再操作也不会重复建");
});

test("no warm-up happens when motion is reduced, even after the visitor has moved", () => {
  const h = harness({ withScene: true, reduced: true });
  h.intent();
  h.fire("warm", true);
  assert.equal(h.scene.getAttribute("preload"), "none", "静态模式不读视频");
  assert.equal(h.scene.loads, 0);
  assert.ok(h.sources.every(source => source.getAttribute("src") === null));
});

test("the scene poster is only requested once the lead distance has opened", () => {
  const h = harness({ withScene: true });
  assert.equal(h.find("near").options.rootMargin, "0px");
  h.fire("near", false);
  assert.equal(h.scene.getAttribute("poster"), null);
  h.load();
  assert.equal(h.find("near").options.rootMargin, "800px 0px");
  assert.equal(h.all("near")[0].disconnected, true);
  h.fire("near", true);
  assert.equal(h.scene.getAttribute("poster"), "/assets/p-1280.webp");
});

test("the hero video does not start before the page has loaded or the visitor has started to move", () => {
  const h = harness({ withVideo: true });
  h.flush();
  assert.equal(h.video.plays, 0, "页面没加载完，首屏图片还在用带宽");
  h.scroll();
  h.flush();
  assert.equal(h.video.plays, 1, "开始滚动也算放行");
});

test("the hero video waits for images that are on their way, counts a failed one as done, and does not wait for cached ones", () => {
  const h = harness({ withVideo: true, lazies: 3, readyState: "complete" });
  h.lazy[2].complete = true;
  h.fire("lazy", true, h.lazy[0]);
  h.fire("lazy", true, h.lazy[1]);
  h.fire("lazy", true, h.lazy[2]);
  h.flush();
  assert.equal(h.video.plays, 0, "两张图还在路上，视频不开始");
  h.lazy[0].settle("load");
  h.flush();
  assert.equal(h.video.plays, 0, "还剩一张");
  h.lazy[1].settle("error");
  h.lazy[1].settle("load");
  h.flush();
  assert.equal(h.video.plays, 1, "失败也算完成，重复触发只算一次");
});

test("an image that arrives before the page has finished loading does not start the hero video early", () => {
  const h = harness({ withVideo: true, lazies: 1 });
  h.fire("lazy", true, h.lazy[0]);
  h.lazy[0].settle("load");
  h.flush();
  assert.equal(h.video.plays, 0, "页面还没加载完，图到了也不能抢跑");
  h.load();
  h.flush();
  assert.equal(h.video.plays, 1);
});

test("if an image never finishes, the hero video starts after 6 seconds anyway", () => {
  const h = harness({ withVideo: true, lazies: 1, readyState: "complete" });
  h.fire("lazy", true, h.lazy[0]);
  h.flush();
  assert.equal(h.video.plays, 0);
  h.fallback();
  assert.equal(h.video.plays, 1);
});

test("with the visitor's data saver on the hero video is never started, while 3G / 2G estimates still play it", () => {
  for (const connection of [{ saveData: true }, { effectiveType: "4g", saveData: true }]) {
    const h = harness({ withVideo: true, connection });
    h.startHero();
    h.fallback();
    assert.equal(h.video.plays, 0, JSON.stringify(connection) + "：只显示封面，视频一个字节都不下");
  }
  for (const effectiveType of ["3g", "2g", "slow-2g"]) {
    const slowEstimate = harness({ withVideo: true, connection: { effectiveType } });
    slowEstimate.startHero();
    assert.equal(slowEstimate.video.plays, 1, effectiveType + "：网络估算偏慢不等于访客要省流量，照常播放");
  }
});

test("each kind of real visitor input counts as intent, a plain scroll event does not, and scripted input cannot warm the video", () => {
  for (const type of ["wheel", "touchmove", "keydown", "pointerdown"]) {
    const h = harness({ withScene: true });
    h.intent(type);
    assert.ok(h.find("warm"), type + " 应当触发热身");
    assert.equal(h.find("near").options.rootMargin, "800px 0px", "操作也放行预读距离");
  }
  const h = harness({ withScene: true });
  h.scroll();
  assert.equal(h.find("warm"), undefined);
  h.intent("wheel", false);
  assert.equal(h.find("warm"), undefined, "脚本 dispatchEvent 的滚轮不该让屏下视频耗流量");
  assert.equal(h.find("near").options.rootMargin, "800px 0px", "伪造输入也不能放宽预读距离");
});

test("changing to 3G rebuilds the lead observers, cancels warm-up, and switches an already prepared video to the small tier", () => {
  const h = harness({ withScene: true, connection: { effectiveType: "4g" }, width: 1040, dpr: 2 });
  h.load();
  h.intent();
  assert.equal(h.find("near").options.rootMargin, "800px 0px");
  assert.ok(h.find("warm"));
  h.fire("warm", true);
  assert.deepEqual(h.sources.filter(source => source.getAttribute("src")).map(source => source.getAttribute("src")), ["/assets/v.mp4", "/assets/v.webm"], "高清屏 4G 先用大档");
  const loads = h.scene.loads;
  h.changeConnection({ effectiveType: "3g" });
  assert.equal(h.find("near").options.rootMargin, "200px 0px", "切到 3G 缩短预读距离");
  assert.equal(h.find("warm"), undefined, "3G 必须取消热身观察器");
  assert.equal(h.scene.getAttribute("preload"), "none", "切到 3G 要撤销此前的 auto 热身，不能继续提前下载");
  assert.deepEqual(h.sources.filter(source => source.getAttribute("src")).map(source => source.getAttribute("src")), ["/assets/v-1470.mp4", "/assets/v-1470.webm"], "已经准备过大档也必须改装小档");
  assert.equal(h.scene.loads, loads + 1, "切档要调用 load() 让浏览器放弃旧源");
});

test("changing to 2G keeps a playing scene video running but moves it to the small tier", () => {
  for (const effectiveType of ["2g", "slow-2g"]) {
    const h = harness({ withScene: true, connection: { effectiveType: "4g" }, width: 1040, dpr: 2 });
    h.load();
    h.fire("near", true);
    h.fire("on", true);
    assert.equal(h.scene.plays, 1);
    assert.deepEqual(h.sources.filter(source => source.getAttribute("src")).map(source => source.getAttribute("src")), ["/assets/v.mp4", "/assets/v.webm"], "高清屏 4G 先用大档");
    const paused = h.scene.pauses;
    h.changeConnection({ effectiveType });
    assert.equal(h.scene.pauses, paused, effectiveType + "：不能因为估算变慢就暂停");
    assert.equal(h.scene.hidden, false);
    assert.equal(h.still.hidden, true);
    assert.deepEqual(h.sources.filter(source => source.getAttribute("src")).map(source => source.getAttribute("src")), ["/assets/v-1470.mp4", "/assets/v-1470.webm"], effectiveType + "：改装小档");
    assert.ok(h.scene.plays >= 2, "切档后继续播放");
  }
});

test("changing to data saver pauses a prepared scene, hides the video, and shows the still picture", () => {
  for (const next of [{ saveData: true }]) {
    const h = harness({ withScene: true, connection: { effectiveType: "4g" } });
    h.load();
    h.fire("near", true);
    h.fire("on", true);
    assert.equal(h.scene.plays, 1);
    const paused = h.scene.pauses;
    const loads = h.scene.loads;
    assert.ok(h.sources.some(source => source.getAttribute("src")), "切换前视频源确实已经装上");
    h.changeConnection(next);
    assert.equal(h.scene.pauses, paused + 1, JSON.stringify(next));
    assert.equal(h.scene.hidden, true);
    assert.equal(h.still.hidden, false);
    assert.equal(h.still.getAttribute("src"), "/assets/scene-4-hide.webp");
    assert.ok(h.sources.every(source => source.getAttribute("src") === null), "静态模式必须清掉已热身的视频源，不能只藏起来还在后台下");
    assert.equal(h.scene.getAttribute("preload"), "none");
    assert.equal(h.scene.loads, loads + 1, "清源后重新 load，浏览器才会停掉后续下载");
    assert.equal(h.find("warm"), undefined, "静态模式不能保留热身观察器");
  }
});

test("a connection object without change support keeps the original safe behavior", () => {
  const h = harness({ withScene: true, connection: { effectiveType: "3g" } });
  assert.equal(h.find("near").options.rootMargin, "0px");
  h.load();
  assert.equal(h.find("near").options.rootMargin, "200px 0px");
  assert.equal(h.find("warm"), undefined);
});

test("the poster and the video tier follow the displayed width times the pixel density, and a slow connection always gets the small tier", () => {
  const tiers = h => h.sources.filter(source => source.getAttribute("src")).map(source => source.getAttribute("src"));
  const small = ["/assets/v-1470.mp4", "/assets/v-1470.webm"], large = ["/assets/v.mp4", "/assets/v.webm"];
  const near = options => { const h = harness({ withScene: true, ...options }); h.fire("near", true); return h; };
  const phone = near({ width: 342, dpr: 3 });
  assert.equal(phone.scene.getAttribute("poster"), "/assets/p-1280.webp", "342 乘 3 = 1026：取 1280 档");
  assert.deepEqual(tiers(phone), small);
  const retina = near({ width: 1040, dpr: 2 });
  assert.equal(retina.scene.getAttribute("poster"), "/assets/p-2100.webp", "1040 乘 2 = 2080：取 2100 档");
  assert.deepEqual(tiers(retina), large, "高清屏用大档，画质不降");
  const huge = near({ width: 1040, dpr: 3 });
  assert.equal(huge.scene.getAttribute("poster"), "/assets/p.webp", "超过最大一档就取最大一档");
  assert.deepEqual(tiers(huge), large);
  const edge = near({ width: 800, dpr: 2 });
  assert.deepEqual(tiers(edge), small, "正好 1600 仍是小档");
  const tiny = near({ width: 300, dpr: 2 });
  assert.equal(tiny.scene.getAttribute("poster"), "/assets/p-720.webp");
  const threeG = near({ width: 1040, dpr: 2, connection: { effectiveType: "3g" } });
  assert.deepEqual(tiers(threeG), small, "3G 即使是高清屏也用小档");
  assert.equal(threeG.scene.getAttribute("poster"), "/assets/p-2100.webp", "封面按屏幕挑，和网速无关（它只有 200KB）");
});

test("the script tolerates a page without the download link or the folded section", () => {
  assert.doesNotThrow(() => harness({ withCta: false, withAlt: false }));
  const h = harness({ withCta: false, withAlt: false, hash: "#alt" });
  assert.equal(h.page.classList.contains("started"), false);
});
