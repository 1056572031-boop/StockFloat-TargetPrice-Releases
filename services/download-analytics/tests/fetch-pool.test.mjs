import { test } from "node:test";
import assert from "node:assert/strict";
import { mapLimit } from "../fetch-pool.mjs";

const tick = () => new Promise(resolve => setImmediate(resolve));

test("runs at most `limit` tasks at once, really uses that many, and keeps the input order", async () => {
  let running = 0;
  let peak = 0;
  const input = Array.from({ length: 20 }, (_, i) => i);
  const output = await mapLimit(input, 4, async n => {
    running += 1;
    peak = Math.max(peak, running);
    for (let i = 0; i < (n % 3) + 1; i += 1) await tick();
    running -= 1;
    return n * 2;
  });
  assert.equal(peak, 4, "并发上限要被用满（否则等于串行），也不能超过");
  assert.deepEqual(output, input.map(n => n * 2), "结果顺序必须和输入一致，读回按位置对应素材名");
});

test("a failure stops new tasks from starting and rejects with the first error", async () => {
  const boom = new Error("boom");
  let started = 0;
  await assert.rejects(
    mapLimit(Array.from({ length: 30 }, (_, i) => i), 2, async n => {
      started += 1;
      await tick();
      if (n === 3) throw boom;
      return n;
    }),
    error => error === boom,
  );
  assert.ok(started < 30, `失败后不该把剩下的任务都跑完，实际启动了 ${started} 个`);
});

test("a task that throws a falsy value still fails the run, and that exact value is what comes out", async () => {
  // 只写 assert.rejects(promise) 抓不住"把假值错存成别的东西"：那种写法照样拒绝，只是拒绝的内容变了。
  for (const thrown of [undefined, 0, "", null]) {
    await assert.rejects(
      mapLimit([1, 2, 3], 2, async n => { if (n === 2) throw thrown; return n; }), // discipline-ok: 假值也必须算失败
      error => error === thrown,
      `抛出 ${JSON.stringify(thrown) ?? "undefined"} 时应原样拒绝`,
    );
  }
});

test("empty input resolves to an empty list and a limit above the item count is fine", async () => {
  assert.deepEqual(await mapLimit([], 6, async () => { throw new Error("不该被调用"); }), []);
  assert.deepEqual(await mapLimit([5, 6], 50, async n => n + 1), [6, 7]);
});

test("a limit that would create no lanes is rejected instead of silently returning holes", async () => {
  for (const limit of [0, -1, 1.5, NaN, undefined]) {
    await assert.rejects(mapLimit([1, 2], limit, async n => n), RangeError, String(limit));
  }
});
