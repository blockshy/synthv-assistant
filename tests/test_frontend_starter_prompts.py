"""验证快捷草稿的显式动作与附件授权边界，所有界面状态均来自隔离夹具。"""

import subprocess
import unittest

from test_frontend_chat import HARNESS, NODE, ROOT


@unittest.skipUnless(NODE, "快捷草稿前端回归需要开发环境提供 Node.js")
class FrontendStarterPromptTests(unittest.TestCase):
    """文案可独立修改，素材页入口不能依赖特定中文词组。"""

    def test_audio_action_is_explicit_and_never_sends_or_attaches(self):
        # 将两个文案故意与旧关键字规则反转，执行正式 chat.js 注册的点击监听。
        # 无动作标记的卡片含“两段音频”，有动作标记的卡片则完全不含该词组。
        harness = HARNESS.replace("vm.runInContext(source,context);", r"""
const plain = new Element('button'); plain.dataset.prompt = '先解释两段音频的比较方法';
const audio = new Element('button'); audio.dataset.prompt = '听一听参考素材'; audio.dataset.promptAction = 'audio';
body.append(plain, audio);
const destinations = [];
window.SynthVPages.go = name => { destinations.push(name); return true; };
vm.runInContext(source,context);
""")
        script = r"""
chat.setConversation(base());
get('include-selection').checked = false;
plain.listeners.click();
assert.equal(get('chat-input').value, plain.dataset.prompt);
assert.deepEqual(destinations, []);
audio.listeners.click();
assert.equal(get('chat-input').value, audio.dataset.prompt);
assert.deepEqual(destinations, ['library']);
assert.equal(get('include-selection').checked, false);
assert.equal(chat.state.attachments.length, 0);
assert.equal(chat.state.conversation.messages.length, 0);
assert.equal(calls.length, 0);
"""
        result = subprocess.run(
            [NODE, "-e", harness + "\n" + script], cwd=ROOT,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
