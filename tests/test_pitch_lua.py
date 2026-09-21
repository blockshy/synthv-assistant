"""在隔离 Lua 5.4 虚拟机中验证原生音高适配器的坐标和恢复边界。

替身只实现官方公开对象接口，所有工程数据均为本文件生成的内存数据。
测试不会启动 SynthV、读取 SVP、连接 IPC 或执行真实工程写入。
"""

import json
from pathlib import Path
import tempfile
import unittest

try:
    # 固定使用 Lua 5.4，保证整数/浮点表示及字符串打包行为与目标脚本环境一致。
    from lupa.lua54 import LuaRuntime
except ImportError:
    LuaRuntime = None


STUB = r'''
-- 控件所有权、量化、单次异常和写后损坏均可独立注入，避免只验证成功路径。
host={controls={},mutations=0,creations=0,clones=0,timeOffset=0,pitchOffset=0,
  begin=1000,finish=2000,quantize=false,deltaMethod="Linear",deltaPoints={{0,0},{4000,0}}}
local function copy(value)
  if type(value)~="table" then return value end
  local result={}; for key,item in pairs(value) do result[key]=copy(item) end
  return result
end
local function numeric(value)
  if host.quantize then return (string.unpack("f",string.pack("f",value))) end
  return value
end
local Point={}; Point.__index=Point
local Curve={}; Curve.__index=Curve
-- 这些元数据是其他脚本的私有内容；被测模块应保留而不是重新构造空对象。
function Point:getScriptDataKeys()
  if host.metadataReadFailure then error("mock metadata failure") end
  local keys={}; for key in pairs(self.metadata) do keys[#keys+1]=key end
  if host.reverseMetadataKeys then table.sort(keys,function(a,b) return a>b end) end
  return keys
end
function Point:getScriptData(key) return copy(self.metadata[key]) end
function Point:getPosition() return host.readFloats and self.position+0.0 or self.position end
function Point:getPitch() return host.readFloats and self.pitch+0.0 or self.pitch end
function Point:setPosition(value) self.position=value end
function Point:setPitch(value) self.pitch=numeric(value) end
function Point:clone()
  host.clones=host.clones+1
  local clone={position=self.position,pitch=self.pitch,metadata=copy(self.metadata),opaque=self.opaque}
  if host.dropCloneMetadata then clone.metadata={} end
  if self.points then clone.points=copy(self.points) end
  return setmetatable(clone,self.points and Curve or Point)
end
-- 将公共父方法复制给曲线，但让点确实不具备 getPoints，模拟两种独立宿主类型。
for name,callback in pairs(Point) do if name~="__index" then Curve[name]=callback end end
function Curve:getPoints()
  local result=copy(self.points)
  if host.readFloats then for _,p in ipairs(result) do p[1]=p[1]+0.0; p[2]=p[2]+0.0 end end
  return result
end
function Curve:setPoints(points)
  self.points=copy(points)
  for _,p in ipairs(self.points) do p[2]=numeric(p[2]) end
end
function Curve:getValueAt(position)
  -- 同时模拟文档相对坐标及真实 2.2.1 的组内绝对坐标，避免替身再次掩盖绑定差异。
  host.valueQueries=host.valueQueries or {}; host.valueQueries[#host.valueQueries+1]=position
  local calibration=self.position==1000000000 and self.pitch==60 and #self.points==2
    and self.points[2][1]==100000000 and self.points[2][2]==2
  if calibration and host.calibrationEvaluator then return host.calibrationEvaluator(position,self.points) end
  if host.nativeSemantics=="group" then position=position-self.position end
  local shift=host.nativeSemantics=="group" and self.pitch or 0
  if not calibration and host.nativeEvaluator then return host.nativeEvaluator(position,self.points)+shift end
  local points=self.points
  if position<=points[1][1] then return points[1][2]+shift end
  if position>=points[#points][1] then return points[#points][2]+shift end
  for index=1,#points-1 do
    local left,right=points[index],points[index+1]
    if position<=right[1] then
      return left[2]+(right[2]-left[2])*(position-left[1])/(right[1]-left[1])+shift
    end
  end
end
function host.addFixture(kind,position,pitch,points,metadata)
  local item=setmetatable({position=position,pitch=pitch,metadata=copy(metadata or {}),
    opaque="内部宿主数据",attached=true},kind=="curve" and Curve or Point)
  if kind=="curve" then item.points=copy(points) end
  host.controls[#host.controls+1]=item
  return item
end

local Delta={}
function Delta:getAllPoints() return copy(host.deltaPoints) end
function Delta:getDefinition() return {defaultValue=host.deltaDefault or 0} end
function Delta:getInterpolationMethod() return host.deltaMethod end
function Delta:get(position)
  if host.deltaEvaluator then return host.deltaEvaluator(position) end
  local points=host.deltaPoints
  if #points==0 then return host.deltaDefault or 0 end
  if position<=points[1][1] then return points[1][2] end
  if position>=points[#points][1] then return points[#points][2] end
  for index=1,#points-1 do
    local left,right=points[index],points[index+1]
    if position<=right[1] then return left[2]+(right[2]-left[2])*(position-left[1])/(right[1]-left[1]) end
  end
end
group={}
function group:getNumPitchControls() return #host.controls end
function group:getPitchControl(index) return host.controls[index] end
function group:getParameter(name) assert(name=="pitchDelta"); return Delta end
function group:removePitchControl(index)
  host.mutations=host.mutations+1
  local removed=table.remove(host.controls,index)
  assert(removed,"invalid index"); removed.removed=true
end
function group:addPitchControl(control)
  -- 附加同一个对象两次应失败，因此每次 write 都必须使用独立 clone。
  assert(not control.attached and not control.removed,"object already owned")
  host.addCalls=(host.addCalls or 0)+1
  if host.failAddAt==host.addCalls then error("mock add failure") end
  host.mutations=host.mutations+1
  control.attached=true
  if host.corruptAddOnce then control.pitch=control.pitch+0.125; host.corruptAddOnce=false end
  host.controls[#host.controls+1]=control
  -- 官方按锚点排序。相同锚点在替身中保留插入顺序；被测代码仍会核验最终顺序。
  local index=#host.controls
  while index>1 and host.controls[index-1].position>control.position do
    host.controls[index]=host.controls[index-1]; index=index-1
  end
  host.controls[index]=control
end
ref={}
function ref:getTimeOffset() return host.timeOffset end
function ref:getPitchOffset() return host.pitchOffset end
axis={}
function axis:getSecondsFromBlick(position)
  if host.variableTempo then
    if position<=6500 then return position/1000 end
    return 6.5+(position-6500)/2000
  end
  return position/1000
end
function axis:getBlickFromSeconds(seconds)
  if host.variableTempo then
    if seconds<=6.5 then return seconds*1000 end
    return 6500+(seconds-6.5)*2000
  end
  return seconds*1000
end
function makeContext()
  return {group=group,ref=ref,axis=axis,begin=host.begin,finish=host.finish,
    startSeconds=axis:getSecondsFromBlick(host.begin+host.timeOffset),
    endSeconds=axis:getSecondsFromBlick(host.finish+host.timeOffset),
    selection={notes={{onset=host.begin,duration=host.finish-host.begin,pitch=60}}}}
end
SV={}
function SV:create(kind)
  assert(kind=="PitchControlCurve","only native continuous curves are allowed")
  host.creations=host.creations+1
  return setmetatable({position=0,pitch=0,points={},metadata={}},Curve)
end
-- 任何偷偷读取“生成音高再叠加”的实现都会使测试失败。
function SV:getComputedPitchForGroup() error("computed pitch must not be used as an additive baseline") end
'''


@unittest.skipIf(LuaRuntime is None, "未安装可选 Lua 测试依赖 lupa")
class NativePitchLuaTests(unittest.TestCase):
    def setUp(self):
        """每个用例使用全新虚拟机，失败注入不会污染其他测试。"""
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        self.lua.execute(STUB)
        source = (Path(__file__).resolve().parents[1] / "synthv" / "pitch.lua").read_text(encoding="utf-8")
        self.native = self.lua.execute(source)
        self.host = self.lua.globals().host
        self.group = self.lua.globals().group
        self.ref = self.lua.globals().ref

    def request(self, curve=None, **kwargs):
        """将 Python 输入转换成真正的 Lua 数组，覆盖生产参数校验而非 Python 适配层。"""
        return self.lua.table_from({"parameter": "pitchCurve", "curve": curve or [[0, 60], [0.5, 60.5], [1, 60]], **kwargs}, recursive=True)

    def preview(self, args=None, context=None):
        return self.native.preview(args if args is not None else self.request(), context or self.lua.globals().makeContext())

    def rejects_preview(self, message, args=None, context=None):
        """所有预览拒绝路径均应保持真实宿主控件和写入计数不变。"""
        before = self.native.snapshot(self.group)
        writes = self.host.mutations
        with self.assertRaisesRegex(Exception, message):
            self.preview(args, context)
        self.assertEqual(writes, self.host.mutations)
        self.assertTrue(self.native.same(self.group, before))

    def test_smooth_preview_constructs_independent_absolute_curve(self):
        proposal = self.preview()
        self.assertEqual(self.host.mutations, 0)
        self.assertEqual(len(self.host.controls), 0)
        self.assertEqual(proposal["after"]["pointCount"], 3)
        control = proposal["after"]["controls"][1]["description"]
        self.assertEqual(control["position"], 1000)
        self.assertEqual(control["pitch"], 60)
        self.assertEqual(control["points"][2][1], 500)
        self.assertEqual(control["points"][2][2], 0.5)
        # 未读取可靠生成音高时，前值为空而不是伪造音符直线或零音高。
        self.assertIsNone(proposal["public"]["curvePreview"][1]["before"])
        self.assertFalse(proposal["public"]["beforeAvailable"])

    def test_time_offset_transposition_and_tempo_use_absolute_seconds(self):
        self.lua.execute("host.timeOffset=5000; host.pitchOffset=12; host.variableTempo=true")
        proposal = self.preview(self.request([[0, 72], [0.5, 73], [1, 72]]))
        row = proposal["after"]["controls"][1]["description"]
        # 绝对时间 6..6.75 秒的一半是 6.375 秒，不是组内 blick 的算术中点。
        self.assertEqual(row["position"], 1000)
        self.assertEqual(row["pitch"], 60)
        self.assertEqual(row["points"][2][1], 375)
        self.assertEqual(proposal["public"]["curvePreview"][49]["position"], 0.5)
        self.assertEqual(proposal["public"]["curvePreview"][49]["after"], 73)

    def test_public_preview_samples_actual_host_interpolation_and_each_interval(self):
        self.lua.execute('''
          host.nativeEvaluator=function(x,points)
            if x<=500 then local t=x/500; return 0.5*t*t end
            local t=(x-500)/500; return 0.5*(1-t*t)
          end
        ''')
        proposal = self.preview()
        self.assertEqual(len(proposal["public"]["curvePreview"]), 97)
        self.assertEqual(proposal["public"]["beforePointCount"], 0)
        # 1/4 选区处真实模拟曲线为 0.125；若直连输入断点会错误显示 0.25。
        self.assertEqual(proposal["public"]["curvePreview"][25]["after"], 60.125)
        queries = list(self.host.valueQueries.values())
        for position in [100, 200, 300, 400, 600, 700, 800, 900]:
            self.assertIn(position, queries)

    def test_native_interpolation_overshoot_is_rejected_without_writing(self):
        self.lua.execute('''
          host.nativeEvaluator=function(x)
            if x==100 then return 3 end
            return x<=500 and x/1000 or (1000-x)/1000
          end
        ''')
        self.rejects_preview("插值采样超出允许音高范围")

    def test_native_points_and_invalid_modes_never_write_or_create(self):
        self.rejects_preview("原生音高点会影响邻近生成音高", self.request(renderMode="points"))
        self.rejects_preview("仅支持 smooth", self.request(renderMode=False))
        self.assertEqual(self.host.creations, 0)

    def test_delta_and_unknown_input_fields_are_rejected(self):
        self.rejects_preview("不接受 delta", self.request(delta=0.2))
        self.rejects_preview("不接受 delta", self.request(unexpected="value"))

    def test_curve_schema_bounds_and_pitch_limits(self):
        # 每个失败案例都验证无宿主变更；覆盖稀疏/乱序/边界以及非有限数值。
        curves = [
            [[0, 60]], [[0.1, 60], [1, 60]], [[0, 60], [0.9, 60]],
            [[0, 60], [0.5, 60], [0.5, 61], [1, 60]],
            [[0, 57.99], [1, 60]], [[0, 60], [1, 62.01]],
            [[0, float("nan")], [1, 60]], [[0, 60], [1, float("inf")]],
            [[index / 64, 60] for index in range(65)],
        ]
        for curve in curves:
            with self.subTest(curve=curve):
                self.rejects_preview("原生音高", self.request(curve))
        sparse = self.lua.eval('{parameter="pitchCurve",curve={[1]={0,60},[3]={1,60}}}')
        self.rejects_preview("连续点", sparse)

    def test_global_midi_limit_is_checked_even_when_transposed_note_is_outside(self):
        self.host.pitchOffset = 67
        self.rejects_preview("0至127", self.request([[0, 127], [1, 127.1]]))

    def test_rounding_duplicate_time_is_rejected_without_writes(self):
        self.rejects_preview("时间精度下重合", self.request([[0, 60], [0.0001, 60.1], [1, 60]]))

    def test_replaces_only_wholly_contained_curve_and_keeps_external_metadata(self):
        self.lua.execute('''
          host.addFixture("point",500,59,nil,{external={owner="other-script",version=2}})
          host.addFixture("curve",1000,60,{{0,0},{1000,0}},{replace="old"})
          host.addFixture("curve",3000,61,{{-500,0},{500,1}},{after="keep"})
        ''')
        original = self.native.snapshot(self.group)
        proposal = self.preview()
        self.assertEqual(proposal["public"]["replacedControlCount"], 1)
        self.assertEqual(len(proposal["after"]["controls"]), 3)
        self.assertTrue(self.native.same(self.group, original))
        self.native.write(self.group, proposal["after"])
        self.assertEqual(self.host.controls[1]["metadata"]["external"]["owner"], "other-script")
        self.assertEqual(self.host.controls[3]["metadata"]["after"], "keep")
        self.assertEqual(self.host.controls[3]["opaque"], "内部宿主数据")
        self.native.write(self.group, proposal["before"])
        self.assertTrue(self.native.same(self.group, original))

    def test_crossing_and_boundary_touching_existing_curves_are_rejected(self):
        for position, points in [(900, [[0, 0], [300, 1]]), (1800, [[0, 0], [300, 1]]), (500, [[0, 0], [500, 0]])]:
            with self.subTest(position=position):
                self.host.controls = self.lua.table()
                self.lua.globals().host.addFixture("curve", position, 60, self.lua.table_from(points, recursive=True))
                self.rejects_preview("跨越选区边界")

    def test_existing_native_guidance_point_inside_is_conservatively_rejected(self):
        self.lua.execute('host.addFixture("point",1500,60,nil,{keep=true})')
        self.rejects_preview("移除它可能影响邻近音高")

    def test_nonzero_delta_and_cubic_internal_overshoot_are_rejected(self):
        self.lua.execute("host.deltaPoints={{0,0},{1500,0.00000001},{4000,0}}")
        self.rejects_preview("音高偏移并非零")
        # 端点均为零仍可能有三次插值弯曲，必须检查分段内部，不能只看控制点。
        self.lua.execute('''
          host.deltaPoints={{0,0},{1000,0},{2000,0},{4000,0}}; host.deltaMethod="Cubic"
          host.deltaEvaluator=function(x) local t=(x-1000)/1000; return t*(1-t)*(t+1) end
        ''')
        self.rejects_preview("音高偏移并非零")

    def test_zero_cosine_delta_is_supported_but_unknown_interpolation_fails_closed(self):
        self.host.deltaMethod = "Cosine"
        self.preview()
        self.host.deltaMethod = "CustomSpline"
        self.rejects_preview("插值方式未知")

    def test_float32_readback_becomes_exact_write_and_restore_target(self):
        self.host.quantize = True
        proposal = self.preview(self.request([[0, 60.123456789], [0.5, 61.234567891], [1, 60.987654321]]))
        normalized = proposal["public"]["curvePreview"][1]["after"]
        self.assertNotEqual(normalized, 60.123456789)
        self.assertAlmostEqual(normalized, 60.123456789, places=5)
        self.native.write(self.group, proposal["after"])
        self.assertTrue(self.native.same(self.group, proposal["after"]))
        self.native.write(self.group, proposal["before"])
        self.assertTrue(self.native.same(self.group, proposal["before"]))

    def test_snapshots_detect_same_count_coordinate_metadata_and_order_changes(self):
        self.lua.execute('''
          host.addFixture("point",500,60,nil,{label="a",detail={version=1}})
          host.addFixture("point",500,61,nil,{label="b"})
        ''')
        before = self.native.snapshot(self.group)
        for mutation in [
            'host.controls[1].pitch=60.25',
            'host.controls[1].metadata.detail.version=2',
            'host.controls[1],host.controls[2]=host.controls[2],host.controls[1]',
        ]:
            with self.subTest(mutation=mutation):
                self.lua.execute(mutation)
                self.assertFalse(self.native.same(self.group, before))
                self.native.write(self.group, before)
                self.assertTrue(self.native.same(self.group, before))

    def test_integer_float_representation_and_metadata_key_order_do_not_conflict(self):
        self.lua.execute('host.addFixture("curve",500,60,{{0,0},{100,1}},{a=1,z=2})')
        snapshot = self.native.snapshot(self.group)
        self.host.readFloats = True
        self.host.reverseMetadataKeys = True
        self.assertTrue(self.native.same(self.group, snapshot))

    def test_delta_dependency_change_blocks_write_before_any_mutation(self):
        proposal = self.preview()
        self.lua.execute("host.deltaPoints[1][2]=1")
        self.assertFalse(self.native.same(self.group, proposal["before"]))
        with self.assertRaisesRegex(Exception, "音高偏移曲线已变化"):
            self.native.write(self.group, proposal["after"])
        self.assertEqual(self.host.mutations, 0)

    def test_partial_write_failure_keeps_snapshots_reusable_for_root_rollback(self):
        self.lua.execute('''
          host.addFixture("point",500,60,nil,{owner="fixture"})
          host.addFixture("curve",1000,60,{{0,0},{1000,0}},{original=true})
        ''')
        proposal = self.preview()
        self.host.failAddAt = 2
        with self.assertRaisesRegex(Exception, "接口调用失败"):
            self.native.write(self.group, proposal["after"])
        self.assertFalse(self.native.same(self.group, proposal["before"]))
        self.native.write(self.group, proposal["before"])
        self.assertTrue(self.native.same(self.group, proposal["before"]))
        # 相同快照可重复恢复；原 clone 没有被工程“接管”或失效。
        self.native.write(self.group, proposal["before"])
        self.assertTrue(self.native.same(self.group, proposal["before"]))

    def test_postwrite_corruption_is_detected_and_original_is_restorable(self):
        proposal = self.preview()
        self.host.corruptAddOnce = True
        with self.assertRaisesRegex(Exception, "写入后完整校验失败"):
            self.native.write(self.group, proposal["after"])
        self.native.write(self.group, proposal["before"])
        self.assertTrue(self.native.same(self.group, proposal["before"]))

    def test_tampered_snapshot_and_incomplete_clone_do_not_clear_host(self):
        self.lua.execute('host.addFixture("point",500,60,nil,{owner="other"})')
        proposal = self.preview()
        proposal["after"]["controls"][1]["clone"]["pitch"] = 99
        with self.assertRaisesRegex(Exception, "快照已变化"):
            self.native.write(self.group, proposal["after"])
        self.assertEqual(self.host.mutations, 0)
        self.host.dropCloneMetadata = True
        with self.assertRaisesRegex(Exception, "克隆未保留完整"):
            self.native.snapshot(self.group)

    def test_describe_includes_group_offsets_and_fails_closed_on_metadata_error(self):
        original = self.native.describe(self.group, self.ref)
        self.assertTrue(original["available"])
        self.host.pitchOffset = 1
        self.assertNotEqual(original["fingerprint"], self.native.describe(self.group, self.ref)["fingerprint"])
        self.lua.execute('host.addFixture("point",500,60,nil,{data=true}); host.metadataReadFailure=true')
        self.assertFalse(self.native.describe(self.group, self.ref)["available"])

    def test_missing_math_type_does_not_disable_native_pitch(self):
        # 标准库函数被嵌入式宿主裁剪时，数字指纹仍应可用，不能误报为接口缺失。
        self.lua.execute("math.type=nil")
        self.assertTrue(self.native.describe(self.group, self.ref)["available"])
        proposal = self.preview()
        self.native.write(self.group, proposal["after"])
        self.assertTrue(self.native.same(self.group, proposal["after"]))

    def test_detached_calibration_is_cached_and_never_exposes_probe(self):
        self.lua.execute('host.addFixture("point",500,61,nil,{private="DO-NOT-EXPORT"})')
        original = self.native.snapshot(self.group)
        result = self.native.describe(self.group, self.ref)
        self.assertTrue(result["available"])
        self.assertIsNone(result["probe"])
        self.assertEqual(list(self.host.valueQueries.values()), [0, 50000000, 100000000, 1000000000, 1050000000, 1100000000])
        self.assertEqual(self.host.creations, 1)
        self.native.describe(self.group, self.ref)
        self.assertEqual(self.host.creations, 1)
        self.assertEqual(self.host.mutations, 0)
        self.assertTrue(self.native.same(self.group, original))

    def test_detached_calibration_failure_is_safe_and_disables_native_pitch(self):
        self.lua.execute('function SV:create(_) error("PRIVATE-PROBE-FAILURE") end')
        result = self.native.describe(self.group, self.ref)
        self.assertFalse(result["available"])
        self.assertEqual(result["reasonCode"], "interpolation")
        self.assertIn("接口调用失败（create）", result["message"])
        self.assertNotIn("PRIVATE-PROBE-FAILURE", result["message"])

    def test_detached_calibration_nonfinite_values_are_rejected_without_export(self):
        self.lua.execute('''
          host.calibrationEvaluator=function(position)
            if position==0 then return 0/0 end
            if position==50000000 then return math.huge end
            if position==100000000 then return -math.huge end
            return {private="NEVER-STRINGIFY"}
          end
        ''')
        result = self.native.describe(self.group, self.ref)
        self.assertFalse(result["available"])
        self.assertEqual(result["reasonCode"], "interpolation")
        self.assertIn("无法唯一校准", result["message"])
        self.assertIsNone(result["probe"])
        self.assertNotIn("NEVER-STRINGIFY", result["message"])

    def test_real_host_group_semantics_with_time_pitch_offsets_and_variable_tempo(self):
        # 组内开始 1000、工程偏移 5000、移调 +12，实际输入应为工程绝对 MIDI 72..73。
        self.lua.execute('host.nativeSemantics="group"; host.timeOffset=5000; host.pitchOffset=12; host.variableTempo=true')
        proposal = self.preview(self.request([[0, 72], [0.5, 73], [1, 72]]))
        row = proposal["after"]["controls"][1]["description"]
        self.assertEqual(row["position"], 1000)
        self.assertEqual(row["pitch"], 60)
        self.assertEqual(row["points"][2][1], 375)
        self.assertEqual(proposal["public"]["curvePreview"][49]["after"], 73)
        self.assertEqual(proposal["public"]["curvePreview"][1]["after"], 72)
        self.assertEqual(proposal["public"]["curvePreview"][97]["after"], 72)
        self.assertEqual(self.host.mutations, 0)
        self.native.write(self.group, proposal["after"])
        self.assertTrue(self.native.same(self.group, proposal["after"]))

    def test_real_host_constant_curve_never_double_adds_anchor_pitch(self):
        self.host.nativeSemantics = "group"
        proposal = self.preview(self.request([[0, 60.25], [1, 60.25]]))
        self.assertTrue(all(point["after"] == 60.25 for point in proposal["public"]["curvePreview"].values()))

    def test_ambiguous_calibration_is_rejected_without_host_mutation(self):
        self.lua.execute('''
          host.calibrationEvaluator=function(position)
            if position<=100000000 then return position/50000000 end
            return 60+(position-1000000000)/50000000
          end
        ''')
        self.rejects_preview("无法唯一校准")

    def test_candidate_node_validation_catches_semantic_change_after_calibration(self):
        self.assertTrue(self.native.describe(self.group, self.ref)["available"])
        # 模拟同一绑定在候选对象上返回稍有不同的值；仍在音高范围内也必须拒绝。
        self.lua.execute('host.nativeEvaluator=function(position,points) return 0.01 end')
        self.rejects_preview("候选节点与插值读回不一致")

    def test_describe_missing_method_reports_safe_binding_type(self):
        self.lua.execute('group.getNumPitchControls={private="SECRET-HOST-PATH"}')
        result = self.native.describe(self.group, self.ref)
        self.assertFalse(result["available"])
        self.assertEqual(result["reasonCode"], "native-controls")
        self.assertIn("getNumPitchControls", result["message"])
        self.assertIn("table", result["message"])
        self.assertNotIn("SECRET-HOST-PATH", result["message"])

    def test_callable_userdata_binding_supports_describe_preview_write_and_restore(self):
        # lupa 将 Python callable 暴露成 Lua userdata，可复现真实 SynthV 的方法类型。
        # 保留明确 self 参数，确保 wrapper 的语义与宿主 object:method(...) 一致。
        def bind(callback):
            def forwarded(target, *arguments):
                return callback(target, *arguments)
            self.assertEqual(self.lua.eval("type")(forwarded), "userdata")
            return forwarded

        for obj, names in [
            (self.group, ["getNumPitchControls", "getPitchControl", "getParameter", "addPitchControl", "removePitchControl"]),
            (self.ref, ["getTimeOffset", "getPitchOffset"]),
            (self.lua.globals().axis, ["getSecondsFromBlick", "getBlickFromSeconds"]),
        ]:
            for name in names:
                obj[name] = bind(obj[name])
        # 控件克隆继续通过 metatable 查找接口；将该表内的方法统一换成 userdata。
        self.lua.execute('host.addFixture("curve",1000,60,{{0,0},{1000,0}},{owner="keep"})')
        methods = self.lua.eval("getmetatable(host.controls[1])")
        for name in ["getPosition", "getPitch", "getPoints", "getValueAt", "setPosition", "setPitch", "setPoints", "clone", "getScriptDataKeys", "getScriptData"]:
            methods[name] = bind(methods[name])
        self.assertTrue(self.native.describe(self.group, self.ref)["available"])
        proposal = self.preview()
        self.native.write(self.group, proposal["after"])
        self.assertTrue(self.native.same(self.group, proposal["after"]))
        self.native.write(self.group, proposal["before"])
        self.assertTrue(self.native.same(self.group, proposal["before"]))

    def test_noncallable_userdata_fails_with_fixed_safe_api_error(self):
        # 接受 userdata 类型不等于信任其可调用性；实际调用失败仍保持 fail-closed。
        self.group["getNumPitchControls"] = object()
        self.assertEqual(self.lua.eval("type")(self.group["getNumPitchControls"]), "userdata")
        result = self.native.describe(self.group, self.ref)
        self.assertFalse(result["available"])
        self.assertEqual(result["reasonCode"], "native-controls")
        self.assertIn("接口调用失败（getNumPitchControls）", result["message"])
        self.assertNotIn("object at", result["message"])

    def test_unknown_getpoints_userdata_is_not_guessed_to_be_a_point(self):
        self.lua.execute('host.addFixture("point",500,60,nil,{owner="keep"})')
        def unknown_member(_):
            raise RuntimeError("SECRET-UNKNOWN-MEMBER")
        self.host.controls[1]["getPoints"] = unknown_member
        result = self.native.describe(self.group, self.ref)
        self.assertFalse(result["available"])
        self.assertIn("接口调用失败（getPoints）", result["message"])
        self.assertNotIn("SECRET-UNKNOWN-MEMBER", result["message"])

    def test_describe_host_exception_is_redacted_but_preserves_stage_and_api(self):
        self.lua.execute('''
          host.addFixture("point",500,60,nil,{owner="private"})
          function ref:getPitchOffset() error("SECRET-HOST-PATH and PRIVATE-METADATA") end
        ''')
        result = self.native.describe(self.group, self.ref)
        self.assertFalse(result["available"])
        self.assertEqual(result["reasonCode"], "group-offsets")
        self.assertEqual(result["pointCount"], 1)
        self.assertIn("getPitchOffset", result["message"])
        self.assertNotIn("SECRET-HOST-PATH", result["message"])
        self.assertNotIn("PRIVATE-METADATA", result["message"])

    def test_describe_delta_failure_is_distinct_from_native_control_failure(self):
        self.lua.execute('''
          local delta=group:getParameter("pitchDelta")
          function delta:getDefinition() error("PRIVATE-CONFIG") end
        ''')
        result = self.native.describe(self.group, self.ref)
        self.assertEqual(result["reasonCode"], "pitch-delta")
        self.assertIn("getDefinition", result["message"])
        self.assertNotIn("PRIVATE-CONFIG", result["message"])

    def test_describe_unexpected_lua_error_uses_fixed_text(self):
        # 即使异常不是通过 invoke 产生，也不能把堆栈、路径或宿主返回内容直接发给 UI。
        self.lua.execute('string.format=function() error("SECRET-INTERNAL-ERROR") end; host.deltaPoints={{0,0.1}}')
        result = self.native.describe(self.group, self.ref)
        self.assertFalse(result["available"])
        self.assertEqual(result["reasonCode"], "pitch-delta")
        self.assertIn("无法安全处理的数据", result["message"])
        self.assertNotIn("SECRET-INTERNAL-ERROR", result["message"])

    def test_metadata_and_point_count_have_explicit_bounds(self):
        self.lua.execute('host.addFixture("point",500,60,nil,{huge=string.rep("x",33000)})')
        with self.assertRaisesRegex(Exception, "过大"):
            self.native.snapshot(self.group)
        self.lua.execute('''
          host.controls={}; local points={}
          for index=1,4001 do points[index]={index,0} end
          host.addFixture("curve",0,60,points,{})
        ''')
        with self.assertRaisesRegex(Exception, "4000"):
            self.native.snapshot(self.group)

    def test_aggregate_capacity_stops_before_cloning_every_control(self):
        # 单条曲线未超限，但总和超过 4000 时应立即停止，不克隆其余全部对象。
        self.lua.execute('''
          local points={}; for index=1,2001 do points[index]={index,0} end
          for index=1,20 do host.addFixture("curve",index*5000,60,points,{}) end
        ''')
        with self.assertRaisesRegex(Exception, "总量超过4000"):
            self.native.snapshot(self.group)
        self.assertEqual(self.host.clones, 2)


@unittest.skipIf(LuaRuntime is None, "未安装可选 Lua 测试依赖 lupa")
class NativePitchBridgeTests(unittest.TestCase):
    """执行真实桥接分发器，验证模块之外的预览状态、撤销和失败恢复生命周期。"""

    def setUp(self):
        # write_mode 会写心跳，因此为每个用例创建临时 IPC 目录，绝不使用运行配置。
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        self.lua.execute(STUB)
        self.lua.globals().IPC_DIR = temporary.name.replace("\\", "/")
        self.lua.execute(r'''
          -- 只补充桥接需要的工程/选区接口；原生控件仍使用上面的带所有权检查替身。
          host.project="isolated-test.svp"; host.undos=0
          function group:getUUID() return "isolated-group" end
          function group:getName() return "隔离测试组" end
          function ref:getTarget() return group end
          function ref:getVoice() return {} end
          function ref:isMain() return true end
          function ref:isInstrumental() return false end
          function axis:getAllTempoMarks() return {{position=0,bpm=120}} end
          local note={}
          function note:getOnset() return host.begin end
          function note:getDuration() return host.finish-host.begin end
          function note:getPitch() return 60 end
          function note:getLyrics() return "测试" end
          function note:getIndexInParent() return 1 end
          local selected={getSelectedNotes=function() return {note} end}
          local track={getNumGroups=function() return 1 end,getGroupReference=function() return ref end}
          local project={getFileName=function() return host.project end,getTimeAxis=function() return axis end,
            getNumTracks=function() return 1 end,getTrack=function() return track end,
            newUndoRecord=function() host.undos=host.undos+1 end}
          local editor={getCurrentGroup=function() return ref end,getCurrentTrack=function() return track end,
            getSelection=function() return selected end}
          function SV:getProject() return project end
          function SV:getMainEditor() return editor end
          function SV:getHostInfo() return {hostVersion="isolated-mock-2.2.1"} end
        ''')
        root = Path(__file__).resolve().parents[1] / "synthv"
        # 与安装器一致地在同一词法作用域加载三个生产模块；仅测试尾部暴露私有分发器。
        source = "local json=(function()\n" + (root / "json.lua").read_text(encoding="utf-8") + "\nend)()\n"
        source += "local NativePitch=(function()\n" + (root / "pitch.lua").read_text(encoding="utf-8") + "\nend)()\n"
        source += (root / "bridge.lua").read_text(encoding="utf-8")
        source += r'''
          return function(action,args)
            local ok,result=pcall(dispatch,action,args)
            return json.encode({ok=ok,result=ok and result or nil,error=not ok and tostring(result) or nil})
          end,NativePitch
        '''
        self.dispatch, self.native = self.lua.execute(source)
        self.host = self.lua.globals().host
        self.group = self.lua.globals().group

    def call(self, action, **args):
        """经过实际 JSON 序列化边界，确保私有宿主 clone 没有泄漏到公开响应。"""
        return json.loads(self.dispatch(action, self.lua.table_from(args, recursive=True)))

    def prepare(self):
        """生成可编辑预览并显式开启隔离宿主写入，生产只读保护没有被绕过。"""
        result = self.call("preview", parameter="pitchCurve", curve=[[0, 60], [0.5, 60.5], [1, 60]])
        self.assertTrue(result["ok"], result.get("error"))
        enabled = self.call("write_mode", enabled=True, expectedProject="isolated-test.svp")
        self.assertTrue(enabled["ok"], enabled.get("error"))
        return result["result"]

    def test_dispatch_preview_apply_restore_preserve_original_and_consume_preview(self):
        self.lua.execute('host.addFixture("curve",1000,60,{{0,0},{1000,0}},{owner="fixture"})')
        original = self.native.snapshot(self.group)
        selection = self.call("get_selection")
        self.assertTrue(selection["result"]["parameters"]["pitchCurve"]["available"])
        preview = self.prepare()
        self.assertEqual(preview["representation"], "native-pitch-curve")
        self.assertEqual(len(preview["curvePreview"]), 97)
        self.assertEqual(self.host.undos, 0)
        self.assertEqual(self.host.mutations, 0)
        self.assertTrue(self.native.same(self.group, original))
        applied = self.call("apply", previewId=preview["previewId"])
        self.assertTrue(applied["ok"], applied.get("error"))
        self.assertTrue(applied["result"]["verified"])
        self.assertEqual(applied["result"]["pointCount"], 3)
        self.assertEqual(self.host.undos, 1)
        # 成功的 previewId 不能重放；拒绝路径也不能新增撤销或写入。
        mutations = self.host.mutations
        self.assertFalse(self.call("apply", previewId=preview["previewId"])["ok"])
        self.assertEqual(self.host.mutations, mutations)
        self.assertEqual(self.host.undos, 1)
        restored = self.call("restore")
        self.assertTrue(restored["ok"], restored.get("error"))
        self.assertTrue(self.native.same(self.group, original))
        self.assertEqual(self.host.undos, 2)
        self.assertEqual(self.host.controls[1]["metadata"]["owner"], "fixture")

    def test_dispatch_corrupted_write_rolls_back_and_invalidates_preview(self):
        original = self.native.snapshot(self.group)
        preview = self.prepare()
        self.host.corruptAddOnce = True
        failed = self.call("apply", previewId=preview["previewId"])
        self.assertFalse(failed["ok"])
        self.assertIn("已恢复并校验", failed["error"])
        self.assertTrue(self.native.same(self.group, original))
        self.assertEqual(self.host.undos, 1)
        mutations = self.host.mutations
        self.assertFalse(self.call("apply", previewId=preview["previewId"])["ok"])
        self.assertEqual(self.host.mutations, mutations)
        # 自动回滚成功后不能留下指向失败候选的恢复记录。
        self.assertIn("没有可恢复", self.call("restore")["error"])

    def test_dispatch_partial_add_exception_restores_all_original_controls(self):
        self.lua.execute('''
          host.addFixture("point",500,59,nil,{outside=true})
          host.addFixture("curve",1000,60,{{0,0},{1000,0}},{inside=true})
        ''')
        original = self.native.snapshot(self.group)
        preview = self.prepare()
        self.host.failAddAt = 2
        failed = self.call("apply", previewId=preview["previewId"])
        self.assertFalse(failed["ok"])
        self.assertIn("已恢复并校验", failed["error"])
        self.assertTrue(self.native.same(self.group, original))
        self.assertEqual(self.host.undos, 1)

    def test_dispatch_failed_rollback_retains_snapshot_for_guarded_restore(self):
        self.lua.execute('host.addFixture("curve",1000,60,{{0,0},{1000,0}},{original=true})')
        original = self.native.snapshot(self.group)
        preview = self.prepare()
        # 第一次添加后数据损坏使应用校验失败；第二次添加异常使自动回滚也失败。
        self.host.corruptAddOnce = True
        self.host.failAddAt = 2
        failed = self.call("apply", previewId=preview["previewId"])
        self.assertFalse(failed["ok"])
        self.assertIn("恢复失败，已保留恢复记录", failed["error"])
        self.assertFalse(self.native.same(self.group, original))
        self.assertEqual(self.host.undos, 1)
        # 异常只注入一次，后续显式恢复应使用保留的独立 clone 完整恢复原状态。
        restored = self.call("restore")
        self.assertTrue(restored["ok"], restored.get("error"))
        self.assertTrue(self.native.same(self.group, original))
        self.assertEqual(self.host.undos, 2)
        self.assertIn("没有可恢复", self.call("restore")["error"])


if __name__ == "__main__":
    unittest.main()
