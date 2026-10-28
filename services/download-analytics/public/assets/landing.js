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
  // 系统开了"减少动态效果"时停掉首屏视频，只显示静止图（样式表负责切换显示，这里负责真的停播）。
  var video = document.getElementById("mv");
  var reduce = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : null;
  function syncMotion() {
    if (!video) return;
    if (reduce && reduce.matches) {
      video.pause();
    } else {
      var started = video.play();
      if (started && started.catch) started.catch(function () {});
    }
  }
  syncMotion();
  if (reduce && reduce.addEventListener) reduce.addEventListener("change", syncMotion);
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
