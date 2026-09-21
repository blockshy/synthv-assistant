"""用 Node 执行网页实际轮询函数，隔离验证长任务状态，不访问浏览器或模型。"""

from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "前端轮询回归需要开发环境提供 Node.js")
class FrontendPollingTests(unittest.TestCase):
    """生产函数仍位于 app.js；测试只替换网络和时钟，不复制轮询实现。"""

    def test_long_running_task_follows_server_terminal_state_without_resubmission(self):
        # 提取有稳定相邻声明的完整函数，在隔离上下文里提供最少依赖。
        # 模拟时钟每次读取跨过十二分钟，旧版浏览器总时限会在此提前失败。
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('web/app.js', 'utf8');
const start = source.indexOf('  async function waitForJob(');
const end = source.indexOf('\n  async function record(', start);
assert(start >= 0 && end > start, '缺少网页轮询函数边界');
const productionFunction = source.slice(start, end);

/** 为单个场景构造全新运行环境，所有请求只读取预设任务状态。 */
function fixture(states) {
  let clock = 0;
  const paths = [];
  const progress = [];
  const pauses = [];
  const context = {
    Date: {now: () => { clock += 12 * 60 * 1000; return clock; }},
    api: async (path) => {
      paths.push(path);
      assert(states.length > 0, '终态后不应继续查询');
      const state = states.shift();
      if (state instanceof Error) throw state;
      return state;
    },
    errorMessage: (value) => typeof value === 'string' ? value : value.message,
    setTimeout: (callback, delay) => { pauses.push(delay); callback(); },
  };
  vm.createContext(context);
  vm.runInContext(productionFunction + '\nthis.wait = waitForJob;', context);
  return {paths, progress, pauses, wait: (id = 'test-job') => context.wait(id, (job) => progress.push(job))};
}

(async () => {
  const completed = fixture([
    {state:'running', progress:{elapsedSeconds:720, reasoning:'公开摘要一'}},
    {state:'running', progress:{elapsedSeconds:1440, reasoning:'公开摘要一、二'}},
    {state:'done', result:{text:'完整结果'}},
  ]);
  assert.deepEqual(await completed.wait(), {text:'完整结果'});
  assert.equal(completed.progress.length, 2);
  assert.deepEqual(completed.paths, Array(3).fill('/api/jobs/test-job'));
  assert.deepEqual(completed.pauses, [850, 850]);

  // 后端终态必须立即结束，不能继续显示忙碌或自动重发模型任务。
  const failed = fixture([{state:'running'}, {state:'error', error:'输出等待超时'}]);
  await assert.rejects(failed.wait(), /输出等待超时/);
  assert.equal(failed.paths.length, 2);
  const disconnected = fixture([new Error('无法连接本地服务')]);
  await assert.rejects(disconnected.wait(), /无法连接本地服务/);
  assert.equal(disconnected.paths.length, 1);
  const unknown = fixture([{state:'unrecognized'}]);
  await assert.rejects(unknown.wait(), /任务状态无法识别/);
  assert.equal(unknown.paths.length, 1);
  const missing = fixture([]);
  await assert.rejects(missing.wait(''), /未返回任务编号/);
  assert.equal(missing.paths.length, 0);
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run([NODE, "-e", script], cwd=ROOT, capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
