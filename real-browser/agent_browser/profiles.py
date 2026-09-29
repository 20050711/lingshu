"""站点档案：站点知识是数据，不是代码。

内置 default；用户可在 ``<home>/profiles/<name>.json`` 覆盖或新增站点档案。
档案里只放**有依据的参数**（节奏、敏感元素、列表选择器、登录判据）——不放指纹、不放 UA。
"""
from __future__ import annotations

import json
from importlib import resources

from . import config

BUILTIN = ("default",)


def available() -> list[str]:
    return list(BUILTIN)


def profile_for(site: str) -> dict:
    override = config.home_dir() / "profiles" / f"{site}.json"
    if override.is_file():
        return json.loads(override.read_text("utf-8"))
    name = site if site in BUILTIN else "default"
    data = resources.files("agent_browser").joinpath("profiles", f"{name}.json")
    return json.loads(data.read_text("utf-8"))
