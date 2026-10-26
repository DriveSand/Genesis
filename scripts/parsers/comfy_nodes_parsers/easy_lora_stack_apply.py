"""easy-use: easy loraStackApply (应用 LoRA 堆并继续 model 链)。"""
MODEL_TYPES = ("easy loraStackApply",)


def extract_model(inputs, ctx, _class_type):
    checkpoint, loras = ctx.model(inputs.get("model"))
    loras += ctx.loras(inputs.get("lora_stack"))
    return checkpoint, loras, None
