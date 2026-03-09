#!/usr/bin/env python3
"""
自动更新七牛云 AI 模型列表脚本

功能：
1. 从七牛云市场 API 获取最新的模型列表
2. 自动生成/更新模型的 YAML 配置文件
3. 更新 _position.yaml 文件中的模型顺序
4. 只处理支持 OpenAI 协议的文本 LLM 模型

数据源：
- API: https://openai.qiniu.com/v1/market/models
- 返回: JSON 格式，包含 status 和 data 字段

数据过滤规则：
1. 协议过滤：必须支持 openai 协议 (support_api_protocols 包含 "openai")
2. 模态过滤：必须是文本 LLM (architecture.input_modalities 和 output_modalities 都包含 "text")

核心字段映射：
┌─────────────────────────────────────────────────────────────────────┐
│ API 数据结构                → Dify YAML 配置                         │
├─────────────────────────────────────────────────────────────────────┤
│ id                          → model (模型 ID)                       │
│ name                        → label.zh_Hans / label.en_US (双语名称) │
│ input_modalities            → features (特性列表)                    │
│   ├─ 默认                   →   ["tool-call", "stream-tool-call"]    │
│   └─ 含 image               →   额外添加 "vision"                    │
│ model_constraints           → model_properties                      │
│   └─ context_length         →   context_size (默认 65536, 64k)      │
│ 默认参数                     → parameter_rules                       │
│   ├─ temperature            →   使用模板 "temperature"              │
│   ├─ top_p                  →   使用模板 "top_p"                    │
│   └─ max_tokens             →   使用模板 "max_tokens"               │
└─────────────────────────────────────────────────────────────────────┘

文件名转换规则：
- 模型 ID 可能包含特殊字符（如 /、:、空格等）
- 转换规则：将特殊字符替换为连字符 (-)
- 示例：
  deepseek/deepseek-v3.2-speciale → deepseek-deepseek-v3.2-speciale.yaml
  google/gemini-2.5-flash        → google-gemini-2.5-flash.yaml

特性默认值：
- 始终输出 ["tool-call", "stream-tool-call"]
- 如果输入模态包含 image，额外添加 "vision"

文件管理策略：
- 新增：创建新的 YAML 文件
- 更新：覆盖现有 YAML 文件（保持最新数据）
- 删除：移除不在 API 列表中的模型文件（全量更新）
- _position.yaml：按模型创建时间倒序排列（新模型在前）
  如果 API 提供 created_at 字段，按该字段排序
  否则保持 API 返回的原始顺序

CI 环境行为：
- 检测环境变量 CI=true 判断是否在 CI 环境中运行
- 如果模型缺少 context_length 字段，记录错误
- 在更新完成后，如果发现有模型缺少必需字段，退出并返回错误码 1
- 本地开发环境会使用默认值继续处理
"""

import sys
import os
import argparse
import yaml
import requests
from pathlib import Path
from typing import List, Dict, Any, Optional

# 七牛云市场 API 端点
MARKET_API_URL = "https://openai.sufy.com/v1/market/models?overseas=true"

# 模型配置目录
SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent
AI_MODELS_DIR = PROJECT_ROOT / "ai-models-provider"
MODELS_DIR = AI_MODELS_DIR / "models" / "llm"
MANIFEST_FILE = AI_MODELS_DIR / "manifest.yaml"
POSITION_FILENAME = "_position.yaml"
POSITION_FILE = MODELS_DIR / POSITION_FILENAME
AI_MODELS_PLUGIN_NAME = "ai-models-provider"
VISION_MODALITIES = {"image"}
DEFAULT_CONTEXT_LENGTH = 65_536
DEFAULT_FEATURES = ["tool-call", "stream-tool-call"]
PARAMETER_RULE_TEMPLATES = [
    {
        "name": "temperature",
        "use_template": "temperature",
    },
    {
        "name": "top_p",
        "use_template": "top_p",
    },
    {
        "name": "max_tokens",
        "use_template": "max_tokens",
    },
]

ModelInfo = Dict[str, Any]

# CI 环境检测
IS_CI = os.getenv("CI", "").lower() in ("true", "1", "yes")

# 记录缺失字段的模型（用于 CI 环境）
models_with_missing_fields = []


def has_vision_capability(input_modalities: List[str]) -> bool:
    """Return True when the model supports any non-text modality we treat as vision."""
    return any(modality in VISION_MODALITIES for modality in (input_modalities or []))


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="七牛云 AI 模型列表自动更新脚本")
    parser.add_argument(
        "--release-meta",
        action="store_true",
        help="输出发布元数据（version 和 tag_name），用于 CI 工作流。",
    )
    parser.add_argument(
        "--plugin-name",
        default=AI_MODELS_PLUGIN_NAME,
        help=f"生成 release tag 时使用的插件名，默认: {AI_MODELS_PLUGIN_NAME}",
    )
    return parser.parse_args(argv)


def get_manifest_version() -> str:
    """读取 manifest.yaml 中的插件版本。"""
    with open(MANIFEST_FILE, "r", encoding="utf-8") as f:
        manifest = yaml.safe_load(f) or {}

    version = str(manifest.get("version", "")).strip()
    if not version:
        raise ValueError(f"manifest.yaml 中缺少有效的 version 字段: {MANIFEST_FILE}")

    return version


def build_release_tag(version: str, plugin_name: str = AI_MODELS_PLUGIN_NAME) -> str:
    """根据插件名和版本号构建 release tag。"""
    return f"{plugin_name}-v{version}"


def output_release_meta(plugin_name: str = AI_MODELS_PLUGIN_NAME) -> None:
    """输出发布元数据，供 GitHub Actions 写入 GITHUB_OUTPUT。"""
    version = get_manifest_version()
    tag_name = build_release_tag(version, plugin_name)
    print(f"version={version}")
    print(f"tag_name={tag_name}")


def is_llm_model(model_info: ModelInfo) -> bool:
    """
    判断是否为文本 LLM 模型
    
    Args:
        model_info: 模型信息字典
    
    Returns:
        是否为 LLM 模型
    """
    architecture = model_info.get("architecture", {})
    input_modalities = architecture.get("input_modalities", [])
    output_modalities = architecture.get("output_modalities", [])
    
    # 必须同时支持文本输入和文本输出
    return "text" in input_modalities and "text" in output_modalities


def get_model_features(model_info: ModelInfo) -> List[str]:
    """
    根据模型信息获取支持的特性
    
    Args:
        model_info: 模型信息字典
    
    Returns:
        特性列表
    """
    # 默认认为支持工具调用能力
    features = list(DEFAULT_FEATURES)
    
    architecture = model_info.get("architecture", {})
    input_modalities = architecture.get("input_modalities", []) or []
    if has_vision_capability(input_modalities) and "vision" not in features:
        features.append("vision")
    
    return features


def get_model_context_size(model_info: ModelInfo) -> int:
    """
    获取模型上下文大小
    
    Args:
        model_info: 模型信息字典
    
    Returns:
        上下文大小
    """
    model_constraints = model_info.get("model_constraints", {})
    context_length = model_constraints.get("context_length", 0)
    
    # 如果有有效的上下文长度，使用它
    if context_length > 0:
        return context_length
    
    # 否则使用默认值并输出警告
    model_id = model_info.get("id", "unknown")
    error_msg = f"模型 {model_id} 缺少 context_length 字段"
    print(f"  ⚠ 警告: {error_msg}，使用默认值 65536")
    
    # 在 CI 环境中记录错误
    if IS_CI:
        models_with_missing_fields.append(error_msg)
    
    return DEFAULT_CONTEXT_LENGTH


def generate_model_yaml(model_info: ModelInfo) -> Dict[str, Any]:
    """
    生成模型的 YAML 配置
    
    Args:
        model_info: 模型信息字典
    
    Returns:
        YAML 配置字典
    """
    model_id = model_info.get("id", "")
    model_name = model_info.get("name", model_id)
    
    # 基础配置
    config = {
        "model": model_id,
        "label": {
            "zh_Hans": model_name,
            "en_US": model_name
        },
        "model_type": "llm",
        "features": get_model_features(model_info),
        "model_properties": {
            "mode": "chat",
            "context_size": get_model_context_size(model_info),
        },
        "parameter_rules": [template.copy() for template in PARAMETER_RULE_TEMPLATES],
    }
    
    return config


def fetch_models_from_api() -> List[ModelInfo]:
    """
    从七牛云市场 API 获取模型列表
    
    Returns:
        符合条件的模型列表
    """
    try:
        print(f"正在从市场 API 获取模型列表: {MARKET_API_URL}")
        response = requests.get(MARKET_API_URL, timeout=30)
        response.raise_for_status()
        
        data = response.json()
        
        # 检查响应状态
        if not data.get("status"):
            print(f"✗ API 返回失败状态: {data}")
            return []
        
        raw_models = data.get("data", [])
        print(f"  从 API 获取到 {len(raw_models)} 个原始模型")
        
        filtered_models = []
        skipped_count = {
            "no_openai": 0,
            "not_llm": 0,
        }
        
        for model_info in raw_models:
            model_id = model_info.get("id", "")
            if not model_id:
                continue
            
            # 过滤条件 1: 必须支持 OpenAI 协议
            protocols = model_info.get("support_api_protocols", [])
            if "openai" not in protocols:
                skipped_count["no_openai"] += 1
                print(f"  ⊗ 跳过 {model_id}: 不支持 OpenAI 协议")
                continue
            
            # 过滤条件 2: 必须是文本 LLM 模型
            if not is_llm_model(model_info):
                skipped_count["not_llm"] += 1
                architecture = model_info.get("architecture", {})
                print(f"  ⊗ 跳过 {model_id}: 非文本 LLM 模型 (输入:{architecture.get('input_modalities')}, 输出:{architecture.get('output_modalities')})")
                continue
            
            # 通过所有过滤条件
            print(f"  ✓ 保留 {model_id}")
            filtered_models.append(model_info)
        
        print()
        print(f"✓ 成功获取 {len(filtered_models)} 个符合条件的模型")
        print(f"  跳过 {skipped_count['no_openai']} 个不支持 OpenAI 协议的模型")
        print(f"  跳过 {skipped_count['not_llm']} 个非文本 LLM 模型")
        
        return filtered_models
    
    except requests.RequestException as e:
        print(f"✗ 网络请求失败: {e}")
        return []
    except Exception as e:
        print(f"✗ 处理 API 响应失败: {e}")
        import traceback
        traceback.print_exc()
        return []


def sanitize_filename(model_id: str) -> str:
    """
    将模型 ID 转换为合法的文件名
    
    Args:
        model_id: 模型 ID，可能包含特殊字符如 /
    
    Returns:
        合法的文件名（不包含扩展名）
    
    Examples:
        deepseek/deepseek-v3.2-speciale -> deepseek-deepseek-v3.2-speciale
        google/gemini-2.5-flash -> google-gemini-2.5-flash
    """
    # 将特殊字符替换为连字符
    filename = model_id.replace("/", "-").replace(":", "-").replace(" ", "-").replace("@", "-")
    # 移除连续的连字符
    while "--" in filename:
        filename = filename.replace("--", "-")
    # 移除首尾的连字符
    filename = filename.strip("-")
    return filename


def get_existing_model_files() -> Dict[str, Path]:
    """Return a mapping of sanitized model IDs to their YAML file paths."""
    return {
        file.stem: file
        for file in MODELS_DIR.glob("*.yaml")
        if file.name != POSITION_FILENAME
    }

def update_model_files(models: List[ModelInfo]) -> tuple[List[str], List[str], List[str]]:
    """
    更新模型配置文件
    
    Args:
        models: 模型信息列表
    
    Returns:
        (新增的模型 ID 列表, 更新的模型 ID 列表, 删除的模型文件名列表)
    """
    existing_models = get_existing_model_files()
    new_model_filenames = set()
    added: List[str] = []
    updated: List[str] = []
    removed: List[str] = []
    
    # 新增或更新模型
    for model_info in models:
        model_id = model_info["id"]
        model_name = model_info.get("name", model_id)
        filename = sanitize_filename(model_id)
        file_path = MODELS_DIR / f"{filename}.yaml"
        new_model_filenames.add(filename)
        
        config = generate_model_yaml(model_info)
        
        if filename not in existing_models:
            print(f"  + 新增: {model_id}")
            if model_name != model_id:
                print(f"         {model_name}")
            added.append(model_id)
        else:
            print(f"  ↻ 更新: {model_id}")
            updated.append(model_id)
        
        with open(file_path, "w", encoding="utf-8") as f:
            yaml.dump(config, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
    
    # 删除不在新列表中的模型（全量更新）
    for filename, file_path in existing_models.items():
        if filename not in new_model_filenames:
            print(f"  - 删除: {filename}")
            file_path.unlink()
            removed.append(filename)
    
    return added, updated, removed


def update_position_file(models: List[ModelInfo]):
    """
    更新 _position.yaml 文件
    
    策略：
    按照模型创建时间倒序排列（新模型在前）
    如果 API 提供了 created_at 字段，使用该字段排序
    否则使用 API 返回的原始顺序（假设已按时间排序）
    
    Args:
        models: 模型信息列表
    """
    # 尝试按创建时间排序
    # 检查是否有 created_at 或类似的时间字段
    has_timestamp = any(m.get("created_at") or m.get("created") or m.get("creation_time") for m in models)
    
    if has_timestamp:
        # 按时间戳倒序排序（新模型在前）
        sorted_models = sorted(
            models,
            key=lambda m: m.get("created_at") or m.get("created") or m.get("creation_time") or 0,
            reverse=True
        )
    else:
        # 使用 API 返回的原始顺序（假设 API 已按时间排序，或保持原顺序）
        sorted_models = models
    
    # 转换为文件名列表
    ordered_models = [sanitize_filename(m["id"]) for m in sorted_models]
    
    # 写入文件
    with open(POSITION_FILE, "w", encoding="utf-8") as f:
        yaml.dump(ordered_models, f, allow_unicode=True, default_flow_style=False)
    
    print(f"✓ 已更新 _position.yaml，共 {len(ordered_models)} 个模型")


def summarize_changes(added: List[str], updated: List[str], removed: List[str], total: int) -> None:
    print()
    print("=" * 70)
    print("更新完成！")
    print(f"  新增模型: {len(added)} 个")
    print(f"  更新模型: {len(updated)} 个")
    print(f"  删除模型: {len(removed)} 个")
    print(f"  总计模型: {total} 个")
    print("=" * 70)


def enforce_ci_requirements() -> None:
    if IS_CI and models_with_missing_fields:
        print()
        print("=" * 70)
        print("✗ 错误：在 CI 环境中检测到模型缺少必需字段")
        print("=" * 70)
        for error in models_with_missing_fields:
            print(f"  ✗ {error}")
        print()
        print(f"共 {len(models_with_missing_fields)} 个模型缺少必需字段")
        print("请联系 API 提供方修复数据完整性问题")
        print("=" * 70)
        sys.exit(1)


def exit_with_change_status(added: List[str], updated: List[str], removed: List[str]) -> None:
    if added or updated or removed:
        sys.exit(0)
    print()
    print("提示：没有检测到模型变更")
    sys.exit(1)


def main(argv: Optional[List[str]] = None):
    """主函数"""
    args = parse_args(argv)

    if args.release_meta:
        output_release_meta(args.plugin_name)
        return

    print("=" * 70)
    print("七牛云 AI 模型列表自动更新脚本")
    print(f"数据源: {MARKET_API_URL}")
    print("=" * 70)
    print()
    
    # 从 API 获取模型列表
    models = fetch_models_from_api()
    
    if not models:
        print()
        print("✗ 未能从 API 获取模型列表")
        print("提示：请检查网络连接或 API 是否可用")
        sys.exit(1)
    
    print()
    print("开始更新模型配置文件...")
    print("-" * 70)
    
    # 更新模型文件
    added, updated, removed = update_model_files(models)
    
    print("-" * 70)
    print()
    
    # 更新 position 文件
    update_position_file(models)
    summarize_changes(added, updated, removed, len(models))
    enforce_ci_requirements()
    exit_with_change_status(added, updated, removed)


if __name__ == "__main__":
    main()
