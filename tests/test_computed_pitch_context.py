"""宿主计算音高的只读上下文测试；不调用实际 SynthV、模型或音频文件。

执行生产桥接和 JSON 编码器，专门覆盖 Lua 稀疏表、缺帧及不同于乐谱音高的值。
这些测试不假设声学质量，也不把采样当作已经渲染试听或可以直接写回的控制曲线。
"""

import json
from pathlib import Path
import unittest

from synthv_assistant.parameters import public_computed_pitch
from tests import test_lua_bridge as lua_fixture


class PublicComputedPitchTests(unittest.TestCase):
    def test_ready_samples_preserve_null_frames_and_filter_private_fields(self):
        source = {"status": "ready", "unit": "MIDI", "samples": [[0, None], [0.4, 60.25], [1, None]],
                  "path": "private-project", "fingerprint": "private-state"}
        public = public_computed_pitch(source)
        self.assertEqual(public, {key: source[key] for key in ("status", "unit", "samples")})
        self.assertNotIn("private", json.dumps(public))
        public["samples"][1][1] = 70
        self.assertEqual(source["samples"][1][1], 60.25)

    def test_ready_can_report_no_pitch_data_without_fabricating_values(self):
        """就绪只代表返回了采样网格；整段无基频的帧仍是 None，不能补成乐谱音高。"""
        value = {"status": "ready", "unit": "MIDI", "samples": [[0, None], [1, None]]}
        self.assertEqual(public_computed_pitch(value), value)

    def test_pending_and_unavailable_require_empty_samples(self):
        for status in ("pending", "unavailable"):
            with self.subTest(status=status):
                value = {"status": status, "unit": "MIDI", "samples": []}
                self.assertEqual(public_computed_pitch(value), value)
                self.assertIsNone(public_computed_pitch({**value, "samples": [[0, 60], [1, 60]]}))

    def test_bad_status_unit_shape_values_and_sample_budget_are_filtered(self):
        valid = {"status": "ready", "unit": "MIDI", "samples": [[0, 60], [1, 60]]}
        invalid = [None, [], {}, {**valid, "status": "finished"}, {**valid, "status": []},
                   {**valid, "status": True}, {**valid, "unit": "Hz"}, {**valid, "samples": None}]
        for samples in (
            [], [[0, 60]], [[0, 60], [0.9, 60]], [[0.1, 60], [1, 60]],
            [[0, 60], [0.5, 60], [0.5, 60], [1, 60]],
            [[0, 60], [0.6, 60], [0.4, 60], [1, 60]],
            [[False, 60], [1, 60]], [[0, 60], [1, True]], [[0, 60], [1, "60"]],
            [[0, 60], [1, float("nan")]], [[0, 60], [1, float("inf")]],
            [[0, 60], [1, -1]], [[0, 60], [1, 128]], [[0, 60], [1, 10 ** 1000]],
            [[0, 60, "private"], [1, 60]], [[index / 97, 60] for index in range(98)],
        ):
            invalid.append({**valid, "samples": samples})
        for value in invalid:
            with self.subTest(value=value):
                self.assertIsNone(public_computed_pitch(value))


@unittest.skipIf(lua_fixture.LuaRuntime is None, "可选 Lua 测试依赖 lupa 未安装")
class LuaComputedPitchTests(unittest.TestCase):
    call = lua_fixture.LuaBridgeTests.call

    def setUp(self):
        lua_fixture.LuaBridgeTests.setUp(self)

    def selection(self):
        result = self.call("get_selection")
        self.assertTrue(result["ok"], result.get("error"))
        return result["result"]

    def test_api_receives_reference_absolute_blick_and_exact_frame_count(self):
        self.lua.execute('''
local ref=SV:getMainEditor():getCurrentGroup()
function ref:getTimeOffset() return 4000 end
function ref:getPitchOffset() return 12 end
host.pitchReads=0
function SV:getComputedPitchForGroup(group,start,interval,frames)
  host.pitchReads=host.pitchReads+1
  host.pitchArgs={sameRef=group==ref,start=start,interval=interval,frames=frames}
  local values={} for index=1,frames do values[index]=60.25+index/1000 end
  return values
end
''')
        result = self.selection()["computedPitch"]
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["unit"], "MIDI")
        self.assertEqual(len(result["samples"]), 97)
        self.assertTrue(self.host.pitchArgs.sameRef)
        self.assertEqual(self.host.pitchArgs.start, 5000)
        self.assertEqual(self.host.pitchArgs.frames, 97)
        self.assertAlmostEqual(self.host.pitchArgs.interval, 1000 / 96)
        self.assertEqual(result["samples"][0], [0, 60.251])
        self.assertEqual(result["samples"][-1], [1, 60.347])
        # 宿主返回值不再加组移调；API 只读一次，也不为等结果安排轮询或写操作。
        self.assertEqual(self.host.pitchReads, 1)
        self.assertIsNone(self.host.queued)
        self.assertEqual((self.host.mutations, self.host.undos), (0, 0))

    def test_blick_samples_are_converted_to_seconds_positions_across_tempo_change(self):
        self.lua.execute('''
local axis=SV:getProject():getTimeAxis()
function axis:getSecondsFromBlick(b) return b<=1500 and b/1000 or 1.5+(b-1500)/2000 end
function axis:getBlickFromSeconds(s) return s<=1.5 and s*1000 or 1500+(s-1.5)*2000 end
function SV:getComputedPitchForGroup(_,_,_,frames)
  local values={} for index=1,frames do values[index]=60 end return values
end
''')
        values = self.selection()["computedPitch"]["samples"]
        # 第49帧在blick中点1500，但它对应1.5秒，即整个0.75秒选区的2/3处。
        self.assertAlmostEqual(values[48][0], 2 / 3)
        self.assertEqual(values[0][0], 0)
        self.assertEqual(values[-1][0], 1)

    def test_sparse_host_frames_keep_leading_middle_and_trailing_nulls(self):
        self.lua.execute('''
function SV:getComputedPitchForGroup(_,_,_,_)
  return {[2]=60.2,[48]=60.4,[96]=60.1}
end
''')
        result = self.selection()["computedPitch"]
        self.assertEqual(result["status"], "ready")
        self.assertEqual(len(result["samples"]), 97)
        self.assertIsNone(result["samples"][0][1])
        self.assertEqual(result["samples"][1][1], 60.2)
        self.assertIsNone(result["samples"][2][1])
        self.assertEqual(result["samples"][47][1], 60.4)
        self.assertIsNone(result["samples"][-1][1])
        self.assertEqual(public_computed_pitch(result), result)

    def test_empty_response_is_pending_and_never_retried(self):
        self.lua.execute('''
host.pitchReads=0
function SV:getComputedPitchForGroup(_,_,_,_)
  host.pitchReads=host.pitchReads+1; return {}
end
''')
        self.assertEqual(self.selection()["computedPitch"], {"status": "pending", "unit": "MIDI", "samples": []})
        self.assertEqual(self.host.pitchReads, 1)
        self.assertIsNone(self.host.queued)

    def test_missing_api_and_invalid_responses_are_unavailable_without_error_text(self):
        expected = {"status": "unavailable", "unit": "MIDI", "samples": []}
        self.assertEqual(self.selection()["computedPitch"], expected)
        for body in ("error('private-host-error')", "return nil", "return 60", "return 'private'",
                     "return {[1]=0/0}", "return {[1]=math.huge}", "return {[1]=false}",
                     "return {[1]='60'}", "return {[1]=-1}", "return {[1]=128}",
                     "return {[0]=60}", "return {[98]=60}", "return {[1.5]=60}",
                     "return {path='private'}", "return {[1]={}}"):
            with self.subTest(body=body):
                self.lua.execute("function SV:getComputedPitchForGroup(_,_,_,_) " + body + " end")
                result = self.selection()["computedPitch"]
                self.assertEqual(result, expected)
                self.assertNotIn("private", json.dumps(result))

    def test_out_of_scope_selection_never_calls_computed_api(self):
        self.lua.execute('''
host.pitchReads=0
function SV:getComputedPitchForGroup(_,_,_,_)
  host.pitchReads=host.pitchReads+1; return {60}
end
local selection=SV:getMainEditor():getSelection()
local note=selection:getSelectedNotes()[1]
function note:getDuration() return 31000 end
''')
        self.assertEqual(self.selection()["computedPitch"]["status"], "unavailable")
        self.assertEqual(self.host.pitchReads, 0)

    def test_empty_or_excessive_note_selection_never_calls_computed_api(self):
        self.lua.execute('''
host.pitchReads=0
function SV:getComputedPitchForGroup(_,_,_,_)
  host.pitchReads=host.pitchReads+1; return {60}
end
local selection=SV:getMainEditor():getSelection()
local note=selection:getSelectedNotes()[1]
host.selectedCount=0
function selection:getSelectedNotes()
  local notes={} for index=1,host.selectedCount do notes[index]=note end return notes
end
''')
        for count in (0, 129):
            with self.subTest(count=count):
                self.host.selectedCount = count
                self.assertEqual(self.selection()["computedPitch"]["status"], "unavailable")
                self.assertEqual(self.host.pitchReads, 0)

    def test_computed_readiness_does_not_change_parameter_fingerprints(self):
        """计算进度变化只是新的参考资料，不能让同一工程的已确认候选无故失效。"""
        before = self.selection()
        self.lua.execute('''
function SV:getComputedPitchForGroup(_,_,_,frames)
  local values={} for index=1,frames do values[index]=60.1 end return values
end
''')
        after = self.selection()
        self.assertNotEqual(before.pop("computedPitch"), after.pop("computedPitch"))
        self.assertEqual(before, after)

    def test_json_null_sentinel_encodes_array_holes_without_changing_decode(self):
        """验证实际 vendored 编码器扩展，版权和上游既有 decode(null) 语义不变。"""
        parser = (Path(__file__).resolve().parents[1] / "synthv" / "json.lua").read_text(encoding="utf-8")
        encoded, decode_is_nil, ordinary_empty = self.lua.execute(
            "local json=(function()\n" + parser + "\nend)()\n"
            'return json.encode({{0,json.null},{0.5,60},{1,json.null}}),json.decode("null")==nil,json.encode({})')
        self.assertEqual(json.loads(encoded), [[0, None], [0.5, 60], [1, None]])
        self.assertTrue(decode_is_nil)
        self.assertEqual(ordinary_empty, "[]")
