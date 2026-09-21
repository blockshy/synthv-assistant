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
        bridge = (root / "bridge.lua").read_text(encoding="utf-8")
        # 同一代码块末尾暴露局部分发器，仅用于测试；生产文件本身不增加后门。
        self.dispatch, self.poll, self.session = self.lua.execute(
            "local json=(function()\n" + parser + "\nend)()\n" + bridge + r'''
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
        result = self.call("preview", parameter="tension", delta=0.1)
        self.assertFalse(result["ok"])
        self.assertIn("选区以外", result["error"])
        self.assertEqual(self.host.mutations, 0)
        self.assertEqual(self.host.undos, 0)

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


if __name__ == "__main__":
    unittest.main()
