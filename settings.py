"""配置、目录和模型选择。

该模块只负责运行配置，不包含日记、模型请求或分析业务。
"""

import sys
from pathlib import Path
from typing import Any

import yaml


ModelDict = dict[str, Any]


def _get_config_path() -> Path:
    """获取 config.yaml 路径，兼容 PyInstaller 打包后的路径。"""
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
    else:
        base = Path(__file__).parent
    return base / "config.yaml"


def _load_config() -> dict:
    config_path = _get_config_path()
    if not config_path.exists():
        return {}
    with config_path.open("r", encoding="utf-8") as file:
        return yaml.safe_load(file) or {}


CONFIG = _load_config()
DIARY_DIR = Path(CONFIG.get("diary_dir", "./AgentRecords"))
ANALYSIS_DIR = Path(CONFIG.get("analysis_dir", "./AnalysisReports"))
DIARY_DIR.mkdir(parents=True, exist_ok=True)


class ModelConfig:
    """统一管理 OpenAI 兼容模型配置。"""

    @classmethod
    def models(cls) -> list[ModelDict]:
        return CONFIG.get("models", [])

    @classmethod
    def get_model(cls, name_or_index: str | int | None = None) -> ModelDict:
        models = cls.models()
        if not models:
            raise RuntimeError("config.yaml 中未配置任何模型")
        if name_or_index is None:
            return models[0]
        if isinstance(name_or_index, int):
            return models[name_or_index % len(models)]

        name_lower = name_or_index.lower()
        for model in models:
            if model["name"].lower() == name_lower:
                return model
        for model in models:
            if name_lower in model["name"].lower():
                return model
        raise KeyError(f"未找到匹配模型 '{name_or_index}'")

    @classmethod
    def index_of(cls, name: str) -> int:
        for index, model in enumerate(cls.models()):
            if model["name"] == name:
                return index
        return 0

    @classmethod
    def next_after(cls, name: str) -> ModelDict:
        models = cls.models()
        index = cls.index_of(name)
        return models[(index + 1) % len(models)]
