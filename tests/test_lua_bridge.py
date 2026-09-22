"""使用独立 Lua VM 模拟宿主，验证真实桥接源码的编辑保护和曲线边界。

测试不会加载 SynthV、访问 SVP 或修改安装目录；lupa 仅为可选测试依赖。
曲线替身实现线性与三次 Hermite 插值，用于复现添加锚点影响相邻片段的问题。
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time
import unittest

try:
    # 明确选择与 SynthV 官方环境一致的 Lua 5.4，避免依赖 lupa 的默认版本。
    from lupa.lua54 import LuaRuntime
except ImportError:
    LuaRuntime = None


STUB = r'''
-- 所有可变状态均属于测试虚拟机，不引用真实宿主对象。
host={project="test.svp",tempo=120,references=1,undos=0,mutations=0,clones=0,method="Linear"}
local function copy(points)
  local result={}; for i,p in ipairs(points) do result[i]={p[1],p[2]} end
  return result
end
local Curve={}; Curve.__index=Curve
function Curve:getAllPoints()
  local points=copy(self.points)
  -- 某些绑定会把相同整数在不同调用中返回为浮点数，模拟这种表示差异。
  if host.readFloats then for _,p in ipairs(points) do p[1]=p[1]+0.0; p[2]=p[2]+0.0 end end
  return points
end
function Curve:getDefinition() return {range={-1,1},defaultValue=0} end
function Curve:getInterpolationMethod() return self.method or host.method end
function Curve:removeAll()
  if not self.cloned then
    host.mutations=host.mutations+1
    self.writeRound=(self.writeRound or 0)+1
  end
  self.points={}
end
function Curve:add(x,y)
  if not self.cloned then host.mutations=host.mutations+1 end
  -- float32 量化和同位置替换与真实 Automation 的读回行为保持一致。
  if host.quantize then y=string.unpack("f",string.pack("f",y)) end
  if not self.cloned and host.corruptRounds and host.corruptRounds[self.writeRound] then y=y+0.001 end
  for _,point in ipairs(self.points) do
    if point[1]==x then
      host.duplicateAdds=(host.duplicateAdds or 0)+1
      point[2]=y; return
    end
  end
  self.points[#self.points+1]={x,y}
  table.sort(self.points,function(a,b) return a[1]<b[1] end)
end
function Curve:clone()
  host.clones=host.clones+1
  return setmetatable({points=copy(self.points),method=self:getInterpolationMethod(),cloned=true},Curve)
end
function Curve:get(x)
  local p=self.points
  if #p==0 then return 0 end
  if x<=p[1][1] then return p[1][2] end
  if x>=p[#p][1] then return p[#p][2] end
  for i=1,#p-1 do
    if x<=p[i+1][1] then
      local a,b=p[i],p[i+1]
      local length=b[1]-a[1]; local t=(x-a[1])/length
      if self:getInterpolationMethod():lower()=="linear" then return a[2]+t*(b[2]-a[2]) end
      -- 三次 Hermite 的切线依赖相邻控制点，能够暴露选区外的曲线漂移。
      local previous,nextp=p[math.max(1,i-1)],p[math.min(#p,i+2)]
      local m0=(b[2]-previous[2])/(b[1]-previous[1])
      local m1=(nextp[2]-a[2])/(nextp[1]-a[1])
      -- 允许不同三次切线权重，验证保护算法依赖宿主读回而非猜测一种样条公式。
      m0=m0*(host.cubicSlopeFactor or 1); m1=m1*(host.cubicSlopeFactor or 1)
      return (2*t^3-3*t^2+1)*a[2]+(t^3-2*t^2+t)*length*m0
        +(-2*t^3+3*t^2)*b[2]+(t^3-t^2)*length*m1
    end
  end
end
host.curve=setmetatable({points={{0,0},{3000,0}}},Curve)
local note={}
function note:getOnset() return 1000 end
function note:getDuration() return 1000 end
function note:getPitch() return 60 end
function note:getLyrics() return "啦" end
function note:getIndexInParent() return 1 end
local axis={}
function axis:getAllTempoMarks() return {{position=0,bpm=host.tempo}} end
function axis:getSecondsFromBlick(b) return b/1000*120/host.tempo end
function axis:getBlickFromSeconds(s) return s*1000*host.tempo/120 end
local group={}
function group:getUUID() return "group-test" end
function group:getName() return "测试组" end
function group:getParameter(_) return host.curve end
local ref={}
function ref:getTarget() return group end
function ref:getTimeOffset() return 0 end
function ref:getPitchOffset() return host.pitchOffset or 0 end
function ref:getVoice() return host.voice or {} end
function ref:isMain() return true end
function ref:isInstrumental() return false end
local track={}
function track:getNumGroups() return host.references end
function track:getGroupReference(_) return ref end
local project={}
function project:getFileName() return host.project end
function project:getTimeAxis() return axis end
function project:getNumTracks() return 1 end
function project:getTrack(_) return track end
function project:newUndoRecord() host.undos=host.undos+1 end
local selection={}
function selection:getSelectedNotes() return {note} end
local editor={}
function editor:getCurrentGroup() return ref end
function editor:getCurrentTrack() return track end
function editor:getSelection() return selection end
SV={}
function SV:getProject() return project end
function SV:getMainEditor() return editor end
function SV:getHostInfo() return {hostVersion="mock-2.2.1"} end
function SV:setTimeout(_,callback) host.queued=(host.queued or 0)+1; host.callback=callback end
function SV:finish() host.finished=true end
'''


@unittest.skipIf(LuaRuntime is None, "可选 Lua 测试依赖 lupa 未安装")
class LuaBridgeTests(unittest.TestCase):
    """直接执行生产 Lua 的分发函数，避免复制业务逻辑形成无效测试。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        self.lua.globals().IPC_DIR = self.temporary.name.replace("\\", "/")
        self.lua.execute(STUB)
        root = Path(__file__).resolve().parents[1] / "synthv"
        parser = (root / "json.lua").read_text(encoding="utf-8")
        pitch = (root / "pitch.lua").read_text(encoding="utf-8")
        bridge = (root / "bridge.lua").read_text(encoding="utf-8")
        # 同一代码块末尾暴露局部分发器，仅用于测试；生产文件本身不增加后门。
        self.dispatch, self.poll, self.session = self.lua.execute(
            "local json=(function()\n" + parser + "\nend)()\nlocal NativePitch=(function()\n" + pitch + "\nend)()\n" + bridge + r'''
return function(action,args)
  local ok,result=pcall(dispatch,action,args)
  return json.encode({ok=ok,result=ok and result or nil,error=not ok and tostring(result) or nil})
end, function() poll() end, function() return session end
''')
        self.host = self.lua.globals().host

    def call(self, action, **args):
        return json.loads(self.dispatch(action, self.lua.table_from(args, recursive=True)))

    def preview_id(self):
        result = self.call("preview", parameter="tension", delta=0.1)
        self.assertTrue(result["ok"], result.get("error"))
        return result["result"]["previewId"]

    def enable(self):
        result = self.call("write_mode", enabled=True, expectedProject="test.svp")
        self.assertTrue(result["ok"], result.get("error"))

    def test_preview_is_read_only_and_apply_restore_have_separate_undo_records(self):
        preview = self.preview_id()
        self.assertEqual(self.host.mutations, 0)
        self.assertEqual(self.host.clones, 1)
        self.enable()
        self.assertTrue(self.call("apply", previewId=preview)["ok"])
        self.assertEqual(self.host.undos, 1)
        self.assertTrue(self.call("restore")["ok"])
        self.assertEqual(self.host.undos, 2)
        self.assertEqual(len(self.host.curve.points), 2)

    def test_expected_project_mismatch_never_enables_writes(self):
        result = self.call("write_mode", enabled=True, expectedProject="other.svp")
        self.assertFalse(result["ok"])
        self.assertIn("工程已切换", result["error"])
        preview = self.preview_id()
        self.assertIn("只读", self.call("apply", previewId=preview)["error"])
        self.assertEqual(self.host.undos, 0)

    def test_tempo_change_invalidates_preview_before_any_write(self):
        preview = self.preview_id()
        self.enable()
        self.host.tempo = 90
        self.assertFalse(self.call("apply", previewId=preview)["ok"])
        self.assertEqual(self.host.mutations, 0)
        self.assertEqual(self.host.undos, 0)

    def test_new_shared_reference_blocks_apply_and_restore(self):
        preview = self.preview_id()
        self.enable()
        self.host.references = 2
        self.assertIn("共用", self.call("apply", previewId=preview)["error"])
        self.assertEqual(self.host.undos, 0)
        self.host.references = 1
        self.assertTrue(self.call("apply", previewId=preview)["ok"])
        self.host.references = 2
        self.assertIn("共用", self.call("restore")["error"])
        self.assertEqual(self.host.undos, 1)

    def test_interpolation_change_blocks_apply(self):
        preview = self.preview_id()
        self.enable()
        self.host.method = "Cubic"
        self.assertIn("插值方式", self.call("apply", previewId=preview)["error"])
        self.assertEqual(self.host.mutations, 0)

    def test_cubic_boundary_drift_is_rejected_without_host_mutation(self):
        self.host.method = "Cubic"
        # 非线性原曲线的区外切线依赖区内邻点；不能因为平直基线现已支持而删去此保护用例。
        self.lua.execute("host.curve.points={{0,0.2},{500,-0.2},{2400,0.4},{3000,0}}")
        result = self.call("preview", parameter="tension", delta=0.1)
        self.assertFalse(result["ok"])
        self.assertIn("选区以外", result["error"])
        self.assertNotIn("选择控制点模式", result["error"])
        self.assertEqual(self.host.mutations, 0)
        self.assertEqual(self.host.undos, 0)

    def test_outside_guards_preserve_actual_cubic_samples_and_restore_original_points(self):
        """左右区外片段都核验真实插值；保护点应用后可恢复完整的原控制点快照。"""
        for slope in (1, 0.5):
            for mirrored in (False, True):
                with self.subTest(slope=slope, mirrored=mirrored):
                    self.lua.execute('''
host.method="Cubic"; host.quantize=true; host.mutations=0; host.undos=0
host.curve.points={{0,0},{500,.00001},{2400,.00002},{3000,.00003}}
for _,p in ipairs(host.curve.points) do p[2]=string.unpack("f",string.pack("f",p[2])) end
''')
                    self.host.cubicSlopeFactor = slope
                    if mirrored:
                        self.lua.execute('''
local p={}; for index=#host.curve.points,1,-1 do
  local source=host.curve.points[index]; p[#p+1]={3000-source[1],-source[2]}
end; host.curve.points=p
''')
                    original = [[point[1], point[2]] for point in self.host.curve.points.values()]
                    positions = [index / 4 for index in range(-400, 12401)
                                 if index <= 4000 or index >= 8000]
                    before = [self.host.curve.get(self.host.curve, value) for value in positions]
                    preview = self.call("preview", parameter="breathiness", delta=0.05)
                    self.assertTrue(preview["ok"], preview.get("error"))
                    self.assertTrue(any("区外保护点" in warning for warning in preview["result"]["capabilityWarnings"]))
                    self.assertEqual(self.host.mutations, 0)
                    self.assertEqual(self.host.undos, 0)
                    self.enable()
                    applied = self.call("apply", previewId=preview["result"]["previewId"])
                    self.assertTrue(applied["ok"], applied.get("error"))
                    self.assertEqual(self.host.curve.getInterpolationMethod(self.host.curve), "Cubic")
                    after = [self.host.curve.get(self.host.curve, value) for value in positions]
                    self.assertLessEqual(max(abs(left - right) for left, right in zip(before, after)), 1e-7)
                    restored = self.call("restore")
                    self.assertTrue(restored["ok"], restored.get("error"))
                    self.assertEqual([[point[1], point[2]] for point in self.host.curve.points.values()], original)
                    self.assertEqual([self.host.curve.get(self.host.curve, value) for value in positions], before)

    def test_default_smooth_mode_removes_redundant_points_without_changing_target(self):
        """相同增量优先保留转折，密集点模式仍可显式选择，两个预览均不写宿主。"""
        smooth = self.call("preview", parameter="tension", delta=0.1)["result"]
        dense = self.call("preview", parameter="tension", delta=0.1, renderMode="points")["result"]
        self.assertLess(smooth["pointCount"], dense["pointCount"] / 2)
        self.assertEqual(smooth["representation"], "automation-simplified")
        self.assertGreater(smooth["pointReduction"], 0)
        self.assertAlmostEqual(smooth["curvePreview"][48]["after"], 0.1)
        self.assertEqual(self.host.mutations, 0)

    def test_preview_nodes_match_applied_points_without_external_points_or_resampling(self):
        """真实节点与隔离宿主写后读回一一对应，变速、时间偏移和 float32 量化不改变契约。"""
        self.lua.execute('''
host.quantize=true
local ref=SV:getMainEditor():getCurrentGroup()
function ref:getTimeOffset() return 5000 end
local axis=SV:getProject():getTimeAxis()
function axis:getSecondsFromBlick(b)
  if b<=6500 then return b/1000 end
  return 6.5+(b-6500)/2000
end
function axis:getBlickFromSeconds(s)
  if s<=6.5 then return s*1000 end
  return 6500+(s-6.5)*2000
end
''')
        self.enable()
        for mode in ("smooth", "points"):
            with self.subTest(mode=mode):
                response = self.call("preview", parameter="tension", delta=0.100000001, renderMode=mode)
                self.assertTrue(response["ok"], response.get("error"))
                preview = response["result"]
                nodes = preview["controlPoints"]
                self.assertNotEqual(len(nodes), len(preview["curvePreview"]))
                self.assertEqual(nodes[0]["position"], 0)
                self.assertEqual(nodes[-1]["position"], 1)
                self.assertTrue(self.call("apply", previewId=preview["previewId"])["ok"])
                expected = []
                for point in self.host.curve.points.values():
                    blick, value = point[1], point[2]
                    if 1000 <= blick <= 2000:
                        absolute = blick + 5000
                        seconds = absolute / 1000 if absolute <= 6500 else 6.5 + (absolute - 6500) / 2000
                        expected.append({"position": (seconds - 6) / 0.75, "value": value})
                self.assertEqual(len(nodes), len(expected))
                for actual, target in zip(nodes, expected):
                    # Lua JSON 编码以有限有效数字输出，允许序列化舍入，不允许坐标变换漂移。
                    self.assertAlmostEqual(actual["position"], target["position"], places=13)
                    self.assertAlmostEqual(actual["value"], target["value"], places=13)
                # 完整写入快照另含两侧保护锚点和原区外节点，不能全部透传给图形。
                self.assertEqual(preview["pointCount"] - len(nodes), 4)
                self.assertTrue(self.call("restore")["ok"])

    def test_uniform_preview_contains_live_pitched_notes_with_tempo_and_group_offsets(self):
        """均匀增量也须自带本次宿主选区的音符坐标，不依赖调用者先读取选区。"""
        self.lua.execute('''
host.pitchOffset=12
local ref=SV:getMainEditor():getCurrentGroup()
function ref:getTimeOffset() return 5000 end
local axis=SV:getProject():getTimeAxis()
function axis:getSecondsFromBlick(b)
  if b<=6500 then return b/1000 end
  return 6.5+(b-6500)/2000
end
function axis:getBlickFromSeconds(s)
  if s<=6.5 then return s*1000 end
  return 6500+(s-6.5)*2000
end
local selected=SV:getMainEditor():getSelection()
local function note(onset,pitch,index)
  return {getOnset=function() return onset end,getDuration=function() return 500 end,
    getPitch=function() return pitch end,getIndexInParent=function() return index end,
    getLyrics=function() return "不得公开的歌词" end}
end
function selected:getSelectedNotes() return {note(1500,64,2),note(1000,60,1)} end
''')
        response = self.call("preview", parameter="tension", delta=0.1)
        self.assertTrue(response["ok"], response.get("error"))
        notes = response["result"]["notes"]
        self.assertEqual(len(notes), 2)
        self.assertEqual([note["pitch"] for note in notes], [72, 76])
        self.assertEqual(notes[0]["startPosition"], 0)
        self.assertAlmostEqual(notes[0]["endPosition"], 2 / 3)
        self.assertAlmostEqual(notes[1]["startPosition"], 2 / 3)
        self.assertEqual(notes[1]["endPosition"], 1)
        self.assertTrue(all(set(note) == {"startPosition", "endPosition", "pitch"} for note in notes))
        self.assertEqual(self.host.mutations, 0)

    def test_cubic_flat_baseline_has_zero_external_drift(self):
        """成对边缘锚点允许三次曲线中的平直片段生成稀疏预览，并守住选区外基线。"""
        self.host.method = "Cubic"
        preview = self.call("preview", parameter="tension", delta=0.1)
        self.assertTrue(preview["ok"], preview.get("error"))
        self.enable()
        self.assertTrue(self.call("apply", previewId=preview["result"]["previewId"])["ok"])
        for position in (0, 500, 999, 2001, 2500, 3000):
            self.assertAlmostEqual(self.host.curve.get(self.host.curve, position), 0, places=7)

    def test_cubic_float32_existing_curve_at_real_blick_scale_stays_sparse(self):
        """真实量级的时间单位和非零已有曲线不能让小幅均匀调整退化为密集点。"""
        # 旧替身将 1000 blick 当作一秒，会把“一 blick”误当作毫秒，无法覆盖
        # 真实宿主中极短锚点间距与 float32 数值量化共同出现的场景。
        # 本例仅替换时间轴与音符范围；仍运行生产桥接和已有的 Hermite 插值替身，
        # 不把替身描述为 SynthV 私有插值算法的精确复刻。
        self.lua.execute('''
local scale=700000000
local axis=SV:getProject():getTimeAxis()
function axis:getSecondsFromBlick(b) return b/scale end
function axis:getBlickFromSeconds(s) return s*scale end
local note=SV:getMainEditor():getSelection():getSelectedNotes()[1]
function note:getOnset() return scale end
function note:getDuration() return 4.5*scale end
host.method="Cubic"; host.quantize=true; host.curve.points={}
for index=0,225 do
  -- 位置按宿主整数 blick 保存，避免浮点乘法制造几乎重合的额外采样点。
  local position=math.floor(scale*(1+index*.02)+.5)
  local phase=math.max(0,math.min(1,(index*.02-.02)/4.46))
  local value=.1234567+.04*math.sin(math.pi*phase)^2
  -- 原曲线和后续候选都经过 float32；不能只量化新控制点而留下理想化基线。
  value=string.unpack("f",string.pack("f",value))
  host.curve.points[#host.curve.points+1]={position,value}
end
''')
        before = self.call("get_selection")["result"]["parameters"]["tension"]["fingerprint"]
        for parameter in ("breathiness", "tension"):
            with self.subTest(parameter=parameter):
                smooth_response = self.call("preview", parameter=parameter, delta=0.05)
                dense_response = self.call("preview", parameter=parameter, delta=0.05, renderMode="points")
                self.assertTrue(smooth_response["ok"], smooth_response.get("error"))
                self.assertTrue(dense_response["ok"], dense_response.get("error"))
                smooth, dense = smooth_response["result"], dense_response["result"]
                self.assertEqual(smooth["beforePointCount"], 226)
                self.assertEqual((smooth["startSeconds"], smooth["endSeconds"]), (1, 5.5))
                self.assertEqual(smooth["representation"], "automation-simplified")
                # 比较实际返回的两种表示，不绑定实现恰好选择的 21 个控制点。
                self.assertLess(smooth["pointCount"], dense["pointCount"] / 3)
                self.assertEqual(smooth["pointReduction"], dense["pointCount"] - smooth["pointCount"])
                self.assertEqual(len(smooth["curvePreview"]), 97)
                for row in smooth["curvePreview"]:
                    self.assertGreater(row["before"], 0.1)
                    elapsed = 4.5 * row["position"]
                    envelope = max(0, min(1, elapsed / 0.08, (4.5 - elapsed) / 0.08))
                    # 校验返回采样相对非零原基线的实际改变量，而非只检查点数减少。
                    self.assertAlmostEqual(row["after"] - row["before"], 0.05 * envelope, delta=0.002)
        after = self.call("get_selection")["result"]["parameters"]["tension"]["fingerprint"]
        self.assertEqual(before, after)
        self.assertEqual(self.host.mutations, 0)
        self.assertEqual(self.host.undos, 0)

    def test_curve_offsets_follow_ordered_user_shape(self):
        """不同时间点的调整量形成同一个预览，而非把整段错误地平移成末点值。"""
        preview = self.call("preview", parameter="tension", curve=[[0, 0], [0.25, 0.1], [0.75, -0.1], [1, 0]])
        self.assertTrue(preview["ok"], preview.get("error"))
        view = preview["result"]["curvePreview"]
        self.assertAlmostEqual(view[24]["after"], 0.1)
        self.assertAlmostEqual(view[72]["after"], -0.1)
        self.assertEqual(self.host.mutations, 0)

    def test_curve_shape_and_unknown_fields_are_rejected_before_writes(self):
        for arguments in (
            {"curve": [[0, 0], [0.5, 0.1], [0.5, 0], [1, 0]]},
            {"curve": [[0.1, 0.1], [1, 0]]},
            {"curve": [[0, 0.1], [1, 0.4]]},
            {"curve": [[0, True], [1, 0]]},
            {"curve": [[0, 0.1], [1, 0]], "delta": 0.1},
            {"delta": 0.1, "script": "arbitrary"},
            {"delta": 0.1, "renderMode": "unknown"},
        ):
            with self.subTest(arguments=arguments):
                self.assertFalse(self.call("preview", parameter="tension", **arguments)["ok"])
        self.assertEqual(self.host.mutations, 0)

    def test_vocal_modes_are_discovered_and_voice_changes_expire_preview(self):
        """只接受当前宿主目录中的模式，切换声线默认配置后即使点数未变也必须重做预览。"""
        self.lua.execute('host.voice={vocalModeParams={Airy={pitch=20,timbre=30,pronunciation=40}}}')
        selection = self.call("get_selection")["result"]
        self.assertEqual(selection["parameters"]["vocalMode_Airy"]["kind"], "vocalMode")
        self.assertNotIn("vocalMode_Cute", selection["parameters"])
        self.assertFalse(self.call("preview", parameter="vocalMode_Cute", delta=10)["ok"])
        preview = self.call("preview", parameter="vocalMode_Airy", delta=0.1)
        self.assertTrue(preview["ok"], preview.get("error"))
        self.enable()
        self.lua.execute('host.voice.vocalModeParams.Airy.timbre=31')
        self.assertFalse(self.call("apply", previewId=preview["result"]["previewId"])["ok"])
        self.assertEqual(self.host.mutations, 0)

    def test_parameter_fingerprint_detects_same_count_edits_and_is_numeric_stable(self):
        before = self.call("get_selection")["result"]["parameters"]["tension"]["fingerprint"]
        self.host.readFloats = True
        self.assertEqual(before, self.call("get_selection")["result"]["parameters"]["tension"]["fingerprint"])
        self.lua.execute('host.curve.points[1][2]=0.05')
        self.assertNotEqual(before, self.call("get_selection")["result"]["parameters"]["tension"]["fingerprint"])

    def test_unsupported_interpolation_and_excessive_curve_are_rejected(self):
        self.host.method = "unverified-method"
        self.assertFalse(self.call("preview", parameter="tension", delta=0.1)["ok"])
        self.host.method = "Linear"
        self.lua.execute("host.curve.points={}; for i=1,4001 do host.curve.points[i]={i,0} end")
        self.assertIn("4000", self.call("preview", parameter="tension", delta=0.1)["error"])
        self.assertEqual(self.host.mutations, 0)

    def test_float32_candidate_readback_is_the_verified_write_target(self):
        # 0.1 无法用 float32 精确表示，未经候选读回规范化会导致写后校验假失败。
        self.host.quantize = True
        preview = self.preview_id()
        self.enable()
        applied = self.call("apply", previewId=preview)
        self.assertTrue(applied["ok"], applied.get("error"))
        self.assertTrue(self.call("restore")["ok"])

    def test_equal_integer_and_float_positions_never_create_duplicate_adds(self):
        self.lua.execute("host.curve.points={{0.0,0},{1000.0,0},{1020.0,0},{2000.0,0},{3000.0,0}}")
        preview = self.preview_id()
        self.enable()
        self.assertTrue(self.call("apply", previewId=preview)["ok"])
        self.assertEqual(self.host.duplicateAdds or 0, 0)

    def test_point_fingerprint_ignores_integer_float_representation(self):
        preview = self.preview_id()
        self.host.readFloats = True
        self.enable()
        result = self.call("apply", previewId=preview)
        self.assertTrue(result["ok"], result.get("error"))

    def test_failed_postcondition_rolls_back_and_verifies_original_points(self):
        preview = self.preview_id()
        self.enable()
        self.host.corruptRounds = self.lua.table_from({1: True})
        result = self.call("apply", previewId=preview)
        self.assertFalse(result["ok"])
        self.assertIn("已恢复并校验", result["error"])
        self.assertEqual(len(self.host.curve.points), 2)
        self.assertEqual(self.host.curve.points[1][2], 0)
        self.assertEqual(self.host.undos, 1)

    def test_failed_rollback_retains_snapshot_for_later_guarded_restore(self):
        preview = self.preview_id()
        self.enable()
        self.host.corruptRounds = self.lua.table_from({1: True, 2: True})
        result = self.call("apply", previewId=preview)
        self.assertFalse(result["ok"])
        self.assertIn("已保留恢复记录", result["error"])
        # 模拟瞬时宿主故障解除，恢复命令应仍持有最初的原控制点。
        self.host.corruptRounds = self.lua.table()
        result = self.call("restore")
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(len(self.host.curve.points), 2)
        self.assertEqual(self.host.curve.points[1][2], 0)

    def put_request(self, action):
        """向隔离目录写入真实 IPC 信封，用于验证 poll 的文件所有权与停止流程。"""
        request = {"id": "test-request", "session": self.session(), "expires": time.time() + 60,
                   "action": action, "args": {}}
        path = Path(self.temporary.name) / "request.json"
        path.write_text(json.dumps(request), encoding="utf-8")
        return path

    def test_failed_request_claim_never_dispatches_or_deletes_request(self):
        request = self.put_request("get_selection")
        self.lua.execute(r'''
local rename=os.rename
os.rename=function(source,target)
  if source:match("/request%.json$") then return nil,"simulated claim failure" end
  return rename(source,target)
end
''')
        self.poll()
        self.assertTrue(request.exists())
        self.assertFalse((Path(self.temporary.name) / "response.json").exists())
        self.assertEqual(self.host.queued, 1)

    def test_stop_bridge_replies_without_scheduling_another_poll(self):
        self.put_request("stop_bridge")
        self.poll()
        response = json.loads((Path(self.temporary.name) / "response.json").read_text(encoding="utf-8"))
        self.assertTrue(response["ok"])
        self.assertTrue(self.host.finished)
        self.assertEqual(self.host.queued or 0, 0)

    def test_user_mode_registration_only_expands_current_session_catalog(self):
        # 注册遗漏名称不能修改声音、创建撤销记录或自动开启工程写入。
        before = self.call("get_selection")["result"]
        expected = {key: before[key] for key in
                    ("projectFile", "groupUUID", "groupOffset", "groupPitchOffset", "voiceFingerprint")}
        preview = self.preview_id()
        result = self.call("register_vocal_mode", name="Cute", expected=expected)
        self.assertTrue(result["ok"], result.get("error"))
        item = result["result"]["parameters"]["vocalMode_Cute"]
        self.assertEqual(item["source"], "user")
        self.assertEqual(self.host.mutations, 0)
        self.assertEqual(self.host.undos, 0)
        self.enable()
        self.assertFalse(self.call("apply", previewId=preview)["ok"])
        self.assertTrue(self.call("preview", parameter="vocalMode_Cute", delta=0.1)["ok"])
        # 声线设置改变后，之前的手动声明不自动套用到新上下文。
        self.lua.execute("host.voice={paramTension=0.2}")
        self.assertNotIn("vocalMode_Cute", self.call("get_selection")["result"]["parameters"])
        self.assertFalse(self.call("register_vocal_mode", name="Cute", expected=expected)["ok"])

    def test_user_mode_registration_rejects_missing_stale_or_invalid_context(self):
        before = self.call("get_selection")["result"]
        expected = {key: before[key] for key in
                    ("projectFile", "groupUUID", "groupOffset", "groupPitchOffset", "voiceFingerprint")}
        for name in ("", " Cute", "Cute ", "Bad\nName", "x" * 81):
            with self.subTest(name=name):
                self.assertFalse(self.call("register_vocal_mode", name=name, expected=expected)["ok"])
        for context in ({}, {**expected, "groupOffset": 1}, {**expected, "voiceFingerprint": "stale"}):
            self.assertFalse(self.call("register_vocal_mode", name="Cute", expected=context)["ok"])
        self.assertEqual(self.host.mutations, 0)
        self.assertNotIn("vocalMode_Cute", self.call("get_selection")["result"]["parameters"])

    def test_cubic_large_mode_fade_remains_sparse_and_does_not_overshoot(self):
        # 声线百分点的较大斜率能复现淡入转折过冲；不得靠退回均匀密集点掩盖。
        self.lua.execute('''
host.method="Cubic"; host.voice={vocalModeParams={Airy={pitch=0,timbre=0,pronunciation=0}}}
function host.curve:getDefinition() return {range={-150,150},defaultValue=0} end
''')
        result = self.call("preview", parameter="vocalMode_Airy", delta=10)
        self.assertTrue(result["ok"], result.get("error"))
        self.assertLess(result["result"]["pointCount"], 30)
        self.assertEqual(self.host.mutations, 0)
        self.assertTrue(all(-0.15 <= row["after"] <= 10.15 for row in result["result"]["curvePreview"]))


if __name__ == "__main__":
    unittest.main()
