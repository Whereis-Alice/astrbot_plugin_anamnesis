"""
constants.py - 插件使用的常量
"""

# 注入到 System Prompt 的记忆头尾格式
MEMORY_INJECTION_HEADER = "<Anamnesis-Memory>"
MEMORY_INJECTION_FOOTER = "</Anamnesis-Memory>"

# 伪造工具调用注入相关常量
FAKE_TOOL_CALL_NAME = "anamnesis_recall_memory"  # 复用已注册的工具名
FAKE_TOOL_CALL_ID_PREFIX = "anam_recall_stub_"  # ID 前缀，用于清理时识别伪造消息
