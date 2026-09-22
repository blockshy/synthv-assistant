"""隔离验证用户消息的即时显示与服务端记录去重，不调用供应商或访问真实会话。"""

from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")

# 执行生产模块的事件和发送逻辑，仅用可控 Promise 替换同源接口与浏览器 DOM。
# 测试出口在运行时注入，不给正式网页增加访问私有会话状态的调试接口。
HARNESS = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const all = [];
class Element {
  constructor(tag='div') { this.tag=tag; this.children=[]; this.dataset={}; this.attrs={}; this.listeners={};
    this.className=''; this.value=''; this.checked=false; this.disabled=false; this.scrollTop=0; this.scrollHeight=1000;
    this.clientHeight=600; this._text=''; all.push(this);
    this.classList={contains:name=>this.className.split(' ').includes(name),
      add:name=>{if(!this.classList.contains(name))this.className+=' '+name;},
      remove:(...names)=>{this.className=this.className.split(' ').filter(n=>!names.includes(n)).join(' ');},
      toggle:(name,enabled)=>{const next=enabled===undefined?!this.classList.contains(name):enabled; next?this.classList.add(name):this.classList.remove(name); return next;}};
  }
  set textContent(text) { this._text=String(text); this.replaceChildren(); }
  get textContent() { return this._text+this.children.map(child=>child.textContent).join(''); }
  append(...children) { for(const child of children){child.remove();child.parent=this;this.children.push(child);} }
  replaceChildren(...children) { for(const child of this.children)child.parent=null; this.children=[]; this.append(...children); }
  remove() { if(this.parent)this.parent.children=this.parent.children.filter(child=>child!==this);this.parent=null; }
  setAttribute(name,value) {this.attrs[name]=value;}
  addEventListener(name,callback) {this.listeners[name]=callback;}
  querySelector(selector) {return this.children.find(child=>child.tag===selector)||null;}
  querySelectorAll() {return [];}
  focus() {}
  contains() {return false;}
}
let source=fs.readFileSync('web/chat.js','utf8');
const fields=new Map();
for(const match of source.matchAll(/\$\("([\w-]+)"\)/g)){
  if(!match[1].startsWith('chat-live-')){const node=new Element();node.id=match[1];fields.set(match[1],node);}
}
const get=id=>fields.get(id)||all.find(node=>node.id===id&&node.parent)||null;
for(const id of ['chat-job-text','chat-job-time','send-scope','conversation-feedback','chat-feedback','library-feedback','chat-subtitle','library-selection-count','audio-library-items','save-conversation-metadata','confirm-delete-conversation','cancel-delete-conversation','pane-project','pane-listen','tab-project','tab-listen','open-audio-library','close-audio-library','cancel-audio-library']) {
  if(!fields.has(id)){const node=new Element();node.id=id;fields.set(id,node);}
}
get('send-message').append(new Element('span'));
const body=new Element('body');
const document={getElementById:get,createElement:tag=>new Element(tag),body,querySelectorAll:()=>[],querySelector:()=>body,
  addEventListener(){},removeEventListener(){}};
const calls=[];
let api=async()=>{throw new Error('未配置的测试接口');};
let wait=async()=>{throw new Error('未配置的测试任务');};
let save=async()=>{};
const bridge={api:(...args)=>{calls.push(args);return api(...args);}, waitForJob:(...args)=>wait(...args),
  getStatus:()=>({bridge:{connected:true},writeEnabled:false}),getManualBusy:()=>false,errorMessage:error=>error.message,
  printable:value=>JSON.stringify(value),setAssistantBusy(){},clearManualPreview(){}};
const models={getState:()=>({configured:true,valid:true,audioInput:'supported',platformName:'测试',modelLabel:'离线模型'}),
  setInteractionBusy(){},setConversation(){},snapshot:()=>({platformId:'offline',model:'offline-model',reasoningEffort:'default'}),ensureSaved:()=>save()};
const window={SynthVWorkbench:bridge,SynthVUI:{icon:()=>new Element('svg'),setIconButton(){},iconButton:()=>new Element('button')},
  SynthVModels:models,SynthVPages:{current:'chat',go(){return true;}},SynthVCurves:{representation:()=>'',createPreview:()=>null},
  addEventListener(){},dispatchEvent(){}};
const context={window,document,location:{origin:'http://local.test',href:'http://local.test/'},URL,Date,Map,Set,structuredClone,
  localStorage:{getItem(){return null;},setItem(){},removeItem(){}},matchMedia:()=>({matches:false,addEventListener(){}}),
  setInterval:()=>1,clearInterval(){},requestAnimationFrame:callback=>callback(),Event:class{},CustomEvent:class{}};
vm.createContext(context);
source=source.replace('  initialize();','  window.testChat = {sendMessage, setConversation, state, openConversation, renderHistory};');
vm.runInContext(source,context);
const chat=window.testChat;
const base=(id='conversation-a',messages=[])=>({id,title:'隔离会话',messages,renderMode:'smooth',modelOptions:{}});
const user=(id='server-user',text='请调整气声',attachments=[],selection)=>({id,role:'user',text,attachments,selection,renderMode:'smooth'});
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
const tick=async()=>{for(let i=0;i<15;i++)await Promise.resolve();};
const messages=()=>get('chat-history').children.filter(node=>node.classList.contains('message-user'));
const start=(text='请调整气声')=>{get('chat-input').value=text;get('send-message').disabled=false;return chat.sendMessage({preventDefault(){}});};
"""


@unittest.skipUnless(NODE, "会话前端回归需要开发环境提供 Node.js")
class FrontendChatTests(unittest.TestCase):
    """验证慢请求、失败恢复及切换会话的实际异步边界。"""

    def run_case(self, script):
        result = subprocess.run([NODE, "-e", HARNESS + "\n(async()=>{\n" + script
                                 + "\n})().catch(error=>{console.error(error);process.exitCode=1;});"],
                                cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_immediate_message_survives_preflight_and_stale_read_then_deduplicates(self):
        self.run_case(r"""
const existing=user('old-identical');
chat.setConversation(base('conversation-a',[existing]));
const saving=deferred(),posting=deferred(),completion=deferred();
save=()=>saving.promise;
let stored=base('conversation-a',[existing]);
api=(path,body)=>path.endsWith('/messages')?posting.promise:Promise.resolve(path==='/api/conversations'?{items:[]}:stored);
wait=()=>completion.promise;
const sending=start();
// 保存选项尚未完成、POST 尚未发送时，正文就应已出现在历史中。
assert.equal(messages().length,2);
assert.equal(messages()[1].dataset.delivery,'preparing');
assert.match(messages()[1].textContent,/请调整气声/);
assert.equal(calls.some(([path])=>path.endsWith('/messages')),false);
saving.resolve();await tick();
assert.equal(messages().length,2); // 缓慢的 POST 不能隐藏已经显示的消息。
posting.resolve({jobId:'offline-job'});await tick();
assert.equal(messages().length,2); // 入队后的 GET 仍未保存用户记录，不得删除本地快照。
assert.equal(messages()[1].dataset.delivery,'queued');
const saved=user('new-user');
stored=base('conversation-a',[existing,saved,{id:'reply',role:'assistant',text:'模拟返回',actions:[]}]);
completion.resolve(stored);await sending;
assert.equal(messages().length,2); // 相同正文的旧历史保留；这次快照仅被对应的新记录替换。
assert(messages().every(node=>!node.dataset.delivery));
assert.equal(chat.state.localMessages.length,0);
assert.equal(get('chat-input').value,'');
assert.equal(calls.filter(([path])=>path.endsWith('/messages')).length,1);
""")

    def test_new_conversation_shows_audio_and_selection_intent_before_creation(self):
        self.run_case(r"""
const creating=deferred(),completion=deferred();
const audio={kind:'upload',id:'audio-a',name:'公开测试.wav',url:'/uploads/audio-a.wav',available:true,durationSeconds:1};
chat.state.attachments=[audio];get('include-selection').checked=true;
let stored=base();
api=(path,body)=>path==='/api/conversations'&&body?creating.promise:Promise.resolve(path.endsWith('/messages')?{jobId:'job'}:path==='/api/conversations'?{items:[]}:stored);
wait=()=>completion.promise;
const sending=start('试听这个附件');
assert.equal(messages().length,1);
assert.match(messages()[0].textContent,/公开测试.wav/);
assert.match(messages()[0].textContent,/拟附带选区/);
assert.doesNotMatch(messages()[0].textContent,/已发送 1/);
assert.equal(chat.state.localMessages[0].conversationId,null);
creating.resolve(base());await tick();
assert.equal(messages().length,1);
assert.equal(chat.state.localMessages[0].conversationId,'conversation-a');
stored=base('conversation-a',[user('user-with-audio','试听这个附件',[audio],{noteCount:2})]);
chat.setConversation(stored);
assert.equal(messages().length,1); // 正式记录先到达时，也不能同时显示两条附件消息。
assert.match(messages()[0].textContent,/已发送 1 段音频/);
assert.match(messages()[0].textContent,/发送时的选区摘要/);
completion.resolve(stored);await sending;
assert.equal(chat.state.attachments.length,0);
""")

    def test_timeout_keeps_visible_unknown_state_and_scopes_it_to_original_conversation(self):
        self.run_case(r"""
chat.setConversation(base());
api=async(path,body)=>{
  if(path.endsWith('/messages'))throw new Error('连接超时，结果待核实');
  if(path.endsWith('/conversation-b'))return base('conversation-b');
  return base();
};
await start();
assert.equal(messages().length,1);
assert.equal(messages()[0].dataset.delivery,'unknown');
assert.match(messages()[0].textContent,/发送状态未确认/);
assert.equal(get('chat-input').value,'请调整气声');
assert.equal(calls.filter(([path])=>path.endsWith('/messages')).length,1);
await chat.openConversation('conversation-b');
assert.equal(messages().length,0); // 失败的 A 会话消息不能出现在 B 会话。
await chat.openConversation('conversation-a');
assert.equal(messages().length,1);
chat.setConversation(base('conversation-a',[user()]));
assert.equal(messages().length,1);
assert.equal(messages()[0].dataset.delivery,undefined); // 迟到的持久记录核实成功后移除未知快照。
""")

    def test_preflight_failure_preserves_visible_unsent_message_without_posting(self):
        self.run_case(r"""
chat.setConversation(base());
save=async()=>{throw new Error('选项保存失败');};
api=async()=>base();
await start();
assert.equal(messages().length,1);
assert.equal(messages()[0].dataset.delivery,'failed');
assert.match(messages()[0].textContent,/未发送，原草稿已保留/);
assert.equal(get('chat-input').value,'请调整气声');
assert.equal(calls.some(([path])=>path.endsWith('/messages')),false);
assert.equal(chat.state.sending,false);
""")


if __name__ == "__main__":
    unittest.main()
