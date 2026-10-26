"""easy-use: easy promptConcat (按 separator 拼接多段提示词)。"""
PROMPT_TYPES = ("easy promptConcat",)


def extract_prompt(inputs, side, ctx):
    first = ctx.prompt(inputs.get("prompt1"), side)
    second = ctx.prompt(inputs.get("prompt2"), side)
    separator = inputs.get("separator")
    if not isinstance(separator, str):
        separator = ","
    return ctx.join([first, second], separator)
