"""参数当前值上下文的回归测试；只读虚拟宿主，不访问工程、密钥或模型。

真实 Lua 源码负责采样，Python 负责公开数据白名单。两侧分别验证，防止只测试
手工构造的理想 JSON，而遗漏速度图、组偏移、读取故障及上下文预算等实际条件。
"""

import copy
import json
import unittest

from synthv_assistant.parameters import public_parameter_catalog
from tests import test_lua_bridge as lua_fixture
from tests.test_parameters import modern_selection


def sampled_values():
    """固定的合成通道值；故意使用非零基线以识别错误的默认零值回退。"""
    return {"samples": [[0, 0.2], [0.5, 0.4], [1, 0.3]], "minimum": 0.2, "maximum": 0.4}


class PublicParameterContextTests(unittest.TestCase):
    def setUp(self):
        self.selection = modern_selection()
        self.definition = self.selection["parameters"]["tension"]
        self.definition["currentValues"] = sampled_values()

    def current(self):
        return public_parameter_catalog(self.selection)["tension"].get("currentValues")

    def test_actual_values_are_copied_and_private_extra_fields_are_filtered(self):
        """目录及摘要两层都只输出已知字段，不能把路径或宿主指纹发给模型。"""
        self.definition["fingerprint"] = "private-identity"
        self.definition["currentValues"].update(path="private-path", fingerprint="private-identity")
        result = self.current()
        self.assertEqual(result, sampled_values())
        self.assertNotIn("private", json.dumps(public_parameter_catalog(self.selection)))
        result["samples"][0][1] = 99
        self.assertEqual(self.definition["currentValues"]["samples"][0][1], 0.2)

    def test_malformed_samples_are_omitted_without_removing_parameter_capability(self):
        """拒绝缺端点、无序、非数值和超预算摘要；不能拿局部有效点拼出假基线。"""
        invalid_samples = (
            [], [[0, 0.2]], [[0.1, 0.2], [1, 0.3]], [[0, 0.2], [0.9, 0.3]],
            [[0, 0.2], [0.5, 0.4], [0.4, 0.3], [1, 0.3]], [[0, 0.2], [0, 0.3], [1, 0.3]],
            [[0, 0.2], [1, True]], [[False, 0.2], [1, 0.3]], [[0, 0.2], [1, "0.3"]],
            [[0, 0.2], [1, float("nan")]], [[0, 0.2], [1, float("inf")]],
            [[0, 0.2], [1, 10 ** 1000]], [[0, 0.2, "private"], [1, 0.3]],
            [[index / 33, 0.2] for index in range(34)],
        )
        for samples in invalid_samples:
            self.definition["currentValues"] = {**sampled_values(), "samples": samples}
            with self.subTest(samples=samples):
                self.assertIsNone(self.current())
                self.assertIn("tension", public_parameter_catalog(self.selection))

    def test_summary_extrema_must_equal_observed_sample_values(self):
        for field, value in (("minimum", 0), ("maximum", 1), ("minimum", True),
                             ("maximum", float("nan")), ("minimum", None)):
            self.definition["currentValues"] = {**sampled_values(), field: value}
            with self.subTest(field=field, value=value):
                self.assertIsNone(self.current())

    def test_native_pitch_and_missing_summaries_are_never_invented(self):
        self.selection["parameters"]["pitchCurve"]["currentValues"] = sampled_values()
        result = public_parameter_catalog(self.selection)
        self.assertNotIn("currentValues", result["pitchCurve"])
        self.assertNotIn("currentValues", result["breathiness"])

    def test_many_voice_modes_share_fixed_total_sample_budget(self):
        """模拟不遵守预算的桥接输入，公开层仍限制总点数而不扩张参数权限。"""
        values = {"samples": [[index / 32, 0.2] for index in range(33)], "minimum": 0.2, "maximum": 0.2}
        for index in range(64):
            name = "Mode" + str(index)
            self.selection["parameters"]["vocalMode_" + name] = {
                "kind": "vocalMode", "modeName": name, "range": [0, 100], "currentValues": copy.deepcopy(values)}
        public = public_parameter_catalog(self.selection)
        total = sum(len(item.get("currentValues", {}).get("samples", [])) for item in public.values())
        self.assertLessEqual(total, 512)
        self.assertGreater(total, 400)
        self.assertEqual(len(public), len(self.selection["parameters"]))


@unittest.skipIf(lua_fixture.LuaRuntime is None, "可选 Lua 测试依赖 lupa 未安装")
class LuaParameterContextTests(unittest.TestCase):
    call = lua_fixture.LuaBridgeTests.call

    def setUp(self):
        lua_fixture.LuaBridgeTests.setUp(self)

    def selection(self):
        result = self.call("get_selection")
        self.assertTrue(result["ok"], result.get("error"))
        return result["result"]

    def test_reads_actual_curve_values_without_mutation_or_clone(self):
        self.lua.execute("host.curve.points={{1000,0.2},{1500,0.6},{2000,0.3}}")
        values = self.selection()["parameters"]["tension"]["currentValues"]
        self.assertEqual(len(values["samples"]), 33)
        self.assertEqual(values["samples"][0], [0, 0.2])
        self.assertEqual(values["samples"][16], [0.5, 0.6])
        self.assertEqual(values["samples"][-1], [1, 0.3])
        self.assertEqual((values["minimum"], values["maximum"]), (0.2, 0.6))
        self.assertEqual((self.host.mutations, self.host.clones, self.host.undos), (0, 0, 0))

    def test_seconds_sampling_respects_tempo_change_inside_selection(self):
        """半程时间对应 1375 blick，不能误用音符组内坐标的算术中点 1500。"""
        self.lua.execute('''
local axis=SV:getProject():getTimeAxis()
function axis:getSecondsFromBlick(b) return b<=1500 and b/1000 or 1.5+(b-1500)/2000 end
function axis:getBlickFromSeconds(s) return s<=1.5 and s*1000 or 1500+(s-1.5)*2000 end
host.curve.points={{1000,0},{2000,1}}
''')
        values = self.selection()["parameters"]["tension"]["currentValues"]
        self.assertEqual(values["samples"][16], [0.5, 0.375])

    def test_group_time_offset_is_removed_before_automation_read(self):
        self.lua.execute('''
local ref=SV:getMainEditor():getCurrentGroup()
function ref:getTimeOffset() return 1000 end
host.curve.points={{1000,0.2},{2000,0.6},{3000,1}}
''')
        values = self.selection()["parameters"]["tension"]["currentValues"]
        self.assertEqual(values["samples"][0], [0, 0.2])
        self.assertEqual(values["samples"][-1], [1, 0.6])

    def test_one_failed_channel_omits_its_complete_summary_only(self):
        self.lua.execute('''
local group=SV:getMainEditor():getCurrentGroup():getTarget()
local original=group.getParameter
function group:getParameter(name)
  local curve=original(self,name)
  if name=="tension" then
    return setmetatable({get=function(_,position)
      if position>1400 then error("private-host-failure") end
      return 0.2
    end},{__index=curve})
  end
  return curve
end
''')
        catalog = self.selection()["parameters"]
        self.assertTrue(catalog["tension"]["available"])
        self.assertNotIn("currentValues", catalog["tension"])
        self.assertIn("currentValues", catalog["breathiness"])
        self.assertNotIn("private-host-failure", json.dumps(catalog))

    def test_invalid_host_sample_is_not_replaced_with_zero(self):
        self.lua.execute("function host.curve:get(_) return 0/0 end")
        self.assertTrue(all("currentValues" not in item for item in self.selection()["parameters"].values()))

    def test_out_of_scope_selections_do_not_read_automation_values(self):
        """无音符、过长或过多音符的选区仍返回原目录，但不花费采样预算。"""
        for mode in ("empty", "long", "many"):
            with self.subTest(mode=mode):
                self.setUp()
                self.lua.execute('''
host.sampleReads=0
function host.curve:get(_) host.sampleReads=host.sampleReads+1; return 0.2 end
local chosen=SV:getMainEditor():getSelection()
local note=chosen:getSelectedNotes()[1]
'''+ {
                    "empty": "function chosen:getSelectedNotes() return {} end",
                    "long": "function note:getDuration() return 31000 end",
                    "many": "function chosen:getSelectedNotes() local notes={} for index=1,129 do notes[index]=note end return notes end",
                }[mode])
                self.assertTrue(all("currentValues" not in item for item in self.selection()["parameters"].values()))
                self.assertEqual(self.host.sampleReads, 0)

    def test_large_voice_catalog_stays_within_shared_budget(self):
        self.lua.execute('''
host.voice={vocalModeParams={}}
for index=1,64 do host.voice.vocalModeParams["Mode"..index]={} end
''')
        catalog = self.selection()["parameters"]
        summaries = [item["currentValues"] for item in catalog.values() if "currentValues" in item]
        self.assertEqual(len(summaries), 71)
        self.assertLessEqual(sum(len(item["samples"]) for item in summaries), 512)
        self.assertTrue(all(2 <= len(item["samples"]) <= 33 for item in summaries))
        self.assertTrue(all(item["samples"][0][0] == 0 and item["samples"][-1][0] == 1 for item in summaries))
