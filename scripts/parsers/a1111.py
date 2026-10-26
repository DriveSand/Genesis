"""a1111.py —— A1111 WebUI infotext 家族解析器。

覆盖范围: 
- A1111 WebUI: PNG tEXt "parameters" / JPEG、WEBP 的 EXIF UserComment
- LibLibAI: PNG "parameters" + "AIGC" chunk (AIGC 为 Python dict repr, 非合法 JSON) 
- Civitai 在线生成: UserComment 扩展 infotext, 尾部带
  "Civitai resources" / "Civitai metadata" 两个 JSON 块 (旧图 metadata 为 {}) 

契约: TOOL_NAME / can_parse / parse, 见 scripts/parsers/__init__.py。

硬性约束: 本模块无文件 I/O、无 SQL、不 import scripts.database /
scripts.file_manager, 仅使用标准库与 scripts.logger。
"""
from __future__ import annotations

import ast
import json
import re
from typing import TYPE_CHECKING, Any

from scripts.logger import log

if TYPE_CHECKING:
    from scripts.parsers import RawMetadata

TOOL_NAME = "A1111 WebUI"

# ---- 正则与常量 ----
_STEPS_START_RE = re.compile(r"(?m)^[ \t]*Steps:\s")            # 参数块起点 (行首) 
_NEG_LINE_RE = re.compile(r"(?m)^[ \t]*Negative prompt:[ \t]?")  # 负提示词行首标记
_LORA_TAG_RE = re.compile(r"<(lora|lyco):((?:\\.|[^<>\\])+?):([^<>]*)>")
_KV_BOUNDARY_RE = re.compile(r",\s*(?=[A-Za-z][A-Za-z0-9 _\-]*:\s)")
_KV_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 _\-]*):\s*(.*)$", re.S)
_MODEL_EXT_RE = re.compile(r"\.(safetensors|ckpt|pt)$", re.I)
_VAE_SKIP = {"", "automatic", "none", "null"}
# 被正规消费的参数键, 不再重复进 note
_MAPPED_KEYS = {"Steps", "Sampler", "CFG scale", "Seed", "Size", "Model", "Scheduler"}


def can_parse(raw: "RawMetadata") -> bool:
    """判定是否属于 A1111 infotext 家族: 候选文本含行首 "Steps:" 特征。"""
    text = _candidate_infotext(raw)
    return text is not None and bool(_STEPS_START_RE.search(text))


def parse(raw: "RawMetadata") -> dict[str, Any]:
    """解析 A1111 infotext, 返回结构化元数据 (含 models 四键) 。"""
    text = (_candidate_infotext(raw) or "").replace("\r\n", "\n").replace("\r", "\n")

    # 1) 三段切分: 正向提示词 / 负向提示词 / 参数块
    positive, negative, params_text = _split_infotext(text)

    # 2) 先摘除两个 Civitai JSON 块 (值含逗号/引号, 必须先整体摘除) , 再切 KV
    resources_str, params_text = _extract_json_block(
        params_text, "Civitai resources", "[", "]")
    metadata_str, params_text = _extract_json_block(
        params_text, "Civitai metadata", "{", "}")
    pairs = _split_kv(params_text)

    resources = _loads_list(resources_str)
    civ_meta = _loads_dict(metadata_str)   # {} 视为无, 自动走扁平回退

    # 3) Civitai metadata 权威字段覆盖 (旧图 {} 时保持扁平结果) 
    if civ_meta:
        positive = str(civ_meta.get("prompt") or positive)
        negative = str(civ_meta.get("negativePrompt") or negative)
        scheduler = str(civ_meta.get("scheduler") or "")
    else:
        scheduler = ""
    # 个别变体扁平参数里带 Scheduler 键时, 扁平优先
    scheduler = pairs.get("Scheduler") or scheduler

    # 4) 数值/字符串正规字段
    seed = _to_int(pairs.get("Seed"), 0)
    steps = _to_int(pairs.get("Steps"), 0)
    cfg = _to_float(pairs.get("CFG scale"), 0.0)
    sampler = pairs.get("Sampler", "")

    # 5) 模型信息 (结构化返回, 本模块不碰数据库；url 一律留空) 
    checkpoints: list[dict[str, Any]] = []
    loras: list[dict[str, Any]] = []
    vaes: list[dict[str, Any]] = []

    # 5a) Civitai resources 优先
    for entry in resources or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("modelName") or "").strip()
        if not name:
            continue
        version = str(entry.get("modelVersionName") or "")
        kind = str(entry.get("type") or "").lower()
        if kind == "checkpoint":
            checkpoints.append({"name": name, "version": version, "url": None})
        elif kind in ("lora", "lyco", "lycoris"):
            loras.append({
                "name": name, "version": version, "url": None,
                "weight": _to_float(entry.get("weight"), 1.0),
                "model_type": 0 if kind == "lora" else 1,
            })
        elif kind == "vae":
            vaes.append({"name": name, "version": version, "url": None})

    # 5b) 扁平 Model 参数回退 (无 Civitai resources 的 checkpoint 时) 
    model_val = pairs.get("Model", "").strip()
    if model_val and not checkpoints:
        base = _MODEL_EXT_RE.sub("", model_val)
        # 按第一个 "_" 拆分 name/version (无下划线时整体作 name) 
        name, sep, version = base.partition("_")
        if name:
            checkpoints.append({
                "name": name,
                "version": version if sep else "",
                "url": None,
            })

    # 5c) <lora:>/<lyco:> 内联标签 (标签权重是实际生效权重, 覆盖 resources) 
    _merge_lora_tags(loras, positive + "\n" + negative)

    # 5d) vae_name 参数 (automatic/none 等占位值不建记录) 
    vae_name = pairs.get("vae_name", "").strip()
    if vae_name and vae_name.lower() not in _VAE_SKIP and not vaes:
        vaes.append({"name": vae_name, "version": "", "url": None})

    _dedupe_models(checkpoints, key=lambda e: e["name"])
    _dedupe_models(loras, key=lambda e: (e["name"], e["model_type"]))
    _dedupe_models(vaes, key=lambda e: e["name"])

    # 6) note: 正规字段之外的额外参数, 紧凑 JSON, 不丢弃
    extras = {k: v for k, v in pairs.items() if k not in _MAPPED_KEYS}
    note_obj: dict[str, Any] = {}
    if extras:
        note_obj["extra_params"] = extras
    aigc = _parse_aigc(raw.text.get("AIGC"))
    if aigc is not None:
        note_obj["aigc"] = aigc
    if resources:
        note_obj["civitai_resources"] = resources
    if civ_meta:
        residual = {k: v for k, v in civ_meta.items()
                    if k not in ("prompt", "negativePrompt")}
        if residual:
            note_obj["civitai_metadata"] = residual
    note = json.dumps(note_obj, ensure_ascii=False,
                      separators=(",", ":")) if note_obj else ""

    return {
        "positive_prompt": positive,
        "negative_prompt": negative,
        "seed": seed,
        "steps": steps,
        "cfg": cfg,
        "sampler": sampler,
        "scheduler": scheduler,
        "note": note,
        "models": {
            "checkpoints": checkpoints,
            "loras": loras,
            "embeddings": [],   # infotext 无法区分 embedding, 不猜测
            "vaes": vaes,
            "upscales": [],
        },
    }


# ---- 内部辅助 ----

def _candidate_infotext(raw: "RawMetadata") -> str | None:
    """按容器类型取候选 infotext 文本。"""
    if raw.format == "PNG":
        return raw.text.get("parameters")
    return raw.user_comment  # JPEG / WEBP


def _split_infotext(text: str) -> tuple[str, str, str]:
    """三段切分: (正向提示词, 负向提示词, 参数块文本)。

    参数块起点 = 第一处行首 "Steps:"；无参数行时整体保守视为正向提示词；
    无提示词 img2img 变体 (文本直接以 Steps: 开头) 返回两个空提示词。
    """
    m_steps = _STEPS_START_RE.search(text)
    if m_steps is None:
        return text.strip(), "", ""
    head, params_block = text[:m_steps.start()], text[m_steps.start():]
    # 压平参数块折行续行 (仅去行首尾空白；不得折叠连续空白, 
    # 否则会破坏 Civitai JSON 块内字符串值的原始内容) 
    params_text = " ".join(line.strip() for line in params_block.splitlines())
    params_text = params_text.strip()

    m_neg = _NEG_LINE_RE.search(head)
    if m_neg:
        positive = head[:m_neg.start()].strip()
        negative = head[m_neg.end():].strip()
    else:
        positive, negative = head.strip(), ""
    return positive, negative, params_text


def _extract_json_block(text: str, key: str,
                        open_ch: str, close_ch: str) -> tuple[str | None, str]:
    """提取 "Key: " 后的平衡括号块 (字符串内的括号/转义不计深度) 。

    返回 (JSON 字符串或 None, 摘除该块后的参数文本)。
    """
    m = re.search(re.escape(key) + r":\s*", text)
    if not m:
        return None, text
    start = m.end()
    if start >= len(text) or text[start] != open_ch:
        return None, text
    depth, in_str, esc, i = 0, False, False, start
    while i < len(text):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == open_ch:
                depth += 1
            elif c == close_ch:
                depth -= 1
                if depth == 0:
                    seg = text[start:i + 1]
                    rest = (text[:m.start()] + text[i + 1:]).strip(" ,\n")
                    return seg, rest
        i += 1
    return None, text   # 括号不平衡: 视为无此块, 原文保留


def _split_kv(params_text: str) -> dict[str, str]:
    """参数块 KV 切分: 按 ",<空白><键形>: " 前瞻边界切分, 
    非 KV 段并入上一键的值 (不丢数据) 。"""
    pairs: dict[str, str] = {}
    last_key: str | None = None
    for seg in _KV_BOUNDARY_RE.split(params_text):
        m = _KV_RE.match(seg)
        if m:
            last_key = m.group(1).strip()
            pairs[last_key] = m.group(2).strip()
        elif last_key is not None and seg.strip():
            pairs[last_key] += " " + seg.strip()
    return pairs


def _merge_lora_tags(loras: list[dict[str, Any]], prompt_text: str) -> None:
    """提取 <lora:NAME:WEIGHT> / <lyco:...> 内联标签, 并入 loras。

    按 (name, model_type) 合并: 已存在则标签权重覆盖 (实际生效权重) , 
    否则新增 (version 未知留空) 。
    """
    for m in _LORA_TAG_RE.finditer(prompt_text or ""):
        kind = m.group(1)
        name = re.sub(r"\\(.)", r"\1", m.group(2)).strip()
        if not name:
            continue
        model_type = 0 if kind == "lora" else 1
        weight = _to_float(m.group(3), 1.0)
        for e in loras:
            if e["name"] == name and e["model_type"] == model_type:
                e["weight"] = weight
                break
        else:
            loras.append({"name": name, "version": "", "url": None,
                          "weight": weight, "model_type": model_type})


def _parse_aigc(s: str | None) -> dict | None:
    """解析 PNG AIGC chunk (Python dict repr, 单引号, 非合法 JSON) 。"""
    if not s:
        return None
    try:
        v = ast.literal_eval(s)
    except (ValueError, SyntaxError):
        try:
            v = json.loads(s)   # 兜底: 写端日后可能改为合法 JSON
        except ValueError:
            log.debug(f"AIGC chunk parse failed, ignored: {s[:80]!r}")
            return None
    return v if isinstance(v, dict) else None


def _dedupe_models(rows: list[dict[str, Any]], key) -> None:
    """按键去重, 保留首个 (防模型绑定出现同名重复项) 。"""
    seen: set = set()
    i = 0
    while i < len(rows):
        k = key(rows[i])
        if k in seen:
            del rows[i]
        else:
            seen.add(k)
            i += 1


def _loads_list(s: str | None) -> list | None:
    if not s:
        return None
    try:
        v = json.loads(s)
    except ValueError:
        log.debug(f"Civitai JSON chunk parse failed, ignored: {s[:80]!r}")
        return None
    return v if isinstance(v, list) else None


def _loads_dict(s: str | None) -> dict | None:
    if not s:
        return None
    try:
        v = json.loads(s)
    except ValueError:
        log.debug(f"Civitai JSON chunk parse failed, ignored: {s[:80]!r}")
        return None
    return v if isinstance(v, dict) else None


def _to_int(s: Any, default: int) -> int:
    if s is None:
        return default
    try:
        return int(float(str(s).strip()))
    except (ValueError, OverflowError):
        return default


def _to_float(s: Any, default: float) -> float:
    if s is None:
        return default
    try:
        return float(str(s).strip())
    except (ValueError, OverflowError):
        return default
