"""窗口几何与显示偏好的持久化。

**不 import Qt**（C36）。屏幕坐标由调用方以 (x, y, w, h) 的形式传进来，
所以「落在已拔掉的副屏上」这条规则也能离线单测。
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from chrometrans.config import DEFAULT_LANGUAGE, LANGUAGES

_INT_FIELDS = ("x", "y", "width", "height", "font_px")


@dataclass(frozen=True)
class GuiSettings:
    x: int | None = None
    y: int | None = None
    width: int = 900
    height: int = 220
    font_px: int = 22
    opacity: float = 0.9
    language: str = DEFAULT_LANGUAGE


def settings_path() -> Path:
    """%APPDATA%/chrometrans/gui.json；取不到 APPDATA 时退回用户主目录。"""
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "chrometrans" / "gui.json"


def _coerce(raw: dict) -> GuiSettings:
    """逐字段校验。坏字段单独退回该字段的默认值，不牵连其它字段。"""
    default = GuiSettings()
    out: dict = {}
    for name in _INT_FIELDS:
        value = raw.get(name)
        # bool 是 int 的子类，`font_px: true` 必须当成坏值
        ok = isinstance(value, int) and not isinstance(value, bool)
        out[name] = value if ok else getattr(default, name)
    value = raw.get("opacity")
    ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    out["opacity"] = float(value) if ok else default.opacity
    # 白名单校验，与 CLI 的 choices 同源（C38）。非字符串一并退默认 ——
    # 沿用这个函数「坏字段只连累自己」的既有策略。
    value = raw.get("language")
    out["language"] = (value if isinstance(value, str) and value in LANGUAGES
                       else default.language)
    return GuiSettings(**out)


def load(path: Path | None = None) -> GuiSettings:
    """读设置。**任何异常都退回默认值** —— 配置文件坏了不该让程序起不来。"""
    try:
        raw = json.loads((path or settings_path()).read_text(encoding="utf-8"))
    except Exception:
        return GuiSettings()
    if not isinstance(raw, dict):
        return GuiSettings()
    return _coerce(raw)


def save(s: GuiSettings, path: Path | None = None) -> None:
    """写设置。失败就算了 —— 界面照常用，只是下次记不住位置。"""
    path = path or settings_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(s), ensure_ascii=False, indent=2),
                        encoding="utf-8")
    except Exception:
        pass


def _centered(s: GuiSettings, screen: tuple[int, int, int, int]) -> GuiSettings:
    x, y, w, h = screen
    return replace(s, x=x + (w - s.width) // 2, y=y + (h - s.height) // 2)


def restore_position(s: GuiSettings,
                     screens: list[tuple[int, int, int, int]]) -> GuiSettings:
    """把窗口坐标搬回可见区域。

    用户拔掉副屏之后，保存的坐标会落在一条不存在的显示器上，窗口就此消失、
    再也找不回来。落在屏幕外（或压根没存过坐标）就居中到第一块屏幕。
    """
    if not screens:
        return replace(s, x=None, y=None)
    if s.x is None or s.y is None:
        return _centered(s, screens[0])
    for x, y, w, h in screens:
        if x <= s.x < x + w and y <= s.y < y + h:
            return s
    return _centered(s, screens[0])


def with_language(s: GuiSettings, language: str) -> GuiSettings:
    """把当前选中的语言并进要保存的设置。

    单独提出来是为了能离线测：`gui/app.py` 那边够不到，而「存了不读」与
    「读了不存」恰恰是最容易漏的两件事。
    """
    return replace(s, language=language)
