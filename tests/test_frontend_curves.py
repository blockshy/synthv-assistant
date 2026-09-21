"""隔离验证乐谱参考与浏览器音高草稿；不访问宿主、音频、模型或私有选区。"""

from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "曲线前端回归需要开发环境提供 Node.js")
class FrontendCurveTests(unittest.TestCase):
    """执行生产 JavaScript，仅替换 DOM 与 Canvas，检查造成误读的真实坐标边界。"""

    def run_script(self, script):
        result = subprocess.run([NODE, "-e", script], cwd=ROOT, capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_note_aligned_draft_preserves_short_notes_and_transposition(self):
        self.run_script(r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('web/curves.js', 'utf8');
const start = source.indexOf('  function previewNotes(');
const end = source.indexOf('  function plot(', start);
assert(start >= 0 && end > start);
// 直接执行实际私有计算函数；不复制草稿算法，也不建立虚构宿主响应。
const context = {finite: (v) => typeof v === 'number' && Number.isFinite(v), clamp: (v, a, b) => Math.max(a, Math.min(b, v))};
vm.createContext(context);
vm.runInContext(source.slice(start, end), context);
const selection = {startSeconds: 101, endSeconds: 105, groupPitchOffset: 5, notes: [
  {onsetSeconds:101, durationSeconds:1.995, pitch:60},
  {onsetSeconds:102.995, durationSeconds:.01, pitch:64},
  {onsetSeconds:103.005, durationSeconds:1.995, pitch:62},
]};
const notes = context.selectionNotes(selection);
assert.equal(notes.length, 3);
assert.equal(notes[1].pitch, 69);
assert(Math.abs(notes[1].startPosition - .49875) < 1e-10);
const points = context.notePitchPoints(selection);
assert.equal(points.length, 6);
assert.equal(points[0][0], 0);
assert.equal(points.at(-1)[0], 1);
assert.deepEqual(Array.from(points, p => p[1]), [65,65,69,69,67,67]);
assert(points.every((p, i) => !i || p[0] > points[i-1][0]));
// 10 毫秒音符仍有独立稳定区；不能只留头尾造成整句长滑音。
assert(points[3][0] - points[2][0] > .001);
assert.throws(() => context.notePitchPoints({...selection, notes: selection.notes.slice(0, 1).map(n => ({...n, durationSeconds:NaN}))}), /完整音符/);
assert.throws(() => context.notePitchPoints({...selection, notes: [{onsetSeconds:101,durationSeconds:3,pitch:60},{onsetSeconds:102,durationSeconds:2,pitch:64}]}), /重叠音符/);
assert.throws(() => context.notePitchPoints({startSeconds:0,endSeconds:33,groupPitchOffset:0,notes:Array.from({length:33},(_,i)=>({onsetSeconds:i,durationSeconds:1,pitch:60+i%3}))}), /32 个音符/);
// 笔迹压缩保留拐点；外部音符锚点由调用者保持，不能在固定网格上重新量化。
const fitted = context.fitStroke([[.2,60],[.3,60],[.4,63],[.5,60],[.6,60]], 3);
assert.deepEqual(Array.from(fitted, p => Array.from(p)), [[.2,60],[.4,63],[.6,60]]);
assert.throws(() => context.fitStroke([[.2,60],[.3,60]], 1), /64 点/);
""")

    def test_pitch_offset_overlay_tracks_notes_without_bridging_rests(self):
        self.run_script(r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('web/curves.js', 'utf8');
const start = source.indexOf('  function previewNotes(');
const end = source.indexOf('  function plot(', start);
const context = {finite: (v) => typeof v === 'number' && Number.isFinite(v), clamp: (v, a, b) => Math.max(a, Math.min(b, v))};
vm.createContext(context);
vm.runInContext(source.slice(start, end), context);
const notes = [
  {startPosition:0,endPosition:.4,pitch:60},
  {startPosition:.4,endPosition:.401,pitch:67},
  {startPosition:.401,endPosition:.65,pitch:64},
  {startPosition:.8,endPosition:1,pitch:62},
];
// 此处刻意只提供头尾采样：比显示采样间隔短的音符也必须拥有独立参考段。
const series = [{before:true,points:[[0,0],[1,0]]}, {points:[[0,100],[1,100]]}];
const controls = [{position:0,value:100},{position:.4,value:20},{position:.7,value:0},{position:1,value:0}];
const result = context.pitchOverlay(notes, series, controls);
assert(result);
const before = result.series.filter(line => line.before);
const after = result.series.filter(line => !line.before);
for (const [lines, offset] of [[before,0],[after,1]]) {
  const points = lines.flatMap(line => line.points).filter(point => Number.isFinite(point[1]));
  for (const note of notes) {
    // 100 音分转换为 1 个 MIDI 半音；升降旋律、短音符及各段端点都必须保留。
    assert(points.some(point => Math.abs(point[0]-note.startPosition)<1e-9 && Math.abs(point[1]-note.pitch-offset)<1e-9));
    assert(points.some(point => Math.abs(point[0]-note.endPosition)<1e-9 && Math.abs(point[1]-note.pitch-offset)<1e-9));
  }
  for (const line of lines) {
    let previous = null;
    for (const point of line.points) {
      if (!Number.isFinite(point[1])) {previous=null; continue;}
      if (previous) {
        // 不能跨过不同基准音高直接连斜线，也不能在休止处画出不存在的演唱音高。
        assert.equal(point[1],previous[1]);
        assert(!(previous[0] <= .65 && point[0] >= .8));
      }
      previous=point;
    }
  }
}
assert.equal(context.pitchNoteAt(notes,.4).pitch,67); // 相邻交界的节点唯一归属后一个音符。
assert.equal(context.pitchNoteAt(notes,1).pitch,62); // 最后一个结束位置仍允许显示端点。
assert.equal(context.pitchNoteAt(notes,.7),null);
assert.deepEqual(Array.from(result.controlPoints, point => [point.position,point.value]), [[0,61],[.4,67.2],[1,62]]);
assert.equal(result.omittedPoints,1); // 休止中的真实节点仅省略显示，不制造新的节点。
assert.equal(controls.length,4); // 显示层不能修改宿主返回的原始数据。
const missingBefore = context.pitchOverlay(notes,[{before:true,points:[[0,null],[1,null]]},{points:[[0,0],[1,0]]}],[]);
assert(missingBefore.series.filter(line=>line.before).every(line=>line.points.every(point=>!Number.isFinite(point[1]))));
const incompleteBefore = context.pitchOverlay(notes,[{before:true,points:[[0,null],[.4,0],[1,0]]},{points:[[0,0],[1,0]]}],[]);
assert(incompleteBefore.series.filter(line=>line.before).flatMap(line=>line.points)
  .every(point=>point[0]>=.4 || !Number.isFinite(point[1]))); // 未知区间不能通过插值补成零音分。
assert.equal(context.pitchOverlay([],series,controls),null);
assert.equal(context.pitchOverlay([{startPosition:0,endPosition:.7,pitch:60},{startPosition:.5,endPosition:1,pitch:64}],series,controls),null);
const roundedBoundary=[{startPosition:0,endPosition:.5+3e-14,pitch:60},{startPosition:.5,endPosition:1,pitch:64}];
const originalBoundary=JSON.stringify(roundedBoundary);
const roundedOverlay=context.pitchOverlay(roundedBoundary,series,[{position:.5,value:25}]);
assert(roundedOverlay); // 秒坐标归一化带来的极小浮点重叠，不能把真实相邻音符误判成复音。
assert.equal(context.pitchNoteAt(roundedBoundary,.5).pitch,64);
assert.deepEqual(Array.from(roundedOverlay.controlPoints,point=>[point.position,point.value]),[[.5,64.25]]);
assert.equal(JSON.stringify(roundedBoundary),originalBoundary); // 对齐仅用于显示副本，不改宿主历史选区。
const realOverlap=[{startPosition:0,endPosition:.500001,pitch:60},{startPosition:.5,endPosition:1,pitch:64}];
assert.equal(context.pitchOverlay(realOverlap,series,controls),null);
assert.equal(context.pitchNoteAt(realOverlap,.5),null); // 超出浮点误差范围的重叠仍不能猜测唯一音符。
const edgeNotes=[{startPosition:0,endPosition:.5,pitch:0},{startPosition:.5,endPosition:1,pitch:127}];
const edgeOverlay=context.pitchOverlay(edgeNotes,[{points:[[0,-100],[1,100]]}],
  [{position:0,value:-100},{position:1,value:100}]);
assert.deepEqual(Array.from(edgeOverlay.controlPoints, point=>point.value),[-1,128]);
const referenceGeometry=context.chartGeometry(480,[-1,128],edgeNotes,true,true);
assert(referenceGeometry.parameter.range[0]<=-1 && referenceGeometry.parameter.range[1]>=128);
const nativeGeometry=context.chartGeometry(480,[-1,128],edgeNotes,true,false);
assert(nativeGeometry.parameter.range[0]>=-.5 && nativeGeometry.parameter.range[1]<=127.5); // 参考域扩展不改变原生 MIDI 域。
""")

    def test_preview_uses_saved_notes_and_keeps_unknown_baseline_unknown(self):
        self.run_script(r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const drawings = [];
/** Canvas 只记录几何数据；页面私有状态与真实工程不能进入这个测试。 */
function element(tag) {
  const element = {tag, children:[], dataset:{}, attrs:{}, isConnected:true,
    classList:{toggle(){}}, append(...items){this.children.push(...items);},
    setAttribute(name,value){this.attrs[name]=value;}, getAttribute(name){return this.attrs[name];}, getBoundingClientRect:()=>({width:480})};
  if (tag === 'canvas') {
    const recording = {rects:[], notes:[], circles:[], texts:[], lines:[], path:[], clips:[]}; drawings.push(recording);
    element.getContext = () => ({scale(){}, clearRect(){}, save(){}, restore(){}, rect(...rectangle){recording.clips.push(rectangle);}, clip(){},
      setLineDash(){}, fillRect(...r){recording.rects.push(r);}, strokeRect(...r){recording.notes.push(r);},
      arc(...circle){recording.circles.push(circle);},fill(){},
      fillText(text){recording.texts.push(text);}, beginPath(){recording.path=[];},
      moveTo(...p){recording.path.push(p);}, lineTo(...p){recording.path.push(p);},
      stroke(){recording.lines.push(recording.path.slice());}});
  }
  return element;
}
const context = {window:{addEventListener(){},devicePixelRatio:1},document:{createElement:element, documentElement:{},getElementById(){},querySelectorAll:()=>[]},
  getComputedStyle:()=>({getPropertyValue:()=> '#888'}),requestAnimationFrame:(callback)=>callback()};
vm.createContext(context);
vm.runInContext(fs.readFileSync('web/curves.js','utf8'),context);
const preview = {parameter:'pitchCurve',unit:'MIDI 半音',startSeconds:10,endSeconds:12,
  curvePreview:[{position:0,before:null,after:60},{position:.5,before:null,after:61},{position:1,before:null,after:62}],
  notes:[{startPosition:0,endPosition:.5,pitch:60},{startPosition:.5,endPosition:1,pitch:62}]};
const figure = context.window.SynthVCurves.createPreview(preview);
assert.equal(figure.children[0].tag,'canvas');
assert.match(figure.children[0].attrs['aria-label'],/2 个乐谱音符/);
assert.match(figure.children[1].textContent,/未提供原曲线/);
assert.match(figure.children[1].textContent,/不代表实际演唱音高/);
const draw=drawings[0];
assert.equal(draw.notes.length,2);
assert.equal(draw.notes[0][0],64); // 键盘与曲线统一留出音名的空间。
assert.equal(draw.notes[0][0]+draw.notes[0][2],draw.notes[1][0]);
assert(draw.notes[0][1]>draw.notes[1][1]); // 高音符显示在低音符上方。
assert(draw.texts.includes('C4') && draw.texts.includes('D4'));
assert(draw.texts.includes('10 s') && draw.texts.includes('12 s'));
assert(draw.lines.some(line=>line.length===0)); // 未知前值保持空路径，未补出零线。
assert.equal(figure.children[2].children[1].children.length,2);
const legacy = context.window.SynthVCurves.createPreview({...preview,notes:undefined});
assert.equal(drawings[1].notes.length,0);
assert.match(legacy.children[1].textContent,/未提供音符位置/);
const automation = context.window.SynthVCurves.createPreview({...preview,parameter:'pitchDelta',unit:'音分',renderMode:'points',
  controlPoints:[{position:0,value:0},{position:.37,value:100},{position:1,value:0}],
  curvePreview:[{position:0,before:0,after:0},{position:1,before:0,after:0}]});
assert.match(automation.children[1].textContent,/参考/);
assert.match(automation.children[1].textContent,/合成|演唱/); // 明确是乐谱加偏移参考，不能冒充宿主生成基频。
assert(drawings[2].notes[0][1]>drawings[2].notes[1][1]);
assert.equal(drawings[2].circles.length,3); // 真实节点仍为三个，不能替换成两个显示采样点。
assert.equal(drawings[2].circles[0][0],drawings[2].notes[0][0]);
assert(Math.abs(drawings[2].circles[1][0]-(64+.37*(480-12-64)))<1e-9);
const noteCenter = rectangle => rectangle[1]+rectangle[3]/2;
assert(Math.abs(drawings[2].circles[0][1]-noteCenter(drawings[2].notes[0]))<1e-9);
assert(Math.abs(drawings[2].circles[2][1]-noteCenter(drawings[2].notes[1]))<1e-9);
// 首音的 +100 音分节点应恰好位于 C4 与 D4 中间，而非落入独立的下方参数轨。
assert(Math.abs(drawings[2].circles[1][1]-(noteCenter(drawings[2].notes[0])+noteCenter(drawings[2].notes[1]))/2)<1e-9);
const oldPoints = context.window.SynthVCurves.createPreview({...preview,parameter:'tension',renderMode:'points'});
assert.equal(drawings[3].circles.length,0);
assert.match(oldPoints.children[1].textContent,/未提供实际控制点/);
const smooth = context.window.SynthVCurves.createPreview({...preview,controlPoints:[{position:.5,value:61}],renderMode:'smooth'});
assert.equal(drawings[4].circles.length,0); // 绘制模式保持连续线条，不伪装成控制点模式。
const smoothAutomation = context.window.SynthVCurves.createPreview({...preview,parameter:'pitchDelta',unit:'音分',renderMode:'smooth',
  controlPoints:[{position:0,value:0},{position:1,value:0}],curvePreview:[{position:0,before:0,after:0},{position:1,before:0,after:0}]});
assert.equal(drawings[5].circles.length,0);
assert.equal(smoothAutomation.children[0].height,320); // 两种绘制模式都应共享单个钢琴卷帘。
const tension = context.window.SynthVCurves.createPreview({...preview,parameter:'tension',unit:'参数值',renderMode:'points',
  controlPoints:[{position:0,value:.1},{position:1,value:.1}],curvePreview:[{position:0,before:0,after:.1},{position:1,before:0,after:.1}]});
assert.match(tension.children[1].textContent,/共用时间轴/);
assert.equal(tension.children[0].height,440);
assert(drawings[6].circles.every(circle=>circle[1]>drawings[6].notes[0][1]+drawings[6].notes[0][3])); // 张力没有 MIDI 含义，仍使用原单位轨。
const missingNotes = context.window.SynthVCurves.createPreview({...preview,parameter:'pitchDelta',unit:'音分',renderMode:'points',notes:undefined,
  controlPoints:[{position:0,value:10},{position:1,value:10}],curvePreview:[{position:0,before:0,after:10},{position:1,before:0,after:10}]});
assert.equal(drawings[7].notes.length,0);
assert.equal(drawings[7].circles.length,2); // 历史数据缺少音符时保留原音分预览，不伪造音符基准。
assert.match(missingNotes.children[1].textContent,/未提供音符位置/);
for (const [pitch,delta] of [[0,-100],[127,100]]) {
  context.window.SynthVCurves.createPreview({...preview,parameter:'pitchDelta',unit:'音分',renderMode:'points',
    notes:[{startPosition:0,endPosition:1,pitch}],controlPoints:[{position:0,value:delta},{position:1,value:delta}],
    curvePreview:[{position:0,before:0,after:delta},{position:1,before:0,after:delta}]});
  const edge=drawings.at(-1),clip=edge.clips.at(-1);
  assert.equal(edge.circles.length,2);
  // MIDI 0 以下和 127 以上的参考节点仍完整落在绘图区，不能只在坐标换算后被 Canvas 裁掉。
  assert(edge.circles.every(circle=>circle[1]-circle[2]>=clip[1] && circle[1]+circle[2]<=clip[1]+clip[3]));
  assert(delta<0 ? edge.circles[0][1]>noteCenter(edge.notes[0]) : edge.circles[0][1]<noteCenter(edge.notes[0]));
}
""")

    def test_editor_converts_piano_roll_strokes_back_to_parameter_units(self):
        self.run_script(r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const fields = new Map();
const notes = [];
/** 仅模拟编辑器需要的 DOM 接口，手势仍调用生产事件处理与生产 getPayload。 */
function element(tag='div') {
  const listeners = new Map(), captures = new Set();
  const item = {tag,children:[],value:'',dataset:{},classList:{toggle(){}},
    append(...children){this.children.push(...children);}, replaceChildren(...children){this.children=children;},
    setAttribute(){}, querySelector(){return {};}, addEventListener(name,listener){listeners.set(name,listener);},
    fire(name,event={}){listeners.get(name)?.(event);},
    getBoundingClientRect:()=>({left:17,top:23,width:480}),
    setPointerCapture(id){captures.add(id);}, hasPointerCapture(id){return captures.has(id);}, releasePointerCapture(id){captures.delete(id);}};
  item.getContext = () => ({scale(){},clearRect(){notes.length=0;},save(){},restore(){},rect(){},clip(){},setLineDash(){},
    fillRect(){},strokeRect(...rectangle){notes.push(rectangle);},arc(){},fill(){},fillText(){},beginPath(){},moveTo(){},lineTo(){},stroke(){}});
  return item;
}
function field(id) {if (!fields.has(id)) fields.set(id,element(id==='curve-editor-canvas'?'canvas':'div'));return fields.get(id);}
field('parameter-method').value='delta';
field('parameter-render-mode').value='smooth';
const context = {window:{addEventListener(){},devicePixelRatio:1},
  document:{createElement:element,documentElement:{},getElementById:field,querySelectorAll:()=>[]},
  Option:function(text,value){return {text,value};},getComputedStyle:()=>({getPropertyValue:()=> '#888'}),
  requestAnimationFrame:(callback)=>callback()};
vm.createContext(context);
vm.runInContext(fs.readFileSync('web/curves.js','utf8'),context);
const editor = context.window.SynthVCurves.createEditor();
editor.setSelection({startSeconds:10,endSeconds:20,groupPitchOffset:5,
  capabilities:{curves:true,nativePitch:true},parameters:{pitchCurve:{available:true}},
  notes:[{onsetSeconds:10,durationSeconds:4,pitch:60},{onsetSeconds:16,durationSeconds:4,pitch:64}]});
editor.setBusy(false,true);
assert(editor.selectParameter('pitchDelta'));
field('parameter-method').value='curve'; field('parameter-method').fire('change');
const canvas=field('curve-editor-canvas');
const center=rectangle=>rectangle[1]+rectangle[3]/2;
function stroke(position,noteIndex,semitones) {
  const pixelsPerSemitone=(center(notes[0])-center(notes[1]))/4;
  const event={button:0,isPrimary:true,pointerId:1,preventDefault(){},
    clientX:17+notes[0][0]+position*(notes[1][0]+notes[1][2]-notes[0][0]),
    clientY:23+center(notes[noteIndex])-semitones*pixelsPerSemitone};
  canvas.fire('pointerdown',event); canvas.fire('pointerup',event);
}
stroke(.2,0,.5);
let payload=editor.getPayload();
assert.equal(payload.parameter,'pitchDelta');
assert.equal(payload.renderMode,'smooth');
assert(payload.curve.some(point=>point[0]===.2 && point[1]===50)); // 上移半个半音，提交 +50 音分，不能提交 65.5。
field('parameter-render-mode').value='points'; field('parameter-render-mode').fire('change');
stroke(.8,1,-.25);
payload=editor.getPayload();
assert.equal(payload.renderMode,'points');
assert(payload.curve.some(point=>point[0]===.8 && point[1]===-25));
const saved=JSON.stringify(payload.curve);
stroke(.5,0,0);
assert.equal(JSON.stringify(editor.getPayload().curve),saved); // 休止处没有唯一音符基准，点击不得改变提交数据。
assert(editor.selectParameter('pitchCurve'));
stroke(.2,0,.5);
payload=editor.getPayload();
assert.equal(payload.parameter,'pitchCurve');
assert(payload.curve.some(point=>point[0]===.2 && point[1]===65.5)); // 原生音高仍提交包含组移调的绝对 MIDI。
for (const [pitches,noteIndex,positions] of [[[0,4],0,[.2,.3]],[[123,127],1,[.7,.8]]]) {
  editor.setSelection({startSeconds:10,endSeconds:20,groupPitchOffset:0,
    capabilities:{curves:true,nativePitch:true},parameters:{pitchCurve:{available:true}},
    notes:[{onsetSeconds:10,durationSeconds:4,pitch:pitches[0]},{onsetSeconds:16,durationSeconds:4,pitch:pitches[1]}]});
  assert(editor.selectParameter('pitchDelta'));
  field('parameter-method').value='curve'; field('parameter-method').fire('change');
  for (const [index,semitones] of [-1,1].entries()) {
    stroke(positions[index],noteIndex,semitones);
    assert(editor.getPayload().curve.some(point=>point[0]===positions[index] && point[1]===semitones*100));
  }
  // MIDI 0 与 127 边界音符都可绘制完整 ±100 音分；视觉域放宽不能限制安全增量域。
  assert(editor.getPayload().curve.every(point=>point[1]>=-100 && point[1]<=100));
}
""")


if __name__ == "__main__":
    unittest.main()
