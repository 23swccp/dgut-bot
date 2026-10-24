/* 在自建测试课的教师数字码页控制台运行。只读 DOM，不发请求或更改刷新设置。 */
(() => {
  "use strict";
  if (location.hostname !== "lms.dgut.edu.cn" || location.pathname !== "/classroom/screen.html" || !location.hash.startsWith("#/attendence_code")) {
    throw new Error("请在教师数字码显示页面运行");
  }
  if (window.dgutDigitSamples?.running) throw new Error("采样已在运行");
  const started = Date.now();
  const observations = [];
  let previous = null;
  let changes = 0;
  let timer;
  let limit;
  const capture = () => {
    const nodes = [...document.querySelectorAll(".att-scroll .num_code > span")];
    const code = nodes.map(node => node.textContent.trim()).join("");
    if (nodes.length !== 4 || !/^\d{4}$/.test(code) || nodes.some(node => !node.getClientRects().length)) return;
    const now = Date.now();
    observations.push({ timestampMs: now, elapsedMs: now - started, code });
    if (code !== previous) {
      changes += 1;
      console.log(`[数字码采样] ${new Date(now).toISOString()} ${code}（第 ${changes} 个显示值）`);
      previous = code;
    }
  };
  const stop = () => {
    if (!window.dgutDigitSamples.running) return;
    clearInterval(timer);
    clearTimeout(limit);
    window.dgutDigitSamples.running = false;
    const result = {
      schema: "dgut-displayed-digit-codes-v1", startedAt: new Date(started).toISOString(),
      finishedAt: new Date().toISOString(), pollMs: 500, observations,
      note: "只观察显示值；刷新后恰好相同、隐藏标签页节流或读数间遗漏的变化无法区分。",
    };
    const blob = new Blob([JSON.stringify(result, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `digit-codes-${started}.json`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
    console.log(`[数字码采样] 已停止，导出 ${observations.length} 条观察、${changes} 个连续显示值。`);
    return result;
  };
  window.dgutDigitSamples = { running: true, observations, stop };
  capture();
  if (!observations.length) {
    window.dgutDigitSamples.running = false;
    throw new Error("没有找到可见的四位数字码，未开始采样");
  }
  timer = setInterval(capture, 500);
  limit = setTimeout(stop, 300000);
  console.log("采样已开始；保持教师页可见，5 分钟后自动导出。提前停止：dgutDigitSamples.stop()");
})();
