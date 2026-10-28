/* 下载页交互：只改状态，不拦截下载本身（没有脚本时链接照样能下）。 */
(function () {
  var page = document.getElementById("page");
  var cta = document.getElementById("cta");
  var alt = document.getElementById("alt");
  if (cta && page && cta.hasAttribute("data-dl")) {
    cta.addEventListener("click", function (e) {
      if (cta.dataset.busy === "1") { e.preventDefault(); return; }
      cta.dataset.busy = "1";
      page.classList.add("started");
      window.setTimeout(function () { delete cta.dataset.busy; }, 1500);
    });
  }
  function openAlt() {
    if (!alt) return;
    alt.open = true;
    var summary = alt.querySelector("summary");
    if (summary) summary.focus({ preventScroll: true });
    alt.scrollIntoView({ block: "center" });
  }
  var go = document.getElementById("goalt");
  if (go) go.addEventListener("click", function (e) { e.preventDefault(); openAlt(); });
  if (location.hash === "#alt") openAlt();
  window.addEventListener("hashchange", function () {
    if (location.hash === "#alt") openAlt();
  });

  var IO = window.IntersectionObserver;
  var reduce = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : null;
  var reduced = function () { return !!(reduce && reduce.matches); };
  // 网络状况：开了"省流量"或只有 2G 级网络时，不拉大视频，第 4 张只显示静止图，首屏视频也不播；
  // 3G 级网络照常，但不热身、预读距离缩短、视频用小档。Safari 和 Firefox 读不到这些信息（navigator.connection 为空），
  // 按"不慢"处理——它们拿到的视频小档只有 0.4MB 上下，大档（高清屏）也只有 1.3MB 上下，热身的代价有限。
  var conn = (typeof navigator !== "undefined" && (navigator.connection || navigator.mozConnection || navigator.webkitConnection)) || null;
  var kind = "", saver = false, slow = false, lead = "800px 0px";
  function readNetwork() {
    kind = (conn && conn.effectiveType) || "";
    saver = !!(conn && (conn.saveData || kind === "slow-2g" || kind === "2g"));
    slow = saver || kind === "3g";
    lead = (slow ? 200 : 800) + "px 0px";
  }
  readNetwork();
  var networkHooks = [];
  function whenNetworkChanges(fn) { networkHooks.push(fn); }
  function syncNetwork() {
    readNetwork();
    networkHooks.forEach(function (fn) { fn(); });
  }
  if (conn && conn.addEventListener) conn.addEventListener("change", syncNetwork);
  var afterPaint = function (fn) {
    if (window.requestAnimationFrame) window.requestAnimationFrame(function () { window.requestAnimationFrame(fn); });
    else window.setTimeout(fn, 32);
  };

  // 分两步放行，免得屏下的内容和首屏抢带宽（慢网下首屏视频要 10 秒才到）：
  //  放行（页面加载完成，或开始滚动）之前，只装已经在屏幕里的图；放行之后才放宽到预读距离 lead。
  //  热身（提前把第 4 张的视频读起来）更保守：只有访客真的动过页面（滚轮、触摸、按键、点击）才做——
  //  不看 scroll 事件，因为刷新后恢复滚动位置、点锚点自动滚动也会触发它，那不是人在看。
  var released = false, intended = false;
  var releaseHooks = [], intentHooks = [];
  function whenReleased(fn) { if (released) fn(); else releaseHooks.push(fn); }
  function whenIntended(fn) { if (intended) fn(); else intentHooks.push(fn); }
  function release() {
    if (released) return;
    released = true;
    releaseHooks.splice(0).forEach(function (fn) { fn(); });
  }
  function intend(event) {
    // scroll 恢复和锚点跳转不算；脚本造的 Event 也不算。只让真实手势为屏下视频预读花流量。
    if (!event || event.isTrusted !== true || intended) return;
    intended = true;
    release();
    intentHooks.splice(0).forEach(function (fn) { fn(); });
  }
  window.addEventListener("load", release);
  window.addEventListener("scroll", release, { passive: true });
  ["wheel", "touchmove", "keydown", "pointerdown"].forEach(function (type) {
    window.addEventListener(type, intend, { passive: true });
  });
  if (document.readyState === "complete") release();

  // 首屏视频：带透明通道、一直循环。不写 autoplay、preload=none，所以脚本不叫它之前一个字节都不下：
  //  放行后、且已经装上的图都到位了（或 6 秒兜底），才开始读和播，让首屏图片先用满带宽；
  //  系统开了"减少动态效果"、省流量或 2G 时一直不播，只显示封面；滚出屏幕就暂停，不白占解码和电量。
  var video = document.getElementById("mv");
  var heroOn = true, heroGo = false, heroChecking = false, pending = 0;
  function syncMotion() {
    if (!video) return;
    if (reduced() || saver || !heroOn || !heroGo) {
      video.pause();
    } else {
      var started = video.play();
      if (started && started.catch) started.catch(function () {});
    }
  }
  function startHero() {
    if (heroGo) return;
    heroGo = true;
    syncMotion();
  }
  function checkHero() {
    if (!video || heroGo || heroChecking || !released) return;
    heroChecking = true;
    // 等两帧再看有没有在途的图：放行时新放宽的图要到下一轮渲染才会被观察器报进来，立刻看会误以为"没有在途的图"。
    afterPaint(function () {
      heroChecking = false;
      if (pending === 0) startHero();
    });
  }
  syncMotion();
  if (video) {
    window.setTimeout(startHero, 6000);
    whenReleased(checkHero);
  }
  if (reduce && reduce.addEventListener) reduce.addEventListener("change", syncMotion);
  if (video && IO) {
    new IO(function (entries) {
      entries.forEach(function (entry) { heroOn = entry.isIntersecting; });
      syncMotion();
    }, { threshold: 0.05 }).observe(video);
  }

  // 屏下的图：HTML 里只有一张 1 像素占位图，真地址在 data-src（和 data-srcset），接近视野才换成真图。
  // srcset 让手机和普通屏只下小图。不用浏览器自带的 loading="lazy"：它的提前量由浏览器定，
  // 常常把首屏下面的整图在首屏加载时就一起拉了。没有 IntersectionObserver 时宁可一次性全装上，也不留空白。
  function fetchImage(img, name, setName) {
    var src = img.getAttribute(name);
    if (!src) return;
    var set = img.getAttribute(setName);
    img.removeAttribute(name);
    if (set) { img.removeAttribute(setName); img.setAttribute("srcset", set); }
    img.setAttribute("src", src);
    if (img.complete) return;
    // 在途的图数量：首屏视频要等它们到位。到了或失败都算完成，只算一次。
    pending += 1;
    var settled = false;
    var done = function () {
      if (settled) return;
      settled = true;
      pending -= 1;
      checkHero();
    };
    img.addEventListener("load", done);
    img.addEventListener("error", done);
  }
  var lazies = document.querySelectorAll("img[data-src]");
  if (IO && lazies.length) {
    var lazyIO = null;
    var watchLazies = function (margin) {
      if (lazyIO) lazyIO.disconnect();
      lazyIO = new IO(function (entries) {
        entries.forEach(function (entry) {
          if (!entry.isIntersecting) return;
          fetchImage(entry.target, "data-src", "data-srcset");
          lazyIO.unobserve(entry.target);
        });
      }, { rootMargin: margin });
      lazies.forEach(function (img) { if (img.getAttribute("data-src")) lazyIO.observe(img); });
    };
    watchLazies("0px");
    whenReleased(function () { watchLazies(lead); });
    whenNetworkChanges(function () { if (released) watchLazies(lead); });
  } else {
    lazies.forEach(function (img) { fetchImage(img, "data-src", "data-srcset"); });
  }

  // 第 4 张卖点图是循环视频，分三步：
  //  热身（访客动过页面、且离视野 1200px 内、网络不慢）：按屏幕挑一档视频装上源并把 preload 改成 auto，真滚到时已经有数据；
  //  接近视野（放行后的预读距离内）：装上封面并显示视频，或在静态模式下装上静止图；
  //  进入视野（露出 35%）：开始播，离开就暂停。
  // 视频分两档：屏幕上显示宽度乘像素密度不超过 1600（手机、普通屏）用 1470 宽的小档，更大的高清屏用 2940 宽的大档；
  // 3G 一律小档。静态模式 = 系统开了"减少动态效果"、省流量/2G、或浏览器没有 IntersectionObserver：只显示静止图，视频不下载。
  var fig = document.getElementById("s4");
  var scene = document.getElementById("sv");
  var still = document.getElementById("ss");
  var pickFrom = function (set, need) {
    var best = "", bestWidth = 0, largest = "", largestWidth = 0;
    (set || "").split(",").forEach(function (item) {
      var parts = item.trim().split(/\s+/);
      var width = parseInt(parts[1], 10);
      if (!parts[0] || !width) return;
      if (width > largestWidth) { largest = parts[0]; largestWidth = width; }
      if (width >= need && (!best || width < bestWidth)) { best = parts[0]; bestWidth = width; }
    });
    return best || largest;
  };
  if (fig && scene && still) {
    var near = !IO;
    var onScreen = false;
    var prepared = false;
    var preparedTier = "";
    var staticMode = function () { return saver || !IO || reduced(); };
    var wantedTier = function () {
      var need = (fig.clientWidth || 0) * (window.devicePixelRatio || 1);
      return (slow || need <= 1600) ? "s" : "l";
    };
    var prepare = function () {
      var tier = wantedTier();
      if (prepared && preparedTier === tier) return;
      var need = (fig.clientWidth || 0) * (window.devicePixelRatio || 1);
      if (!prepared) {
        prepared = true;
        var poster = pickFrom(scene.getAttribute("data-posterset"), need) || scene.getAttribute("data-poster");
        scene.removeAttribute("data-posterset");
        scene.removeAttribute("data-poster");
        if (poster) scene.setAttribute("poster", poster);
      }
      scene.querySelectorAll("source").forEach(function (source) {
        if (source.getAttribute("data-tier") === tier) source.setAttribute("src", source.getAttribute("data-src"));
        else source.removeAttribute("src");
      });
      preparedTier = tier;
      scene.load();
    };
    var unload = function () {
      if (!prepared) return;
      scene.querySelectorAll("source").forEach(function (source) { source.removeAttribute("src"); });
      prepared = false;
      preparedTier = "";
      scene.setAttribute("preload", "none");
      // pause()/hidden 不保证浏览器会取消已经开始的 preload；清源再 load 才能真的停掉后续下载。
      scene.load();
    };
    var arm = function () {
      if (!near) return;
      if (staticMode()) {
        scene.hidden = true;
        still.hidden = false;
        fetchImage(still, "data-still", "data-stillset");
      } else {
        prepare();
        still.hidden = true;
        scene.hidden = false;
      }
    };
    var syncScene = function () {
      arm();
      if (near && onScreen && !staticMode()) {
        var playing = scene.play();
        if (playing && playing.catch) playing.catch(function () {});
      } else {
        scene.pause();
      }
    };
    if (IO) {
      var nearIO = null;
      var warmIO = null;
      var watchNear = function (margin) {
        if (nearIO) nearIO.disconnect();
        nearIO = new IO(function (entries) {
          entries.forEach(function (entry) { near = entry.isIntersecting; });
          syncScene();
        }, { rootMargin: margin });
        nearIO.observe(fig);
      };
      var watchWarm = function () {
        if (slow || !intended || warmIO) return;
        warmIO = new IO(function (entries) {
          entries.forEach(function (entry) {
            if (!entry.isIntersecting || staticMode()) return;
            prepare();
            scene.setAttribute("preload", "auto");
            warmIO.unobserve(fig);
          });
        }, { rootMargin: "1200px 0px" });
        warmIO.observe(fig);
      };
      watchNear("0px");
      whenReleased(function () { watchNear(lead); });
      new IO(function (entries) {
        entries.forEach(function (entry) { onScreen = entry.isIntersecting; });
        syncScene();
      }, { threshold: 0.35 }).observe(fig);
      whenIntended(watchWarm);
      whenNetworkChanges(function () {
        if (released) watchNear(lead);
        if (slow && warmIO) { warmIO.disconnect(); warmIO = null; }
        if (slow) scene.setAttribute("preload", "none");
        if (staticMode()) unload();
        // 已因热身装过源、但还没进 near 时，arm() 不会跑；网络变慢也必须立刻从大档换回小档。
        if (prepared && !staticMode()) prepare();
        syncScene();
        watchWarm();
      });
    } else {
      syncScene();
    }
    if (reduce && reduce.addEventListener) reduce.addEventListener("change", syncScene);
  }

  // 丢图时保留场景底色并留痕，不让一张外部素材把整块卖点变成空白。
  document.querySelectorAll("img").forEach(function (img) {
    img.addEventListener("error", function () { img.classList.add("asset-failed"); });
  });
  // 点下载后用"返回"回到本页时，Safari 的前进后退缓存会连同"已开始下载"一起还原；
  // 页面是从缓存里恢复的（persisted），就把状态清回去，免得没点过也显示已开始。
  window.addEventListener("pageshow", function (e) {
    if (e.persisted && page) page.classList.remove("started");
    if (e.persisted && cta) delete cta.dataset.busy;
  });
})();
