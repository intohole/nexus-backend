"""前缀纪律原语测试：动态上下文后置 user 尾部、消息装配不改写历史（harness r7）。"""
from nexus import disciplined_messages, with_tail_context


class TestWithContextTail:
    def test_blocks_appended_after_content(self):
        out = with_tail_context("今天天气如何", "以下是用户记忆：喜欢跑步", "笔记内容：……")
        assert out == "今天天气如何\n\n以下是用户记忆：喜欢跑步\n\n笔记内容：……"

    def test_empty_blocks_return_content_unchanged(self):
        assert with_tail_context("问题", "", "  ") == "问题"

    def test_no_blocks_passthrough(self):
        assert with_tail_context("问题") == "问题"

    def test_empty_content_blocks_only(self):
        assert with_tail_context("", "记忆块") == "记忆块"

    def test_block_whitespace_stripped(self):
        out = with_tail_context("问题", "\n 记忆块 \n")
        assert out == "问题\n\n记忆块"


class TestDisciplinedMessages:
    def test_assembly_shape(self):
        history = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
        ]
        messages = disciplined_messages("稳定系统提示", history, "q2\n\n动态块")
        assert messages == [
            {"role": "system", "content": "稳定系统提示"},
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2\n\n动态块"},
        ]

    def test_history_roles_filtered(self):
        history = [
            {"role": "system", "content": "旧系统段"},
            {"role": "tool", "content": "x"},
            {"role": "user", "content": "q1"},
        ]
        messages = disciplined_messages("sys", history, "q2")
        assert [m["role"] for m in messages] == ["system", "user", "user"]

    def test_empty_history(self):
        messages = disciplined_messages("sys", [], "q")
        assert [m["role"] for m in messages] == ["system", "user"]

    def test_history_not_mutated(self):
        history = [{"role": "user", "content": "q1"}]
        disciplined_messages("sys", history, "q2")
        assert history == [{"role": "user", "content": "q1"}]
