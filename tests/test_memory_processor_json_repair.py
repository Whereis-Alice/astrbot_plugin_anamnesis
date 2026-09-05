r"""JSON 修复回归测试（Bug A）：_try_fix_json 不得破坏合法的多行 JSON。

背景：修复前 `_try_fix_json` 末尾对整个字符串执行
`replace("\n", "\\n")`，把 JSON 的**结构性换行**也转义成字面 `\n`，
导致 LLM 输出的多行 JSON 反而被"修坏"；而 `_parse_merge_response`
又对每个候选再套一次该函数，使 `merge_memories` 必然抛
RuntimeError("合并结果 JSON 解析失败")。
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot_plugin_anamnesis.core.processors.memory_processor import MemoryProcessor

# 真实风格的多行合并响应（绝大多数 LLM 默认输出这种缩进格式）
MULTILINE_MERGE_RESPONSE = """{
  "summary": "小明和小红确定了周末去西湖骑行的计划，并约好了集合时间与地点。",
  "key_facts": [
    "集合时间是周六上午九点",
    "集合地点在龙翔桥地铁站 A 口",
    "小红负责租两辆共享单车"
  ],
  "topics": ["出行计划", "杭州"],
  "importance": 0.62
}"""


def _make_processor() -> MemoryProcessor:
    return MemoryProcessor(llm_provider=Mock(), context=None)


class TestTryFixJsonKeepsValidJson:
    """合法 JSON 必须原样通过修复函数。"""

    def test_multiline_json_stays_parseable(self):
        processor = _make_processor()
        fixed = processor._try_fix_json(MULTILINE_MERGE_RESPONSE)
        # 结构性换行被转义后 json.loads 会直接报错
        assert json.loads(fixed) == json.loads(MULTILINE_MERGE_RESPONSE)

    def test_multiline_json_is_untouched(self):
        processor = _make_processor()
        assert (
            processor._try_fix_json(MULTILINE_MERGE_RESPONSE)
            == MULTILINE_MERGE_RESPONSE.strip()
        )

    def test_compact_json_is_idempotent(self):
        processor = _make_processor()
        raw = '{"summary": "s", "key_facts": ["a"], "importance": 0.5}'
        assert processor._try_fix_json(raw) == raw
        assert processor._try_fix_json(processor._try_fix_json(raw)) == raw

    def test_escaped_newline_inside_string_is_not_double_escaped(self):
        processor = _make_processor()
        raw = '{"summary": "第一行\\n第二行"}'
        data = json.loads(processor._try_fix_json(raw))
        assert data["summary"] == "第一行\n第二行"

    def test_string_containing_brace_and_comma_is_preserved(self):
        """字符串字面量里的 `, }` 不能被"移除尾随逗号"的正则吃掉。"""
        processor = _make_processor()
        raw = '{"summary": "配置写成 {\\"a\\": 1, } 就报错了"}'
        data = json.loads(processor._try_fix_json(raw))
        assert data["summary"] == '配置写成 {"a": 1, } 就报错了'


class TestTryFixJsonToleranceNotRegressed:
    """畸形输入的容错能力不得下降。"""

    def test_fixes_trailing_comma_single_line(self):
        processor = _make_processor()
        data = json.loads(processor._try_fix_json('{"a": 1, "b": [1, 2,],}'))
        assert data == {"a": 1, "b": [1, 2]}

    def test_fixes_trailing_comma_multiline(self):
        processor = _make_processor()
        raw = """{
  "summary": "多行且带尾随逗号",
  "topics": ["a", "b",],
}"""
        data = json.loads(processor._try_fix_json(raw))
        assert data == {"summary": "多行且带尾随逗号", "topics": ["a", "b"]}

    def test_fixes_single_quotes(self):
        processor = _make_processor()
        data = json.loads(
            processor._try_fix_json("{'summary': '单引号响应', 'importance': 0.4}")
        )
        assert data == {"summary": "单引号响应", "importance": 0.4}

    def test_strips_markdown_json_fence(self):
        processor = _make_processor()
        raw = '```json\n{\n  "summary": "围栏里的多行 JSON"\n}\n```'
        data = json.loads(processor._try_fix_json(raw))
        assert data == {"summary": "围栏里的多行 JSON"}

    def test_strips_bare_markdown_fence(self):
        processor = _make_processor()
        raw = '```\n{"summary": "无语言标记的围栏"}\n```'
        assert json.loads(processor._try_fix_json(raw))["summary"] == "无语言标记的围栏"

    def test_strips_surrounding_prose(self):
        processor = _make_processor()
        raw = '好的，以下是合并后的记忆：\n{\n  "summary": "前后有杂字"\n}\n希望对你有帮助！'
        data = json.loads(processor._try_fix_json(raw))
        assert data == {"summary": "前后有杂字"}

    def test_escapes_bare_newline_inside_string(self):
        processor = _make_processor()
        raw = '{"summary": "第一行\n第二行", "importance": 0.5}'
        data = json.loads(processor._try_fix_json(raw))
        assert data["summary"] == "第一行\n第二行"
        assert data["importance"] == 0.5

    def test_escapes_bare_tab_inside_string(self):
        processor = _make_processor()
        raw = '{"summary": "列一\t列二"}'
        assert json.loads(processor._try_fix_json(raw))["summary"] == "列一\t列二"

    def test_closes_truncated_json(self):
        processor = _make_processor()
        raw = '{"summary": "被截断的摘要'
        assert json.loads(processor._try_fix_json(raw))["summary"] == "被截断的摘要"

    def test_closes_truncated_multiline_json(self):
        processor = _make_processor()
        raw = '{\n  "summary": "被截断的多行摘要",\n  "key_facts": ["事实一"'
        data = json.loads(processor._try_fix_json(raw))
        assert data["summary"] == "被截断的多行摘要"
        assert data["key_facts"] == ["事实一"]

    def test_keeps_nested_object_when_outer_brace_missing(self):
        """截断修复优先于"抽取平衡片段"，不能只捞出内层对象。"""
        processor = _make_processor()
        raw = '{"outer": {"inner": 1}'
        assert json.loads(processor._try_fix_json(raw)) == {"outer": {"inner": 1}}


class TestParseMergeResponse:
    def test_parses_multiline_json(self):
        processor = _make_processor()
        data = processor._parse_merge_response(MULTILINE_MERGE_RESPONSE)
        assert data["importance"] == 0.62
        assert len(data["key_facts"]) == 3

    def test_parses_multiline_json_in_fence(self):
        processor = _make_processor()
        data = processor._parse_merge_response(
            f"```json\n{MULTILINE_MERGE_RESPONSE}\n```"
        )
        assert data["topics"] == ["出行计划", "杭州"]

    def test_uses_raw_candidate_before_repair(self):
        """候选循环必须先直接 json.loads，不能无条件再套一次修复函数。"""
        processor = _make_processor()
        processor._try_fix_json = Mock(return_value="<<broken by fixer>>")
        data = processor._parse_merge_response('{"summary": "原样可解析"}')
        assert data["summary"] == "原样可解析"

    def test_still_raises_on_garbage(self):
        processor = _make_processor()
        with pytest.raises(RuntimeError):
            processor._parse_merge_response("这段文本里完全没有 JSON")


class TestMergeMemoriesEndToEnd:
    @pytest.mark.asyncio
    async def test_accepts_multiline_llm_response(self):
        processor = _make_processor()
        processor._call_llm_with_retry = AsyncMock(
            return_value=MULTILINE_MERGE_RESPONSE
        )

        result = await processor.merge_memories(
            [
                {"id": 1, "content": "a", "metadata": {"persona_summary": "记忆一"}},
                {"id": 2, "content": "b", "metadata": {"persona_summary": "记忆二"}},
            ]
        )

        assert result["summary"].startswith("小明和小红")
        assert result["key_facts"] == [
            "集合时间是周六上午九点",
            "集合地点在龙翔桥地铁站 A 口",
            "小红负责租两辆共享单车",
        ]
        assert result["importance"] == 0.62

    @pytest.mark.asyncio
    async def test_accepts_multiline_response_wrapped_in_fence(self):
        processor = _make_processor()
        processor._call_llm_with_retry = AsyncMock(
            return_value=f"```json\n{MULTILINE_MERGE_RESPONSE}\n```"
        )
        result = await processor.merge_memories([{"content": "a", "metadata": {}}])
        assert result["topics"] == ["出行计划", "杭州"]