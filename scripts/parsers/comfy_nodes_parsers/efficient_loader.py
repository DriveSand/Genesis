"""Efficient Loader: 同时加载 checkpoint 与 LoRA 堆 (Baked VAE 不入库)。"""
MODEL_TYPES = ("Efficient Loader",)
PROMPT_TYPES = ("Efficient Loader",)


def extract_model(inputs, ctx, _class_type):
    checkpoint = inputs.get("ckpt_name")
    if not isinstance(checkpoint, str) or not checkpoint.strip() \
            or checkpoint.strip().lower() == "none":
        checkpoint = None
    loras = ctx.loras(inputs.get("lora_stack"))
    lora_name = inputs.get("lora_name")
    if isinstance(lora_name, str) and lora_name.strip() \
            and lora_name.strip().lower() != "none":
        loras.append(ctx.lora(lora_name, inputs.get("lora_model_strength", 1.0)))
    return checkpoint, loras, None


def extract_prompt(inputs, side, ctx):
    return ctx.prompt(inputs.get(side), side)
