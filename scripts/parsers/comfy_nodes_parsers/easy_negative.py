"""easy-use: easy negative (纯文本反向提示词节点)。"""
PROMPT_TYPES = ("easy negative",)


def extract_prompt(inputs, side, ctx):
    return ctx.prompt(inputs.get("negative"), "negative")
