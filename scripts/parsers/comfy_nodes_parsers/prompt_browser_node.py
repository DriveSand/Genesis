"""Prompt Browser: prompt_text 为纯文本提示词。"""
PROMPT_TYPES = ("PromptBrowserNode",)


def extract_prompt(inputs, side, ctx):
    return ctx.prompt(inputs.get("prompt_text"), side)
