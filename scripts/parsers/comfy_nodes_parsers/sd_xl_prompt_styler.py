"""SDXL Prompt Styler: 由节点分别提供正向/反向文本。"""
PROMPT_TYPES = ("SDXLPromptStyler",)


def extract_prompt(inputs, side, ctx):
    key = "text_positive" if side == "positive" else "text_negative"
    return ctx.prompt(inputs.get(key), side)
