"""設定ファイルの読込・保存を共通化するモジュール。"""

from __future__ import annotations

import os
from typing import Any

import yaml

# 実行ディレクトリ基準の設定ファイル名
CONFIG_PATH = "config.yaml"


def load_config(path: str = CONFIG_PATH) -> dict[str, Any]:
    """YAML設定を読み込み、辞書として返す。"""
    # ファイルをUTF-8で開き、YAMLを辞書化する
    with open(path, "r", encoding="utf-8") as f:
        # 空ファイル等でNoneになった場合は空辞書にフォールバックする
        data = yaml.safe_load(f) or {}
    # 呼び出し側が変更しても元ファイルと独立するよう返す
    return data


def save_config(config: dict[str, Any], path: str = CONFIG_PATH) -> None:
    """設定辞書をYAMLとして保存する。"""
    # Unicodeをそのまま書き出し、キー順は保持する
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(
            config,
            f,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
        )


def ensure_data_dir(dirname: str = "data") -> str:
    """永続データ用ディレクトリを作成し、パスを返す。"""
    # 未作成なら作成する（exist_okで並行実行にも耐える）
    os.makedirs(dirname, exist_ok=True)
    # 呼び出し側が結合しやすいようディレクトリ名を返す
    return dirname


def id_set(values: list[Any] | None) -> set[str]:
    """IDリストを文字列集合に正規化する。"""
    # Noneや空はそのまま空集合にする
    if not values:
        return set()
    # Discord IDは文字列比較で統一する
    return {str(v) for v in values}
