"""comfyui.py —— ComfyUI 工作流解析器。

支持 ComfyUI 原生 PNG 中的 `prompt` (API 工作流) 与 `workflow` (界面工作流)
chunk：从输出节点回溯到 KSampler, 提取提示词、采样参数、模型与 LoRA。

适配自 receyuki/stable-diffusion-prompt-reader 的 ComfyUI 解析器
(https://github.com/receyuki/stable-diffusion-prompt-reader, 许可证见
THIRD_PARTY_LICENSES/stable-diffusion-prompt-reader/LICENSE), 改写为本项目
ParserModule 契约, 并补充常用自定义节点的处理。

优先级：本解析器在注册表中位于 A1111 之前。图片同时具备 ComfyUI 工作流与
A1111 infotext 时由本解析器独占解析, tool 记为 "ComfyUI"; 工作流中缺失的
字段用 A1111 infotext 补齐。

硬性约束：无文件 I/O、无 SQL、不 import scripts.database / file_manager。

原作者信息：
__author__ = "receyuki"
__filename__ = "comfyui.py"
__copyright__ = "Copyright 2023"
__email__ = "receyuki@gmail.com"
"""
from __future__ import annotations

import json
import re
from collections import deque
from typing import TYPE_CHECKING, Any

from scripts.logger import log

if TYPE_CHECKING:
    from scripts.parsers import RawMetadata

TOOL_NAME = "ComfyUI"

# ---- 节点类型 ----
_KSAMPLER_TYPES = {"KSampler", "KSamplerAdvanced", "KSampler (Efficient)"}
_SAVE_TYPES = {"SaveImage", "Image Save", "SDPromptSaver"}
_CHECKPOINT_TYPES = {
    "CheckpointLoader",
    "CheckpointLoaderSimple",
    "unCLIPCheckpointLoader",
    "Checkpoint Loader (Simple)",
}
_LORA_TYPES = {"LoraLoader", "LoraLoaderModelOnly"}

_LORA_TAG_RE = re.compile(r"<(lora|lyco):((?:\\.|[^<>\\])+?):([^<>]*)>")
# ComfyUI Prompt Saver 写出的 infotext 参数: Version: ComfyUI
_COMFY_VERSION_RE = re.compile(r"(?:^|,\s*)Version:\s*ComfyUI(?:\s*,|\s*$)")
# 只剥离已知模型扩展名 (不能用 splitext: 版本号里的点会被误判为扩展名)
_MODEL_EXT_RE = re.compile(r"\.(?:safetensors|ckpt|pt|pth|bin|gguf)$", re.I)


def can_parse(raw: "RawMetadata") -> bool:
    """判定是否包含 ComfyUI 工作流特征。

    只要 `prompt` 是节点图 (值为含 class_type 的对象), 或 `workflow` 含
    nodes 数组, 即视为 ComfyUI 图片; 本判定优先于 A1111。
    """
    if raw.format != "PNG":
        return False
    prompt = _load_json(raw.text.get("prompt"))
    if _is_node_graph(prompt):
        return True
    workflow = _load_json(raw.text.get("workflow"))
    if isinstance(workflow, dict) and isinstance(workflow.get("nodes"), list):
        return True
    # 工作流被移除但保留了 ComfyUI 写出的 infotext (SDPromptSaver)
    return bool(_COMFY_VERSION_RE.search(raw.text.get("parameters") or ""))


def parse(raw: "RawMetadata") -> dict[str, Any]:
    """解析 ComfyUI 工作流, 返回结构化元数据 (含 models 四键)。"""
    prompt = _load_json(raw.text.get("prompt"))
    nodes: dict[str, Any] = prompt if _is_node_graph(prompt) else {}
    saver = _first_saver_inputs(nodes)
    ksampler, starts = _pick_ksampler(nodes)
    fallback = _a1111_fallback(raw)

    positive = negative = ""
    seed: int | None = None
    steps: int | None = None
    cfg: float | None = None
    sampler = scheduler = ""
    sampler_from_infotext = False
    checkpoint: tuple[str, str] | None = None
    loras: list[dict[str, Any]] = []
    extras: dict[str, Any] = {}

    # 1) 工作流 KSampler: 提示词与采样参数的首要来源
    if ksampler is not None:
        inputs = ksampler.get("inputs") or {}
        positive = _prompt_from(nodes, inputs.get("positive"), "positive") or ""
        negative = _prompt_from(nodes, inputs.get("negative"), "negative") or ""
        seed = _as_int(_resolve_scalar(nodes, inputs.get("seed")))
        if seed is None:
            seed = _as_int(_resolve_scalar(nodes, inputs.get("noise_seed")))
        steps = _as_int(_resolve_scalar(nodes, inputs.get("steps")))
        cfg = _as_float(_resolve_scalar(nodes, inputs.get("cfg")))
        sampler = _as_str(_resolve_scalar(nodes, inputs.get("sampler_name")))
        scheduler = _as_str(_resolve_scalar(nodes, inputs.get("scheduler")))
        checkpoint, loras = _collect_model_chain(nodes, inputs.get("model"))
        extras = _ksampler_extras(nodes, inputs)

    # 2) SDPromptSaver: 写图时显式配置的模型/VAE/提示词 (工作流缺失时补齐)
    if saver:
        if not positive:
            positive = _prompt_from(nodes, saver.get("positive"), "positive") or ""
        if not negative:
            negative = _prompt_from(nodes, saver.get("negative"), "negative") or ""
        if seed is None:
            seed = _as_int(_resolve_scalar(nodes, saver.get("seed")))
        if steps is None:
            steps = _as_int(_resolve_scalar(nodes, saver.get("steps")))
        if cfg is None:
            cfg = _as_float(_resolve_scalar(nodes, saver.get("cfg")))
        if not sampler:
            sampler = _as_str(_resolve_scalar(nodes, saver.get("sampler_name")))
        if not scheduler:
            scheduler = _as_str(_resolve_scalar(nodes, saver.get("scheduler")))

    # 3) SDPromptSaver / A1111 infotext 中的模型与 VAE
    saver_model = _as_str(saver.get("model_name")) if saver else ""
    saver_vae = _as_str(saver.get("vae_name")) if saver else ""
    if checkpoint is None and saver_model:
        checkpoint = _split_model_name(saver_model, last_underscore=False)
    vae = _split_model_name(saver_vae, last_underscore=True) if saver_vae else None

    # 4) A1111 infotext 兜底 (保留 tool=ComfyUI)
    if fallback:
        if not positive:
            positive = str(fallback.get("positive_prompt") or "")
        if not negative:
            negative = str(fallback.get("negative_prompt") or "")
        if seed is None:
            seed = _as_int(fallback.get("seed"))
        if steps is None:
            steps = _as_int(fallback.get("steps"))
        if cfg is None:
            cfg = _as_float(fallback.get("cfg"))
        if not sampler:
            sampler = str(fallback.get("sampler") or "")
            sampler_from_infotext = bool(sampler)
        if not scheduler:
            scheduler = str(fallback.get("scheduler") or "")
        if checkpoint is None:
            f_ckpts = (fallback.get("models") or {}).get("checkpoints") or []
            if f_ckpts:
                first = f_ckpts[0]
                checkpoint = (str(first.get("name") or ""),
                              str(first.get("version") or ""))
        if vae is None:
            f_vaes = (fallback.get("models") or {}).get("vaes") or []
            if f_vaes:
                first = f_vaes[0]
                vae = (str(first.get("name") or ""),
                       str(first.get("version") or ""))
            else:
                # SDPromptSaver 的 VAE 写在 infotext 的 "VAE:" 参数里,
                # A1111 解析器只识别 vae_name, 这里从 note 的额外参数补齐
                vae = _fallback_vae(fallback)
        loras = _merge_loras(loras,
                             (fallback.get("models") or {}).get("loras") or [])

    # 5) 工作流未给出 VAE 时, 取 VAEDecode 上连接的 VAELoader
    if vae is None:
        vae = _find_vae(nodes, starts)

    # 6) 调度器无法确定时, 按 A1111 infotext 的 "Sampler" 末段下划线猜测
    if not scheduler and sampler_from_infotext:
        head, sep, tail = sampler.rpartition("_")
        if sep and head:
            sampler, scheduler = head, tail

    # 7) 提示词中的 <lora:...> / <lyco:...> 内联标签
    loras = _merge_loras(loras,
                         _inline_loras(positive + "\n" + negative))

    if not (positive or negative or seed is not None or steps or cfg
            or checkpoint or vae or loras):
        log.debug(f"ComfyUI workflow yielded no usable metadata: {raw.file_path}")
        return {}

    return {
        "positive_prompt": positive.strip(),
        "negative_prompt": negative.strip(),
        "seed": seed or 0,
        "steps": steps or 0,
        "cfg": cfg or 0.0,
        "sampler": sampler,
        "scheduler": scheduler,
        "note": _build_note(fallback, extras),
        "models": {
            "checkpoints": ([_model(checkpoint[0], checkpoint[1])]
                            if checkpoint and checkpoint[0] else []),
            "loras": loras,
            "embeddings": [],
            "vaes": ([_model(vae[0], vae[1])]
                     if vae and vae[0] else []),
        },
    }


# ---------------------------------------------------------------------- #
# 工作流定位
# ---------------------------------------------------------------------- #

def _is_node_graph(value: Any) -> bool:
    """是否为 ComfyUI API 工作流节点图 {id: {class_type, inputs}}。"""
    return isinstance(value, dict) and any(
        isinstance(node, dict) and isinstance(node.get("class_type"), str)
        for node in value.values())


def _load_json(value: Any) -> Any:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return None


def _link(value: Any) -> str | None:
    """ComfyUI 链接形式 ["节点id", 输出槽] -> 节点 id 字符串。"""
    if isinstance(value, (list, tuple)) and value:
        head = value[0]
        if isinstance(head, (str, int)):
            return str(head)
    return None


def _first_saver_inputs(nodes: dict[str, Any]) -> dict[str, Any]:
    """取第一个 SDPromptSaver 节点的 inputs (写图时显式配置的元数据)。"""
    for node in nodes.values():
        if isinstance(node, dict) and node.get("class_type") == "SDPromptSaver":
            return node.get("inputs") or {}
    return {}


def _pick_ksampler(nodes: dict[str, Any]
                   ) -> tuple[dict[str, Any] | None, list[str]]:
    """从输出节点回溯, 返回距输出最近的 KSampler 及其起点列表。

    同一工作流可能存在多个采样阶段 (如高清修复), 距 SaveImage 最近者
    才是最终保存到图片的参数来源。
    """
    starts = [nid for nid, node in nodes.items()
              if isinstance(node, dict)
              and node.get("class_type") in _SAVE_TYPES]
    if not starts:
        starts = list(nodes)
    best: tuple[int, str] | None = None
    for start in starts:
        queue: deque[tuple[str, int]] = deque([(start, 0)])
        seen = {start}
        while queue:
            nid, distance = queue.popleft()
            node = nodes.get(nid)
            if not isinstance(node, dict):
                continue
            if node.get("class_type") in _KSAMPLER_TYPES:
                if best is None or distance < best[0]:
                    best = (distance, nid)
                break  # 不再深入 KSampler 上游
            for value in (node.get("inputs") or {}).values():
                linked = _link(value)
                if linked is not None and linked in nodes and linked not in seen:
                    seen.add(linked)
                    queue.append((linked, distance + 1))
    return (nodes.get(best[1]) if best else None), starts


def _find_vae(nodes: dict[str, Any], starts: list[str]) -> tuple[str, str] | None:
    """沿输出路径查找 VAEDecode 连接的 VAELoader。"""
    queue: deque[str] = deque(starts)
    seen: set[str] = set(starts)
    while queue:
        nid = queue.popleft()
        node = nodes.get(nid)
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs") or {}
        if node.get("class_type") == "VAEDecode":
            linked = _link(inputs.get("vae"))
            target = nodes.get(linked) if linked else None
            if isinstance(target, dict) and target.get("class_type") == "VAELoader":
                name = _as_str((target.get("inputs") or {}).get("vae_name"))
                if name:
                    return _split_model_name(name, last_underscore=True)
        for value in inputs.values():
            linked = _link(value)
            if linked is not None and linked in nodes and linked not in seen:
                seen.add(linked)
                queue.append(linked)
    return None


# ---------------------------------------------------------------------- #
# 参数与提示词
# ---------------------------------------------------------------------- #

def _resolve_scalar(nodes: dict[str, Any], value: Any) -> Any:
    """把字面值或指向原始值节点的链接解析为实际标量。"""
    linked = _link(value)
    if linked is None:
        return value
    target = nodes.get(linked)
    if not isinstance(target, dict):
        return None
    inputs = target.get("inputs") or {}
    if "value" in inputs:
        return inputs["value"]
    if "seed" in inputs and len(inputs) <= 2:
        return inputs["seed"]
    if "string" in inputs:
        return inputs["string"]
    return None


def _prompt_from(nodes: dict[str, Any], value: Any, side: str,
                 seen: set[str] | None = None) -> str | None:
    """递归提取 conditioning -> 提示词文本; 无法识别时返回 None。"""
    if isinstance(value, str):
        return value
    linked = _link(value)
    if linked is None or linked not in nodes:
        return None
    if seen is None:
        seen = set()
    if linked in seen:
        return None
    seen.add(linked)
    node = nodes[linked]
    inputs = node.get("inputs") or {}
    node_type = node.get("class_type")

    if node_type == "CLIPTextEncode":
        return _prompt_from(nodes, inputs.get("text"), side, seen)
    if node_type == "CLIPTextEncodeSDXL":
        text_g = _prompt_from(nodes, inputs.get("text_g"), side, seen)
        text_l = _prompt_from(nodes, inputs.get("text_l"), side, seen)
        return _merge_clip(text_g, text_l)
    if node_type == "CLIPTextEncodeSDXLRefiner":
        return _prompt_from(nodes, inputs.get("text"), side, seen)
    if node_type == "easy positive":
        return _prompt_from(nodes, inputs.get("positive"), "positive", seen)
    if node_type == "easy negative":
        return _prompt_from(nodes, inputs.get("negative"), "negative", seen)
    if node_type == "easy promptConcat":
        first = _prompt_from(nodes, inputs.get("prompt1"), side, seen)
        second = _prompt_from(nodes, inputs.get("prompt2"), side, seen)
        separator = inputs.get("separator")
        separator = separator if isinstance(separator, str) else ","
        return _join_prompts([first, second], separator)
    if node_type == "SDXLPromptStyler":
        key = "text_positive" if side == "positive" else "text_negative"
        return _prompt_from(nodes, inputs.get(key), side, seen)
    if node_type == "ConditioningCombine":
        first = _prompt_from(nodes, inputs.get("conditioning_1"), side, seen)
        second = _prompt_from(nodes, inputs.get("conditioning_2"), side, seen)
        return _join_prompts([first, second], ", ")
    if node_type in ("ControlNetApplyAdvanced", "ControlNetApplySD3"):
        return _prompt_from(nodes, inputs.get(side), side, seen)
    if node_type == "ControlNetApply":
        return _prompt_from(nodes, inputs.get("conditioning"), side, seen)
    if node_type == "ConditioningSetArea":
        return _prompt_from(nodes, inputs.get("conditioning"), side, seen)

    # 未知节点: 依次尝试同名输入、conditioning/text 与原始值
    for key in (side, "conditioning", "text", "positive", "negative"):
        if key in inputs:
            result = _prompt_from(nodes, inputs[key], side, seen)
            if result is not None:
                return result
    if isinstance(inputs.get("value"), str):
        return inputs["value"]
    return None


def _merge_clip(text_g: str | None, text_l: str | None) -> str | None:
    """SDXL 双文本编码合并: 相同时取一份, 否则按行拼接。"""
    if text_g is None:
        return text_l
    if text_l is None:
        return text_g
    if text_g.strip() == text_l.strip():
        return text_g
    return ",\n".join(part for part in (text_g, text_l) if part)


def _join_prompts(parts: list[str | None], separator: str) -> str:
    return separator.join(part for part in parts if part and part.strip())


def _ksampler_extras(nodes: dict[str, Any],
                     inputs: dict[str, Any]) -> dict[str, Any]:
    """采集非标准采样参数 (KSamplerAdvanced 等), 供 note 保存。"""
    extras: dict[str, Any] = {}
    for key in ("add_noise", "start_at_step", "end_at_step",
                "return_with_left_over_noise"):
        if key in inputs:
            extras[key] = _resolve_scalar(nodes, inputs[key])
    denoise = _as_float(_resolve_scalar(nodes, inputs.get("denoise")))
    if denoise is not None and denoise != 1.0:
        extras["denoise"] = denoise
    return extras


# ---------------------------------------------------------------------- #
# 模型
# ---------------------------------------------------------------------- #

def _collect_model_chain(nodes: dict[str, Any], value: Any,
                         seen: set[str] | None = None
                         ) -> tuple[tuple[str, str] | None, list[dict[str, Any]]]:
    """沿 model 链接回溯, 返回 (checkpoint, loras)。"""
    linked = _link(value)
    if linked is None or linked not in nodes:
        return None, []
    if seen is None:
        seen = set()
    if linked in seen:
        return None, []
    seen.add(linked)
    node = nodes[linked]
    inputs = node.get("inputs") or {}
    node_type = node.get("class_type")

    if node_type in _CHECKPOINT_TYPES:
        name = _as_str(inputs.get("ckpt_name") or inputs.get("checkpoint"))
        return (_split_model_name(name, last_underscore=False)
                if name else None), []

    loras: list[dict[str, Any]] = []
    if node_type in _LORA_TYPES:
        loras.append(_lora(inputs.get("lora_name"),
                           inputs.get("strength_model",
                                      inputs.get("strength", 1.0))))
    elif node_type == "easy loraStackApply":
        loras.extend(_easy_lora_stack(nodes, inputs.get("lora_stack")))
    elif node_type == "easy loraStack":
        loras.extend(_easy_stack_inputs(inputs))
    elif node_type == "Lora Loader (LoraManager)":
        manager_loras = _lora_manager_loras(inputs.get("loras"))
        if not manager_loras:
            # loras 列表缺失时退回解析节点文本中的 <lora:...> 标签
            manager_loras = _inline_loras(_as_str(inputs.get("text")))
        loras.extend(manager_loras)

    parent, parents = _collect_model_chain(nodes, inputs.get("model"), seen)
    if parent is None and node_type not in _LORA_TYPES | {
            "easy loraStackApply", "Lora Loader (LoraManager)"}:
        # 未知桥接节点: 继续沿常见模型输入回溯
        for key in ("base_model", "optional_model", "MODEL"):
            if key in inputs:
                parent, parents = _collect_model_chain(nodes, inputs[key], seen)
                if parent is not None:
                    break
    return parent, loras + parents


def _easy_lora_stack(nodes: dict[str, Any],
                     value: Any) -> list[dict[str, Any]]:
    linked = _link(value)
    target = nodes.get(linked) if linked else None
    if not isinstance(target, dict):
        return []
    if target.get("class_type") == "easy loraStack":
        return _easy_stack_inputs(target.get("inputs") or {})
    return []


def _easy_stack_inputs(inputs: dict[str, Any]) -> list[dict[str, Any]]:
    """easy loraStack: lora_N_name / strength / model_strength 系列字段。

    权重取决于 mode: "simple" 用 lora_N_strength (UNet/CLIP 相同),
    "advanced" 用 lora_N_model_strength (单权重字段记录 UNet 权重)。
    """
    if inputs.get("toggle") is False:
        return []
    mode = _as_str(inputs.get("mode")).lower()
    advanced = mode == "advanced"
    try:
        count = int(inputs.get("num_loras") or 0)
    except (TypeError, ValueError):
        count = 0
    result: list[dict[str, Any]] = []
    # num_loras 缺失时按实际存在的 lora_N_name 顺序扫描
    limit = count if count > 0 else 64
    for index in range(1, limit + 1):
        key = f"lora_{index}_name"
        if key not in inputs and count <= 0:
            break
        name = _as_str(inputs.get(key))
        if not name or name.lower() == "none":
            continue
        if advanced:
            weight = inputs.get(f"lora_{index}_model_strength",
                                inputs.get(f"lora_{index}_strength", 1.0))
        else:
            weight = inputs.get(f"lora_{index}_strength", 1.0)
        result.append(_lora(name, weight))
    return result


def _lora_manager_loras(entries: Any) -> list[dict[str, Any]]:
    """ComfyUI-Lora-Manager 的 loras 列表 (结构随版本变化, 宽松解析)。

    实际结构为 ``{"__value__": [{"name", "strength", "active", ...}]}``;
    仅收录 active != False 的条目, 权重取 strength (UNet 权重)。
    """
    # 新版节点把列表包在 __value__ / value 中
    if isinstance(entries, dict):
        entries = entries.get("__value__", entries.get("value"))
    if not isinstance(entries, list):
        return []
    result: list[dict[str, Any]] = []
    for entry in entries:
        if isinstance(entry, str):
            result.append(_lora(entry, 1.0))
        elif isinstance(entry, dict):
            if entry.get("active") is False:
                continue
            name = entry.get("name") or entry.get("lora_name") or entry.get("file")
            weight = entry.get("strength", entry.get("weight", 1.0))
            if isinstance(name, str) and name.strip():
                result.append(_lora(name, weight))
    return result


def _lora(name: Any, weight: Any) -> dict[str, Any]:
    """LoRA 条目; 名称与 VAE 一样按最后一个下划线拆分 name/version。"""
    model_name, version = _split_model_name(
        _as_str(name), last_underscore=True)
    return {
        "name": model_name,
        "version": version,
        "url": None,
        "weight": _as_float(weight, default=1.0),
        "model_type": 0,
    }


def _inline_loras(text: str) -> list[dict[str, Any]]:
    """<lora:NAME:WEIGHT> / <lyco:NAME:WEIGHT> 内联标签。"""
    result: list[dict[str, Any]] = []
    for match in _LORA_TAG_RE.finditer(text or ""):
        kind = match.group(1)
        raw_name = re.sub(r"\\(.)", r"\1", match.group(2)).strip()
        if not raw_name:
            continue
        name, version = _split_model_name(raw_name, last_underscore=True)
        result.append({
            "name": name,
            "version": version,
            "url": None,
            "weight": _as_float(match.group(3), default=1.0),
            "model_type": 0 if kind == "lora" else 1,
        })
    return result


def _merge_loras(primary: list[dict[str, Any]],
                 extra: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 (name, model_type) 合并, 保留已有项 (工作流权重优先)。"""
    merged = list(primary)
    seen = {(item["name"], item["model_type"]) for item in merged}
    for item in extra:
        key = (item.get("name"), item.get("model_type", 0))
        if not key[0] or key in seen:
            continue
        seen.add(key)
        merged.append({
            "name": key[0],
            "version": str(item.get("version") or ""),
            "url": None,
            "weight": _as_float(item.get("weight"), default=1.0),
            "model_type": int(item.get("model_type") or 0),
        })
    return merged


def _model(name: str, version: str) -> dict[str, Any]:
    return {"name": name, "version": version, "url": None}


def _model_stem(name: str) -> str:
    """去掉目录与已知模型扩展名: models/Lora/x.safetensors -> x。"""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    return _MODEL_EXT_RE.sub("", base)


def _split_model_name(name: str, last_underscore: bool
                      ) -> tuple[str, str]:
    """模型文件名 -> (name, version)。

    checkpoint 取第一个下划线之后作为版本 (与 A1111 解析器一致);
    VAE 取最后一个下划线之后作为版本 (如 qwen_image_2.1_vae_bf16)。
    """
    base = _model_stem(name)
    index = base.rfind("_") if last_underscore else base.find("_")
    if index <= 0:
        return base, ""
    return base[:index], base[index + 1:]


# ---------------------------------------------------------------------- #
# 兜底与结果组装
# ---------------------------------------------------------------------- #

def _fallback_vae(fallback: dict[str, Any]) -> tuple[str, str] | None:
    """从 A1111 兜底结果的 note.extra_params 中读取 "VAE" 参数。"""
    note = _load_json(fallback.get("note"))
    if not isinstance(note, dict):
        return None
    extras = note.get("extra_params")
    if not isinstance(extras, dict):
        return None
    for key, value in extras.items():
        if str(key).lower() not in ("vae", "vae_name"):
            continue
        name = _as_str(value)
        if name and name.lower() not in ("automatic", "none", "null"):
            return _split_model_name(name, last_underscore=True)
    return None


def _a1111_fallback(raw: "RawMetadata") -> dict[str, Any] | None:
    """图片同时带 A1111 infotext 时, 复用其解析结果补齐字段。"""
    if raw.format == "PNG":
        text = raw.text.get("parameters")
    else:
        text = raw.user_comment
    if not text or "Steps:" not in text:
        return None
    try:
        from scripts.parsers import a1111  # 延迟导入, 避免包初始化顺序问题
        if not a1111.can_parse(raw):
            return None
        return a1111.parse(raw)
    except Exception as exc:  # noqa: BLE001 兜底失败不影响工作流结果
        log.debug(f"A1111 fallback failed: {raw.file_path} ({exc})")
        return None


def _build_note(fallback: dict[str, Any] | None,
                extras: dict[str, Any]) -> str:
    note: dict[str, Any] = {}
    if fallback and fallback.get("note"):
        try:
            parsed = json.loads(str(fallback["note"]))
            if isinstance(parsed, dict):
                note.update(parsed)
        except (ValueError, TypeError):
            pass
    if extras:
        note["comfyui"] = extras
    return json.dumps(note, ensure_ascii=False,
                      separators=(",", ":")) if note else ""


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(float(str(value).strip()))
    except (ValueError, OverflowError, TypeError):
        return None


def _as_float(value: Any, default: float | None = None) -> float | None:
    if value is None or isinstance(value, bool):
        return default
    try:
        return float(str(value).strip())
    except (ValueError, OverflowError, TypeError):
        return default


def _as_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""
