"""BNK: CLIPTextEncodeAdvanced (text 为字符串或上游文本链接)。"""
PROMPT_TYPES = ("BNK_CLIPTextEncodeAdvanced",)


def extract_prompt(inputs, side, ctx):
    return ctx.prompt(inputs.get("text"), side)
