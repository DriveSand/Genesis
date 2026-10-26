"""easy-use: easy positive (纯文本正向提示词节点)。"""
PROMPT_TYPES = ("easy positive",)


def extract_prompt(inputs, side, ctx):
    return ctx.prompt(inputs.get("positive"), "positive")
