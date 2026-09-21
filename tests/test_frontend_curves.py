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
    setAttribute(name,value){this.attrs[name]=value;}, getBoundingClientRect:()=>({width:480})};
  if (tag === 'canvas') {
    const recording = {rects:[], texts:[], lines:[], path:[]}; drawings.push(recording);
    element.getContext = () => ({scale(){}, clearRect(){}, save(){}, restore(){}, rect(){}, clip(){},
      setLineDash(){}, fillRect(...r){recording.rects.push(r);}, strokeRect(){},
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
assert.equal(draw.rects.length,2);
assert.equal(draw.rects[0][0],54); // 原生 MIDI 轴留出音名和数值的空间。
assert.equal(draw.rects[0][0]+draw.rects[0][2],draw.rects[1][0]);
assert(draw.rects[0][1]>draw.rects[1][1]); // 高音符显示在低音符上方。
assert(draw.texts.includes('10 s') && draw.texts.includes('12 s'));
assert(draw.lines.some(line=>line.length===0)); // 未知前值保持空路径，未补出零线。
assert.equal(figure.children[2].children[1].children.length,2);
const legacy = context.window.SynthVCurves.createPreview({...preview,notes:undefined});
assert.equal(drawings[1].rects.length,0);
assert.match(legacy.children[1].textContent,/未提供音符位置/);
const automation = context.window.SynthVCurves.createPreview({...preview,parameter:'pitchDelta',unit:'音分',curvePreview:[{position:0,before:0,after:10},{position:1,before:0,after:10}]});
assert.match(automation.children[1].textContent,/单独显示在时间条/);
assert(drawings[2].rects.every(rect=>rect[1]===20));
""")


if __name__ == "__main__":
    unittest.main()
