// 有界并发地跑一批异步任务，供部署后的上线读回使用。
// 结果顺序与输入一致；任一任务抛错后不再启动新任务，已在途的等它们结束，然后以第一个错误拒绝。
// limit 必须是 ≥1 的整数：0 或小数会让"没有任何车道"，结果是一份空洞数组却不报错，等于把读回变成永远通过。
export async function mapLimit(items, limit, work) {
  if (!Number.isInteger(limit) || limit < 1) throw new RangeError(`limit must be an integer >= 1, got ${limit}`);
  const results = new Array(items.length);
  let next = 0;
  let failure = null;
  async function lane() {
    while (failure === null) {
      const index = next++;
      if (index >= items.length) return;
      try {
        results[index] = await work(items[index], index);
      } catch (error) {
        // 包一层再存：任务可能抛出 undefined、0 这类假值，直接判 failure 本身会把失败当成没发生。
        failure ??= { error };
      }
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, lane));
  if (failure !== null) throw failure.error;
  return results;
}
