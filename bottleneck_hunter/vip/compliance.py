"""合规免责声明 —— 唯一真源（裁决 C8）。

三处挂载：① 定期报告末尾；② 咨询聊天 SSE `disclaimer` 终结事件；③ 审计记 `disclaimer_version`。
修改声明只改这一处；版本号变更即可在审计里追溯"用户当时看到的是哪版免责"。
"""
from __future__ import annotations

DISCLAIMER_VERSION = "2026-07-v1"

DISCLAIMER_ZH = (
    "【重要声明】本报告/回答由 AI 系统基于你导入的对账单与公开市场数据自动生成，"
    "仅供信息参考，**不构成任何投资建议、要约或承诺**，亦不保证数据完整或准确。"
    "投资有风险，任何操作决策及其后果由你本人独立判断并承担。"
    "涉及具体买卖、税务、法律事项请咨询持牌专业人士。"
)


def with_disclaimer(text: str) -> str:
    """在文本末尾拼接免责声明（唯一拼接入口）。"""
    body = text or ""
    return f"{body}\n\n---\n{DISCLAIMER_ZH}"


def demo() -> None:
    out = with_disclaimer("测试报告正文")
    assert "测试报告正文" in out and DISCLAIMER_ZH in out
    assert DISCLAIMER_ZH in with_disclaimer("")   # 空正文也带声明
    assert DISCLAIMER_VERSION == "2026-07-v1"
    print("compliance 自检通过")


if __name__ == "__main__":
    demo()
