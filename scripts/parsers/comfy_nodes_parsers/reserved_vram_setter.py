"""显存预留节点: anything 为透传输入。"""
PROMPT_TYPES = ("ReservedVRAMSetter",)


def extract_prompt(inputs, side, ctx):
    return ctx.prompt(inputs.get("anything"), side)
