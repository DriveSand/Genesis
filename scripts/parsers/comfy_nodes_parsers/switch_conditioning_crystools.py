"""Crystools: Switch conditioning (按 boolean 选择 on_true/on_false)。"""
PROMPT_TYPES = ("Switch conditioning [Crystools]",)


def extract_prompt(inputs, side, ctx):
    enabled = bool(ctx.scalar(inputs.get("boolean")))
    key = "on_true" if enabled else "on_false"
    return ctx.prompt(inputs.get(key), side)
