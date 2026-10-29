"""WeiLin Prompt UI: 纯提示词节点 (字段名固定为 positive)。"""
PROMPT_TYPES = ("WeiLinPromptUIWithoutLora",)


def extract_prompt(inputs, side, ctx):
    return ctx.prompt(inputs.get("positive"), side)
