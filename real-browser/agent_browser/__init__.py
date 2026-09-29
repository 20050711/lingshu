"""real-browser：真机真浏览器（系统 Edge/Chrome）的 agent 驱动层。

定位（见 `agent驱动浏览器方案-2026-09-24.md`）：
- **只藏自动化，不伪造身份**——不设 UA、不碰指纹、不开调试端口；
- **护栏挂在工具上**，agent 绕不过；
- 给 agent 的是一套"看一眼 → 动一下 → 再看一眼"的原语，不是流程脚本。
"""
from __future__ import annotations

__version__ = "0.1.0"

from .browser_agent import BrowserAgent  # noqa: E402

__all__ = ["BrowserAgent", "__version__"]
