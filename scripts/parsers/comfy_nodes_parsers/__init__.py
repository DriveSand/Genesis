"""ComfyUI 自定义节点解析器注册表。

每个文件对应一种/一组自定义节点, 按节点 class_type 注册:

- PROMPT_TYPES + extract_prompt(inputs, side, ctx)
- MODEL_TYPES  + extract_model(inputs, ctx, class_type)
- UPSCALE_TYPES + extract_upscale(inputs, ctx)

ctx 由 comfyui.py 提供, 包含 prompt/scalar/model/loras/lora/... 等递归接口,
节点模块不直接依赖解析器核心, 便于独立扩展。
"""
from scripts.parsers.comfy_nodes_parsers import (
    bnk_clip_text_encode_advanced,
    cr_combine_prompt,
    cr_upscale_image,
    easy_lora_stack,
    easy_lora_stack_apply,
    easy_negative,
    easy_positive,
    easy_prompt_concat,
    efficient_loader,
    free_u_v2,
    lora_manager,
    prompt_browser_node,
    reserved_vram_setter,
    sd_xl_prompt_styler,
    switch_conditioning_crystools,
    text_concatenate_jps,
    upscale_model_loader,
    wei_lin_prompt_ui_only_lora_stack,
    wei_lin_prompt_ui_without_lora,
)

_MODULES = (
    bnk_clip_text_encode_advanced,
    cr_combine_prompt,
    cr_upscale_image,
    easy_lora_stack,
    easy_lora_stack_apply,
    easy_negative,
    easy_positive,
    easy_prompt_concat,
    efficient_loader,
    free_u_v2,
    lora_manager,
    prompt_browser_node,
    reserved_vram_setter,
    sd_xl_prompt_styler,
    switch_conditioning_crystools,
    text_concatenate_jps,
    upscale_model_loader,
    wei_lin_prompt_ui_only_lora_stack,
    wei_lin_prompt_ui_without_lora,
)

PROMPT_PARSERS = {
    node_type: module
    for module in _MODULES
    for node_type in getattr(module, "PROMPT_TYPES", ())
}
MODEL_PARSERS = {
    node_type: module
    for module in _MODULES
    for node_type in getattr(module, "MODEL_TYPES", ())
}
UPSCALE_PARSERS = {
    node_type: module
    for module in _MODULES
    for node_type in getattr(module, "UPSCALE_TYPES", ())
}


def prompt_parser(class_type: str):
    return PROMPT_PARSERS.get(class_type)


def model_parser(class_type: str):
    return MODEL_PARSERS.get(class_type)


def upscale_parser(class_type: str):
    return UPSCALE_PARSERS.get(class_type)


__all__ = [
    "MODEL_PARSERS",
    "PROMPT_PARSERS",
    "UPSCALE_PARSERS",
    "model_parser",
    "prompt_parser",
    "upscale_parser",
]
