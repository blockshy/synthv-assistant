"""逐音符编译器到真实 Lua 桥接的离线集成测试，仅使用合成音符和参数。

复用已有 Lua 5.4 宿主替身，执行生产 preview/apply/restore 分发器。这里的
“应用”只改变独立虚拟机内的曲线，绝不访问正在打开的 SynthV 或用户工程。
"""

from __future__ import annotations

import unittest

from synthv_assistant.note_curves import compile_note_curves
from tests import test_lua_bridge as lua_bridge


# 与桥接 tension 的既有允许误差一致；本测试不通过放宽容差隐藏局部串扰。
TENSION_TOLERANCE = 0.002
TEST_BLICK_SCALE = 700_000_000


@unittest.skipIf(lua_bridge.LuaRuntime is None, "可选 Lua 测试依赖 lupa 未安装")
class NoteCurveBridgeTests(unittest.TestCase):
    """以组合复用宿主夹具，避免继承或导入旧 TestCase 导致测试重复发现。"""

    def setUp(self):
        self.harness = lua_bridge.LuaBridgeTests(methodName="runTest")
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)
        self.harness.lua.execute(r'''
-- 采用接近真实 SynthV 的 blick 量级，并将参数存储量化为 float32。
-- 音符故意以乱序和不连续工程索引返回，确保编译器采用时间排序后的序号。
local scale=700000000
host.quantize=true
local axis=SV:getProject():getTimeAxis()
function axis:getSecondsFromBlick(b) return b/scale end
function axis:getBlickFromSeconds(s) return s*scale end
local function note(onset,duration,pitch,index)
  return {getOnset=function() return onset*scale end,
    getDuration=function() return duration*scale end,
    getPitch=function() return pitch end,
    getIndexInParent=function() return index end,
    getLyrics=function() return "合成测试" end}
end
local selected=SV:getMainEditor():getSelection()
function selected:getSelectedNotes()
  return {note(4,1.5,67,21),note(1,1,60,7),note(2.5,1,64,13)}
end
host.curve.points={}
for index=0,180 do
  local seconds=1+index*.025
  -- 两端平滑回到同一个非零基线，中间保留多周期起伏；测试的是在原演唱
  -- 之上局部相加，不能仅凭全零基线证明未目标音符和休止保持不变。
  local phase=index/180
  local value=.15+math.sin(math.pi*phase)^2*(.04*math.sin(phase*7*math.pi)+.02*math.cos(phase*3*math.pi))
  local position=math.floor(seconds*scale+.5)
  value=string.unpack("f",string.pack("f",value))
  host.curve.points[#host.curve.points+1]={position,value}
end
-- 原始区外线性段与端点相连，为现有边界保护逻辑保留真实检查对象。
table.insert(host.curve.points,1,{0,host.curve.points[1][2]})
host.curve.points[#host.curve.points+1]={7*scale,host.curve.points[#host.curve.points][2]}
''')

    def _points(self):
        """完整读取虚拟宿主的控制点，用于确认预览无写入及撤销精确恢复。"""
        return [[point[1], point[2]] for point in self.harness.host.curve.points.values()]

    def _get(self, seconds):
        curve = self.harness.host.curve
        return curve.get(curve, seconds * TEST_BLICK_SCALE)

    def _compile_and_preview(self, render_mode, amount):
        """从桥接实际选区回包编译，再送入同一 VM 的生产 preview 函数。"""
        response = self.harness.call("get_selection")
        self.assertTrue(response["ok"], response.get("error"))
        selection = response["result"]
        self.assertEqual(selection["noteCount"], 3)
        self.assertEqual((selection["startSeconds"], selection["endSeconds"]), (1, 5.5))
        curve = compile_note_curves("tension", [{
            "noteIndex": 3,
            "curve": [[0, 0], [0.25, amount], [0.75, amount], [1, 0]],
        }], selection)
        response = self.harness.call("preview", parameter="tension", curve=curve, renderMode=render_mode)
        self.assertTrue(response["ok"], response.get("error"))
        return response["result"]

    def test_preview_preserves_untargeted_notes_and_rests_without_any_host_write(self):
        """线性/三次及两种绘制模式均保留旧表达，末音只叠加要求的正向增量。"""
        original = self._points()
        for method in ("Linear", "Cubic"):
            self.harness.host.method = method
            for render_mode in ("smooth", "points"):
                with self.subTest(interpolation=method, render_mode=render_mode):
                    preview = self._compile_and_preview(render_mode, 0.08)
                    self.assertEqual(self._points(), original)
                    self.assertEqual(self.harness.host.mutations, 0)
                    self.assertEqual(self.harness.host.undos, 0)
                    self.assertEqual(preview["renderMode"], render_mode)
                    untouched, targeted = [], []
                    for sample in preview["curvePreview"]:
                        seconds = 1 + sample["position"] * 4.5
                        difference = sample["after"] - sample["before"]
                        self.assertGreater(sample["before"], 0.08)
                        if seconds <= 4:
                            # 包括前两个音符和 2..2.5、3.5..4 两段休止。
                            untouched.append(sample["before"])
                            self.assertAlmostEqual(difference, 0, delta=TENSION_TOLERANCE)
                        elif 4.375 <= seconds <= 5.125:
                            targeted.append(difference)
                            self.assertAlmostEqual(difference, 0.08, delta=TENSION_TOLERANCE)
                    self.assertGreater(len(untouched), 40)
                    self.assertGreater(max(untouched) - min(untouched), 0.03)
                    self.assertGreater(len(targeted), 10)

    def test_virtual_apply_and_restore_preserve_baseline_and_negative_local_change(self):
        """只在隔离 VM 中应用负向末音包络，并确认撤销恢复完整原始点列。"""
        original = self._points()
        self.harness.enable()
        expected_undo_records = 0
        for method in ("Linear", "Cubic"):
            self.harness.host.method = method
            for render_mode in ("smooth", "points"):
                with self.subTest(interpolation=method, render_mode=render_mode):
                    # 高于网页显示的采样密度，检查未目标音符及休止中的实际候选值；
                    # 这些值来自生产 apply 后的宿主替身，不把显示折线当成实际插值。
                    positions = [1 + index * 0.005 for index in range(601)]
                    baseline = [self._get(seconds) for seconds in positions]
                    target_before = self._get(4.75)
                    mutations_before = self.harness.host.mutations
                    preview = self._compile_and_preview(render_mode, -0.06)
                    self.assertEqual(self.harness.host.mutations, mutations_before)
                    self.assertEqual(self._points(), original)
                    applied = self.harness.call("apply", previewId=preview["previewId"])
                    self.assertTrue(applied["ok"], applied.get("error"))
                    expected_undo_records += 1
                    self.assertEqual(self.harness.host.undos, expected_undo_records)
                    for seconds, before in zip(positions, baseline):
                        self.assertAlmostEqual(self._get(seconds), before, delta=TENSION_TOLERANCE)
                    self.assertAlmostEqual(self._get(4.75) - target_before, -0.06, delta=TENSION_TOLERANCE)
                    restored = self.harness.call("restore")
                    self.assertTrue(restored["ok"], restored.get("error"))
                    expected_undo_records += 1
                    self.assertEqual(self.harness.host.undos, expected_undo_records)
                    self.assertEqual(self._points(), original)
                    for seconds, before in zip(positions, baseline):
                        self.assertEqual(self._get(seconds), before)


if __name__ == "__main__":
    unittest.main()
