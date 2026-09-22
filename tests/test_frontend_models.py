"""执行完整模型选择模块，验证本地缓存复用和异步响应顺序，不访问供应商。"""

from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "模型目录前端回归需要开发环境提供 Node.js")
class FrontendModelsTests(unittest.TestCase):
    def test_local_cache_reused_across_conversations_and_refresh_beats_old_reads(self):
        # 运行生产模块，仅替换 DOM 和本地 API；不复制缓存算法，也不持有任何凭据。
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('web/models.js', 'utf8');
const tick = () => new Promise((resolve) => setImmediate(resolve));
const deferred = () => { let resolve; return {promise:new Promise((done) => {resolve=done;}), resolve:(value) => resolve(value)}; };

/** 最小 DOM 替身只承载真实脚本读写的表单字段，所有结果均由模拟本机服务返回。 */
function fixture(provider='openai', initialCache=[{id:'cached-model',label:'缓存模型'}]) {
  const nodes = new Map(), events = new Map(), calls = [];
  let pendingCache = null, cache = initialCache;
  const node = (id) => {
    if (!nodes.has(id)) nodes.set(id, {value:'',children:[],listeners:new Map(),classList:{toggle(){}},
      addEventListener(name, fn){this.listeners.set(name,fn);}, setAttribute(){},
      replaceChildren(){this.children=[];}, add(value){this.children.push(value);}, append(value){this.children.push(value);}});
    return nodes.get(id);
  };
  const bridge = {errorMessage:(error) => error.message, api:async (path, body) => {
    calls.push({path,body});
    if (path === '/api/model-platforms') return {items:[{id:'default',name:'测试平台',provider,model:'default-model',configured:true}],defaultPlatformId:'default'};
    if (path === '/api/model-capabilities') return {reasoning:{options:[]}};
    if (path === '/api/model-platforms/default/models') {
      if (body !== undefined) {cache=[{id:'fresh-model',label:'刷新模型'}]; return {models:cache,cachedAt:'2026-09-22T00:00:00Z',cachePersisted:true};}
      if (pendingCache) {const result=pendingCache; pendingCache=null; return result.promise;}
      return {models:cache,source:'cache',cacheHit:true,cachedAt:'2026-09-21T00:00:00Z'};
    }
    throw new Error('不应调用的接口：'+path);
  }};
  const window = {SynthVWorkbench:bridge,SynthVUI:{setIconButton(){}},
    addEventListener(name,fn){events.set(name,fn);}, dispatchEvent(){}};
  const context = {window,document:{getElementById:node},Option:function(label,value){this.label=label;this.value=value;},
    CustomEvent:function(type,init){this.type=type;this.detail=init.detail;},clearTimeout(){},setTimeout(){return 1;}};
  vm.runInNewContext(source, context);
  return {nodes,calls,window,holdCache(){pendingCache=deferred();return pendingCache;},
    models:() => node('chat-model-options').children.map((item) => item.value),
    refresh:() => node('refresh-chat-models').listeners.get('click')(),
    select:(id,model='') => window.SynthVModels.setConversation({id,modelOptions:{platformId:'default',model,reasoningEffort:'default'}})};
}

(async () => {
  const first = fixture(); await tick();
  assert.deepEqual(first.models(), ['cached-model']);
  assert(first.calls.some((call) => call.path.endsWith('/models') && call.body === undefined));
  assert(!first.calls.some((call) => call.path.endsWith('/models') && call.body !== undefined));
  first.select('conversation-one','user-draft'); await tick();
  first.select('conversation-two'); await tick();
  assert.deepEqual(first.models(), ['cached-model']);
  assert(!first.calls.some((call) => call.path.endsWith('/models') && call.body !== undefined), '新会话不能自动访问供应商');

  const reopened = fixture(); await tick();
  assert.deepEqual(reopened.models(), ['cached-model'], '重新初始化页面应读取本机持久化目录');
  assert(!reopened.calls.some((call) => call.path.endsWith('/models') && call.body !== undefined));

  const delayed = first.holdCache();
  first.select('conversation-three','manually-entered-model'); await tick();
  await first.refresh();
  assert.deepEqual(first.models(), ['fresh-model']);
  delayed.resolve({models:[],source:'none',cacheHit:false}); await tick();
  assert.deepEqual(first.models(), ['fresh-model'], '迟到的空缓存不能覆盖主动刷新');
  assert.equal(first.nodes.get('chat-model').value, 'manually-entered-model', '目录响应不得覆盖模型输入');
  assert.equal(first.calls.filter((call) => call.path.endsWith('/models') && call.body !== undefined).length, 1);
  first.select('conversation-four'); await tick();
  assert.deepEqual(first.models(), ['fresh-model']);
  // Qwen 初次使用的候选项是明确标记的本地官方预设，不能因此联网或覆盖手动模型。
  const qwen=fixture('qwen',[]); await tick();
  assert.deepEqual(qwen.models(), ['qwen3.8-flash','qwen3.8-max','qwen3.8-omni-flash']);
  assert(qwen.nodes.get('chat-model-options').children.every(item=>item.label.includes('未验证账号权限')));
  assert(!qwen.calls.some(call=>call.path.endsWith('/models') && call.body!==undefined));
  qwen.select('qwen-conversation','qwen3.8-max'); await tick();
  assert.equal(qwen.nodes.get('chat-model').value,'qwen3.8-max');
  await qwen.refresh();
  assert.deepEqual(qwen.models(),['fresh-model'], 'API 目录到达后使用实际目录，不将预设伪装成账号可用模型');
  assert.equal(qwen.nodes.get('chat-model').value,'qwen3.8-max');
})().catch((error) => {console.error(error);process.exitCode=1;});
"""
        result = subprocess.run([NODE, "-e", script], cwd=ROOT, capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
