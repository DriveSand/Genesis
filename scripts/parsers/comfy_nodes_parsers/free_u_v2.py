"""FreeU: 模型透传节点 (FreeU / FreeU_V2)。"""
MODEL_TYPES = ("FreeU_V2", "FreeU")


def extract_model(inputs, ctx, _class_type):
    checkpoint, loras = ctx.model(inputs.get("model"))
    return checkpoint, loras, None
