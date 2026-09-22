"""隔离验证即时消息、音频附件和组合预览确认边界，不调用供应商或访问真实会话。"""

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
const descendants=root=>root.children.flatMap(child=>[child,...descendants(child)]);
const matches=(node,selector)=>{
  if(selector.startsWith('.'))return node.classList.contains(selector.slice(1));
  const data=selector.match(/^\[data-([\w-]+)\]$/);
  if(data)return Object.hasOwn(node.dataset,data[1].replace(/-([a-z])/g,(_,letter)=>letter.toUpperCase()));
  return node.tag===selector;
};
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
  querySelector(selector) {return this.querySelectorAll(selector)[0]||null;}
  querySelectorAll(selector) {return descendants(this).filter(child=>matches(child,selector));}
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
const document={getElementById:get,createElement:tag=>new Element(tag),body,
  querySelectorAll:selector=>[...new Set([...fields.values(),body].flatMap(root=>[root,...descendants(root)]))].filter(node=>matches(node,selector)),querySelector:()=>body,
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
const windowListeners=new Map(),curveCalls={single:[],combined:[]};
const window={SynthVWorkbench:bridge,SynthVUI:{icon:()=>new Element('svg'),setIconButton(){},iconButton:()=>new Element('button')},
  SynthVModels:models,SynthVPages:{current:'chat',go(){return true;}},SynthVCurves:{representation:()=>'',
    createPreview:preview=>{curveCalls.single.push(preview);const chart=new Element('figure');chart.className='single-test-chart';return chart;},
    createCombinedPreview:items=>{curveCalls.combined.push(items);if(!items.some(item=>item.preview))return null;
      const chart=new Element('figure');chart.className='combined-test-chart';return chart;}},
  addEventListener(name,callback){if(!windowListeners.has(name))windowListeners.set(name,[]);windowListeners.get(name).push(callback);},
  dispatchEvent(event){for(const callback of windowListeners.get(event.type)||[])callback(event);}};
const context={window,document,location:{origin:'http://local.test',href:'http://local.test/'},URL,Date,Map,Set,structuredClone,
  localStorage:{getItem(){return null;},setItem(){},removeItem(){}},matchMedia:()=>({matches:false,addEventListener(){}}),
  setInterval:()=>1,clearInterval(){},requestAnimationFrame:callback=>callback(),Event:class{},CustomEvent:class{}};
vm.createContext(context);
source=source.replace('  initialize();','  window.testChat = {sendMessage, setConversation, state, openConversation, renderHistory, executeBatch, executeAction, reuseMessage, syncControls};');
vm.runInContext(source,context);
const chat=window.testChat;
const base=(id='conversation-a',messages=[])=>({id,title:'隔离会话',messages,renderMode:'smooth',modelOptions:{}});
const user=(id='server-user',text='请调整气声',attachments=[],selection)=>({id,role:'user',text,attachments,selection,renderMode:'smooth'});
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
const tick=async()=>{for(let i=0;i<15;i++)await Promise.resolve();};
const messages=()=>get('chat-history').children.filter(node=>node.classList.contains('message-user'));
const start=(text='请调整气声')=>{get('chat-input').value=text;get('send-message').disabled=false;return chat.sendMessage({preventDefault(){}});};
const emit=(type,detail={})=>window.dispatchEvent({type,detail});
const action=(id,parameter='tension')=>({id,parameter,delta:.1,status:'proposed',renderMode:'smooth'});
const proposal=(id,actions)=>({id,role:'assistant',text:'测试组合建议',actions});
const previewed=(source,batchId='aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa')=>({...source,status:'previewed',previewBatchId:batchId,
  preview:{previewId:`preview-${source.id}`,parameter:source.parameter,startSeconds:10,endSeconds:12,
    curvePreview:[{position:0,before:0,after:.1},{position:1,before:0,after:.1}]}});
const batchButtons=()=>get('chat-history').querySelectorAll('[data-batch-apply]');
const allowWrites=()=>{chat.state.status={bridge:{connected:true},writeEnabled:true};chat.syncControls();};
const reuseButtons=()=>get('chat-history').querySelectorAll('[data-message-reuse]');
const reused=(id,actions)=>({...proposal(id,actions),origin:'reuse',text:'本地复用已有建议，未调用模型。',selection:{noteCount:2}});
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

    def test_audio_attachments_share_message_bubble_and_create_player_only_when_expanded(self):
        """附件默认只占文件行，展开播放器也不得提前预读或自动播放。"""
        self.run_case(r"""
const assets=[
  {kind:'upload',id:'a',name:'很长的文件名称用于检查完整名称.wav',url:'/uploads/a.wav',available:true,durationSeconds:2},
  {kind:'recording',id:'b',name:'对比录音.wav',url:'/recordings/b.wav',available:true,durationSeconds:3},
];
chat.setConversation(base('conversation-a',[user('audio-user','请对比这两段录音',assets)]));
const bubble=messages()[0].children.find(node=>node.classList.contains('message-bubble'));
assert(bubble,'正文和音频应共同放在用户气泡中');
assert(bubble.children.some(node=>node.classList.contains('message-text')));
const group=bubble.children.find(node=>node.classList.contains('message-attachments'));
assert.equal(group.children.length,2);
assert.equal(messages()[0].children.some(node=>node.classList.contains('message-audio')),false);
const card=group.children[0];
assert.equal(card.tag,'details');
assert.equal(card.children.length,1); // 折叠状态不创建隐藏播放器或挂载音频地址。
const heading=card.children[0];
assert.equal(heading.tag,'summary');
assert.match(heading.attrs['aria-label'],/展开.*播放控件/);
assert.equal(heading.children.find(node=>node.classList.contains('message-audio-name')).title,assets[0].name);
card.open=true;card.listeners.toggle();
const audio=card.children.find(node=>node.tag==='audio');
assert(audio);
assert.equal(audio.preload,'none');
assert.equal(audio.src,'http://local.test/uploads/a.wav');
assert.equal(audio.controls,true);
assert.equal(audio.autoplay,undefined);
let paused=false;audio.pause=()=>{paused=true;};
card.open=false;card.listeners.toggle();
assert.equal(paused,true); // 收起后不能留下看不见的播放。
card.open=true;card.listeners.toggle();
assert.equal(card.children.filter(node=>node.tag==='audio').length,1);
""")

    def test_deleted_and_external_audio_stays_unavailable_inside_message_bubble(self):
        """历史快照不能让已删附件或外部 URL 获得可播放控件。"""
        self.run_case(r"""
const assets=[
  {kind:'upload',id:'deleted',name:'已删除.wav',url:'/uploads/deleted.wav',deleted:true},
  {kind:'upload',id:'permanent',name:'永久删除.wav',url:'/uploads/permanent.wav',permanent:true},
  {kind:'upload',id:'remote',name:'<script>文本文件名</script>',url:'https://external.example/audio.wav',available:true},
];
chat.setConversation(base('conversation-a',[user('audio-user','查看附件',assets)]));
const bubble=messages()[0].children.find(node=>node.classList.contains('message-bubble'));
const group=bubble.children.find(node=>node.classList.contains('message-attachments'));
assert.equal(group.children.length,3);
assert(group.children.every(node=>node.tag==='div'&&!node.listeners.toggle));
assert.match(group.children[0].textContent,/已移到回收站/);
assert.match(group.children[1].textContent,/已永久删除/);
assert.match(group.children[2].textContent,/暂不可用/);
assert.match(group.children[2].textContent,/<script>文本文件名<\/script>/);
assert(group.children.every(node=>node.children.every(child=>child.tag!=='audio')));
""")

    def test_batch_preview_requests_once_and_confirms_only_successful_parameters(self):
        """组合只创建一个预览请求和一张图；部分失败不混入待确认应用集合。"""
        self.run_case(r"""
const actions=[action('pitch','pitchCurve'),action('breath','breathiness'),action('tension')];
chat.setConversation(base('conversation-a',[proposal('reply-a',actions)]));
const response=deferred(),applying=deferred();
api=(path)=>path.endsWith('/preview')?response.promise:applying.promise;
const pending=chat.executeBatch('reply-a','preview');
assert.equal(calls.length,1);
assert.equal(calls[0][0],'/api/assistant/batches/preview');
assert.deepEqual(Array.from(calls[0][1].actionIds),['pitch','breath','tension']);
await chat.executeBatch('reply-a','preview');
assert.equal(calls.length,1); // 忙碌时重复点击不能并发生成两份确认票据。
response.resolve({batchId:'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',actions:[previewed(actions[0]),actions[1],previewed(actions[2])],
  errors:[{actionId:'breath',message:'宿主未能安全预览气声'}]});
await pending;
assert.deepEqual(Array.from(chat.state.activeBatch.actionIds),['pitch','tension']);
assert.equal(chat.state.actionErrors.get('breath'),'宿主未能安全预览气声');
assert.equal(batchButtons().length,1);
assert.match(batchButtons()[0].textContent,/2 项参数/);
assert.equal(batchButtons()[0].disabled,true); // 未开启写权限仍允许只读预览。
assert.match(get('chat-history').textContent,/1 项未通过/);
assert.match(get('chat-feedback').textContent,/2 项预览/);
assert.equal(get('chat-history').querySelectorAll('.combined-test-chart').length,1);
assert.equal(get('chat-history').querySelectorAll('.single-test-chart').length,0);
assert.equal(curveCalls.single.length,0); // 参数明细不能再生成多张重复图。
await chat.executeBatch('reply-a','apply');assert.equal(calls.length,1);
allowWrites();assert.equal(batchButtons()[0].disabled,false);
const confirmed=chat.executeBatch('reply-a','apply');
assert.equal(chat.state.activeBatch,null); // 发送写请求前立即撤销确认资格。
assert.equal(actions[0].status,'unknown');assert.equal(actions[2].status,'unknown');
assert.equal(actions[1].status,'proposed');
assert.equal(calls[1][0],'/api/assistant/batches/apply');
assert.deepEqual(JSON.parse(JSON.stringify(calls[1][1])),{batchId:'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'});
await chat.executeBatch('reply-a','apply');assert.equal(calls.length,2);
applying.resolve({actions:[{...actions[0],status:'applied'},{...actions[2],status:'applied'}]});
await confirmed;
assert.equal(actions[0].status,'applied');assert.equal(actions[1].status,'proposed');assert.equal(actions[2].status,'applied');
assert.equal(batchButtons().length,0);
assert.match(get('chat-feedback').textContent,/已确认应用 2 项/);
await chat.executeBatch('reply-a','apply');assert.equal(calls.length,2);
""")

    def test_preview_warnings_remain_visible_outside_single_and_batch_details(self):
        """音高语义与边界保护说明常显，组合标注所属参数，字符串不能变成页面脚本。"""
        self.run_case(r"""
const pitch=previewed(action('pitch','pitchCurve'));
pitch.label='原生音高曲线';
pitch.preview.capabilityWarnings=['已有音高偏移会保留；图中原生曲线不是最终音频基频。','<script>仅作文字</script>',null,{},''];
chat.setConversation(base('conversation-a',[proposal('single',[pitch])]));
let notices=get('chat-history').querySelectorAll('[data-preview-warning]');
assert.equal(notices.length,2);
assert(notices.every(node=>node.parent.tag==='article'));
assert.match(notices[0].textContent,/不是最终音频基频/);
assert.equal(notices[1].textContent,'<script>仅作文字</script>');
assert.equal(notices[1].children.length,0);
const tension=previewed(action('tension'));
tension.label='张力';tension.preview.capabilityWarnings=['为保持选区外曲线，已补充保护点。'];
chat.setConversation(base('conversation-b',[proposal('batch',[pitch,tension])]));
notices=get('chat-history').querySelectorAll('[data-preview-warning]');
assert.equal(notices.length,3); // 折叠的逐项卡片不重复生成提示。
assert(notices.every(node=>node.parent.tag==='section'&&node.parent.classList.contains('message-actions')));
assert.match(notices[0].textContent,/^原生音高曲线：.*不是最终音频基频/);
assert.match(notices[2].textContent,/^张力：.*保护点/);
const folded=get('chat-history').querySelectorAll('.batch-action-details')[0];
assert.equal(folded.querySelectorAll('[data-preview-warning]').length,0);
chat.setConversation(base('conversation-c',[proposal('clean',[action('clean')])]));
assert.equal(get('chat-history').querySelectorAll('[data-preview-warning]').length,0);
assert.equal(calls.length,0); // 说明只依赖已返回的预览，不发起额外请求。
""")

    def test_manual_preview_shows_warnings_and_clears_them_for_next_result(self):
        """执行实际手动预览提交处理器，确认提示位于展开图表内且不会残留到下一次预览。"""
        self.run_case(r"""
const appSource=fs.readFileSync('web/app.js','utf8');
const begin=appSource.indexOf('  $("tuning-form").addEventListener("submit",');
const end=appSource.indexOf('  $("apply-change").addEventListener(',begin);
assert(begin>=0&&end>begin,'缺少手动预览处理器边界');
for(const id of ['tuning-form','preview-summary','preview-details','preview-curve-chart','preview-result']) {
  const node=new Element();node.id=id;fields.set(id,node);
}
get('tuning-form').reportValidity=()=>true;
const result={previewId:'manual-preview',parameter:'pitchCurve',label:'原生音高曲线',
  capabilityWarnings:['保留已有音高偏移；原生曲线不是最终音频基频。','<b>仍是纯文本</b>',null]};
let manualPending,previews=0;
const manualContext={document,window,$:get,state:{status:{writeEnabled:false}},
  parameterEditor:{getPayload:()=>({parameter:'pitchCurve',curve:[[0,60],[1,60]]}),describe:()=>({unit:'MIDI 半音'})},
  runBusy:(_kind,_feedback,task)=>{manualPending=task();},invalidatePreview(){},feedback(){},
  api:async(path)=>{assert.equal(path,'/api/preview');previews++;return result;},
  finite:Number.isFinite,numberText:String,printable:JSON.stringify};
vm.createContext(manualContext);vm.runInContext(appSource.slice(begin,end),manualContext);
get('tuning-form').listeners.submit({preventDefault(){}});await manualPending;
const chart=get('preview-curve-chart');
let notices=chart.querySelectorAll('[data-preview-warning]');
assert.equal(notices.length,2);assert(notices.every(node=>node.parent===chart));
assert.match(notices[0].textContent,/不是最终音频基频/);
assert.equal(notices[1].textContent,'<b>仍是纯文本</b>');assert.equal(notices[1].children.length,0);
assert.equal(get('preview-result').hidden,false);
delete result.capabilityWarnings;
get('tuning-form').listeners.submit({preventDefault(){}});await manualPending;
assert.equal(chart.querySelectorAll('[data-preview-warning]').length,0);
assert.equal(previews,2);
""")

    def test_batch_apply_timeout_keeps_unknown_and_never_replays(self):
        """写请求结果不明时两项都保持 unknown，失败后再次调用也不得重放。"""
        self.run_case(r"""
const actions=[action('a'),action('b','breathiness')];
chat.setConversation(base('conversation-a',[proposal('reply',actions)]));allowWrites();
api=async(path)=>{
  if(path.endsWith('/preview'))return {batchId:'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',actions:actions.map(item=>previewed(item)),errors:[]};
  throw new Error('应用连接超时，结果待核实');
};
await chat.executeBatch('reply','preview');
await chat.executeBatch('reply','apply');
assert(actions.every(item=>item.status==='unknown'));
assert.equal(chat.state.activeBatch,null);
assert.equal(batchButtons().length,0);
assert.match(get('chat-feedback').textContent,/结果待核实/);
await chat.executeBatch('reply','apply');
await chat.executeBatch('reply','preview'); // 全部 unknown 不能借重新预览绕过待核实状态。
assert.equal(calls.filter(([path])=>path.endsWith('/apply')).length,1);
assert.equal(calls.filter(([path])=>path.endsWith('/preview')).length,1);
assert(actions.every(item=>chat.state.actionErrors.has(item.id)));
assert.equal(chat.state.actionBusy,'');
""")

    def test_batch_authorization_is_revoked_by_navigation_manual_preview_and_disconnect(self):
        """旧图可以保留阅读；会话、手动工具或连接变化不能保留旧批次确认资格。"""
        self.run_case(r"""
let sequence=0;
const actions=[action('a'),action('b','breathiness')],conversation=base('conversation-a',[proposal('reply',actions)]);
chat.setConversation(conversation);allowWrites();
api=async(path)=>{
  if(path.endsWith('/preview')){const batchId=(++sequence).toString(16).padStart(32,'0');return {batchId,actions:actions.map(item=>previewed(item,batchId)),errors:[]};}
  if(path.endsWith('/conversation-b'))return base('conversation-b');
  if(path.endsWith('/conversation-a'))return conversation;
  throw new Error('失效预览不应发送应用请求');
};
await chat.executeBatch('reply','preview');assert(chat.state.activeBatch);
await chat.openConversation('conversation-b');assert.equal(chat.state.activeBatch,null);
await chat.openConversation('conversation-a');
assert.equal(batchButtons().length,0);
await chat.executeBatch('reply','apply');
assert.equal(calls.some(([path])=>path.endsWith('/apply')),false);
await chat.executeBatch('reply','preview');
emit('synthv:preview-invalidated');
assert.equal(chat.state.activeBatch,null);assert.equal(batchButtons().length,0);
await chat.executeBatch('reply','apply');
await chat.executeBatch('reply','preview');
emit('synthv:state',{status:{bridge:{connected:false},writeEnabled:true},manualBusy:false,settingsSaving:false});
assert.equal(chat.state.activeBatch,null);
assert.equal(get('chat-history').querySelectorAll('[data-batch-preview]')[0].disabled,true);
emit('synthv:state',{status:{bridge:{connected:true},writeEnabled:true},manualBusy:false,settingsSaving:false});
assert.equal(batchButtons().length,0); // 重连不会自行恢复旧票据。
await chat.executeBatch('reply','apply');
assert.equal(calls.some(([path])=>path.endsWith('/apply')),false);
await chat.executeBatch('reply','preview');
emit('synthv:restored',{summary:'隔离恢复结果'});
assert.equal(chat.state.activeBatch,null);assert.equal(batchButtons().length,0);
""")

    def test_starting_another_preview_revokes_previous_batch_even_if_it_fails(self):
        """新批预览或单项预览开始即撤销旧权限，失败不能让旧确认按钮恢复。"""
        self.run_case(r"""
const actions=[action('a'),action('b')],single=action('single','gender');
chat.setConversation(base('conversation-a',[proposal('reply',actions),proposal('other',[single])]));allowWrites();
api=async()=>({batchId:'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',actions:actions.map(item=>previewed(item)),errors:[]});
await chat.executeBatch('reply','preview');assert(chat.state.activeBatch);
const delayed=deferred();api=()=>delayed.promise;
const pending=chat.executeBatch('reply','preview');
assert.equal(chat.state.activeBatch,null);assert.equal(batchButtons().length,0);
delayed.reject(new Error('预览未完成'));await pending;
assert.equal(chat.state.activeBatch,null);assert.equal(batchButtons().length,0);
const previousCount=calls.length;await chat.executeBatch('reply','apply');assert.equal(calls.length,previousCount);
api=async()=>({batchId:'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',actions:actions.map(item=>previewed(item,'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb')),errors:[]});
await chat.executeBatch('reply','preview');
api=async()=>{throw new Error('单项预览失败');};
await chat.executeAction('single','preview');
assert.equal(chat.state.activeBatch,null);assert.equal(chat.state.activePreview,'');
assert.equal(batchButtons().length,0);
""")

    def test_invalid_batch_credentials_do_not_enable_apply(self):
        """响应缺少真实预览凭据时可以展示错误，但不能得到写入入口。"""
        self.run_case(r"""
const actions=[action('a'),action('b')];
chat.setConversation(base('conversation-a',[proposal('reply',actions)]));allowWrites();
api=async()=>({batchId:'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',actions:actions.map(item=>({...previewed(item),preview:{}})),errors:[]});
await chat.executeBatch('reply','preview');
assert.equal(chat.state.activeBatch,null);assert.equal(batchButtons().length,0);
assert.match(get('chat-feedback').textContent,/凭据|核实/);
await chat.executeBatch('reply','apply');
assert.equal(calls.length,1);
""")

    def test_incomplete_batch_apply_response_stays_unknown_without_false_success(self):
        """只收到部分执行记录时不宣称整组成功，也不能保留可重复提交的确认入口。"""
        self.run_case(r"""
const actions=[action('a'),action('b')];
chat.setConversation(base('conversation-a',[proposal('reply',actions)]));allowWrites();
api=async(path)=>path.endsWith('/preview')
  ? {batchId:'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',actions:actions.map(item=>previewed(item)),errors:[]}
  : {actions:[{...actions[0],status:'applied'}]};
await chat.executeBatch('reply','preview');
await chat.executeBatch('reply','apply');
assert(actions.every(item=>item.status==='unknown'));
assert.equal(chat.state.activeBatch,null);assert.equal(batchButtons().length,0);
assert.match(get('chat-feedback').textContent,/未返回完整可核实/);
assert.doesNotMatch(get('chat-feedback').textContent,/已确认应用/);
await chat.executeBatch('reply','apply');assert.equal(calls.length,2);
""")

    def test_reuse_single_creates_local_message_and_previews_without_model_audio_or_write(self):
        """复用只发送来源编号并预览新提案，正文明确为本地操作且保留用户尚未发送的附件。"""
        self.run_case(r"""
const old=action('old'),source=proposal('source',[old]);
chat.setConversation(base('conversation-a',[source]));
chat.state.activePreview='old';chat.state.activeBatch={id:'old-batch'};
chat.state.attachments=[{kind:'upload',id:'unsent',name:'未发送.wav'}];
const fresh=reused('local',[action('fresh')]);
fresh.inputMode='audio';fresh.model='旧模型';fresh.reasoningSummary='旧来源摘要不应冒充新思考';
let saves=0;save=()=>{saves++;};
api=async(path,body)=>{
  if(path.endsWith('/reuse')){
    assert.equal(chat.state.activePreview,'');assert.equal(chat.state.activeBatch,null);
    assert.deepEqual(JSON.parse(JSON.stringify(body)),{messageId:'source'});
    return {conversation:base('conversation-a',[source,fresh]),messageId:'local'};
  }
  assert.equal(path,'/api/assistant/actions/fresh/preview');
  return {action:{...fresh.actions[0],status:'previewed',preview:{previewId:'fresh-preview'}}};
};
await chat.reuseMessage('source');
assert.equal(calls.length,2);assert.equal(saves,0);
assert.equal(calls[0][0],'/api/conversations/conversation-a/reuse');
assert.equal(chat.state.activePreview,'fresh');assert.equal(chat.state.status.writeEnabled,false);
assert.equal(chat.state.attachments[0].id,'unsent');assert.equal(old.status,'proposed');
const card=get('chat-history').children.at(-1);
assert.match(card.textContent,/本地复用/);assert.match(card.textContent,/复用目标选区摘要/);
assert.doesNotMatch(card.textContent,/未提供可展示的思考摘要|本次请求包含音频|旧来源摘要|旧模型/);
assert.equal(get('chat-history').querySelectorAll('[data-action-apply]').at(-1).disabled,true);
assert.equal(calls.some(([path])=>/messages|apply|write|upload/.test(path)),false);
""")

    def test_reuse_batch_previews_only_new_eligible_parameters_once(self):
        """部分参数被后端排除后，自动组合预览只使用新消息内剩余项目；双击不会重复追加。"""
        self.run_case(r"""
const source=proposal('source',[action('a'),action('b','breathiness'),action('c','pitchCurve')]);
chat.setConversation(base('conversation-a',[source]));
const fresh=reused('local',[action('new-a'),action('new-b','breathiness')]),cloning=deferred();
fresh.text+=' 原生音高当前不可用，已排除。';
api=(path,body)=>path.endsWith('/reuse')?cloning.promise:Promise.resolve({batchId:'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
  actions:fresh.actions.map(item=>previewed(item)),errors:[]});
const pending=chat.reuseMessage('source');
assert.equal(reuseButtons()[0].disabled,true);
await chat.reuseMessage('source');assert.equal(calls.length,1);
cloning.resolve({conversation:base('conversation-a',[source,fresh]),messageId:'local'});await pending;
assert.equal(calls.length,2);assert.equal(calls[1][0],'/api/assistant/batches/preview');
assert.deepEqual(Array.from(calls[1][1].actionIds),['new-a','new-b']);
assert.deepEqual(Array.from(chat.state.activeBatch.actionIds),['new-a','new-b']);
assert.equal(batchButtons().length,1);assert.equal(batchButtons()[0].disabled,true);
assert.match(get('chat-history').textContent,/已排除/);
assert.equal(calls.some(([path])=>path.endsWith('/apply')),false);
""")

    def test_reuse_guards_unknown_results_busy_and_disconnect(self):
        """断连、忙碌和未核实写入都不能通过复制建议生成新的可执行提案。"""
        self.run_case(r"""
const one=action('a'),source=proposal('source',[one]);
chat.setConversation(base('conversation-a',[source]));
for(const status of ['unknown','invalid']){
  one.status=status;chat.renderHistory();assert.equal(reuseButtons()[0].disabled,true);
  await chat.reuseMessage('source');
}
one.status='applied';one.result={verified:false};chat.renderHistory();
assert.equal(reuseButtons()[0].disabled,true);assert.match(reuseButtons()[0].title,/未核实/);
await chat.reuseMessage('source');
one.result={verified:true};chat.renderHistory();assert.equal(reuseButtons()[0].disabled,false);
chat.state.status.bridge.connected=false;chat.syncControls();assert.equal(reuseButtons()[0].disabled,true);
await chat.reuseMessage('source');chat.state.status.bridge.connected=true;
for(const flag of ['loading','sending','metadataBusy','renderModeSaving','manualBusy','settingsSaving']){
  chat.state[flag]=true;chat.syncControls();assert.equal(reuseButtons()[0].disabled,true);
  await chat.reuseMessage('source');chat.state[flag]=false;
}
assert.equal(calls.length,0); // 检查不依赖按钮 disabled，直接调用入口也不能绕过保护。
""")

    def test_reuse_lost_response_requires_history_read_before_retry(self):
        """复制可能已持久化但响应丢失时不自动重试，来源锁须在主动重读历史后解除。"""
        self.run_case(r"""
const source=proposal('source',[action('a')]),conversation=base('conversation-a',[source]);
chat.setConversation(conversation);chat.state.activePreview='old';
api=async(path)=>{if(path.endsWith('/reuse'))throw new Error('响应连接中断');return conversation;};
await chat.reuseMessage('source');
assert.equal(chat.state.activePreview,'');assert.equal(chat.state.activeBatch,null);
assert.equal(reuseButtons()[0].disabled,true);assert.match(get('chat-feedback').textContent,/重新打开会话/);
await chat.reuseMessage('source');assert.equal(calls.length,1);
await chat.openConversation('conversation-a');assert.equal(reuseButtons()[0].disabled,false);
await chat.reuseMessage('source');assert.equal(calls.filter(([path])=>path.endsWith('/reuse')).length,2);
assert.equal(calls.some(([path])=>path.endsWith('/preview')),false);
""")

    def test_reuse_business_rejection_can_retry_after_selection_change(self):
        """明确的 400 业务拒绝没有追加消息，用户换好选区后可以主动再次尝试。"""
        self.run_case(r"""
chat.setConversation(base('conversation-a',[proposal('source',[action('a')])]));
api=async()=>{const error=new Error('当前音符节奏不匹配');error.httpStatus=400;throw error;};
await chat.reuseMessage('source');
assert.equal(reuseButtons()[0].disabled,false);assert.match(get('chat-feedback').textContent,/节奏不匹配/);
assert.doesNotMatch(get('chat-feedback').textContent,/已追加/);
await chat.reuseMessage('source');assert.equal(calls.length,2);
assert.equal(chat.state.conversation.messages.length,1);
""")

    def test_reuse_rejects_old_or_malformed_tickets_and_does_not_preview_on_disconnect(self):
        """畸形响应不能激活历史票据；复制期间断连则保留新建议，重连后仍由用户发起预览。"""
        self.run_case(r"""
const source=proposal('source',[action('a')]),conversation=base('conversation-a',[source]);
chat.setConversation(conversation);
api=async()=>({conversation,messageId:'source'});
await chat.reuseMessage('source');assert.equal(calls.length,1);assert.equal(chat.state.activePreview,'');
assert.match(get('chat-feedback').textContent,/未返回可核实/);
api=async()=>conversation;await chat.openConversation('conversation-a');
const fresh=reused('local',[action('b')]);
api=async()=>{
  emit('synthv:state',{status:{bridge:{connected:false},writeEnabled:false},manualBusy:false,settingsSaving:false});
  return {conversation:base('conversation-a',[source,fresh]),messageId:'local'};
};
await chat.reuseMessage('source');
assert.equal(chat.state.conversation.messages.length,2);assert.equal(chat.state.activePreview,'');
assert.match(get('chat-feedback').textContent,/尚未预览或修改工程/);
emit('synthv:state',{status:{bridge:{connected:true},writeEnabled:false},manualBusy:false,settingsSaving:false});
assert.equal(calls.some(([path])=>path.endsWith('/preview')),false);
""")


if __name__ == "__main__":
    unittest.main()
