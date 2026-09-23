import json


def test_round_trip_preserves_every_field(tmp_path):
    from chrometrans.gui.settings import GuiSettings, load, save

    p = tmp_path / "gui.json"
    original = GuiSettings(x=10, y=20, width=640, height=180,
                           font_px=30, opacity=0.5)
    save(original, p)

    assert load(p) == original


def test_save_creates_missing_parent_directories(tmp_path):
    from chrometrans.gui.settings import GuiSettings, load, save

    p = tmp_path / "nested" / "deeper" / "gui.json"
    save(GuiSettings(width=700), p)

    assert load(p).width == 700


def test_missing_file_yields_defaults(tmp_path):
    from chrometrans.gui.settings import GuiSettings, load

    assert load(tmp_path / "nope.json") == GuiSettings()


def test_corrupt_file_yields_defaults(tmp_path):
    """配置文件坏了不该让程序起不来。"""
    from chrometrans.gui.settings import GuiSettings, load

    p = tmp_path / "gui.json"
    p.write_text("{ this is not json", encoding="utf-8")

    assert load(p) == GuiSettings()


def test_json_that_is_not_an_object_yields_defaults(tmp_path):
    from chrometrans.gui.settings import GuiSettings, load

    p = tmp_path / "gui.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")

    assert load(p) == GuiSettings()


def test_wrong_field_types_fall_back_per_field_and_keep_the_good_ones(tmp_path):
    """一个字段坏掉不该把整份设置拖回默认值。"""
    from chrometrans.gui.settings import GuiSettings, load

    p = tmp_path / "gui.json"
    p.write_text(json.dumps({"width": "很宽", "height": 300,
                             "font_px": True, "opacity": "半透明"}),
                 encoding="utf-8")

    got = load(p)
    assert got.width == GuiSettings().width, "坏字段退回默认值"
    assert got.height == 300, "好字段要留下来"
    assert got.font_px == GuiSettings().font_px, "bool 不是合法的字号"
    assert got.opacity == GuiSettings().opacity


def test_unknown_keys_are_ignored(tmp_path):
    from chrometrans.gui.settings import GuiSettings, load

    p = tmp_path / "gui.json"
    p.write_text(json.dumps({"width": 800, "未来的字段": 1}), encoding="utf-8")

    assert load(p).width == 800


def test_save_failure_is_swallowed(tmp_path):
    """目录只读时保存失败就算了：界面照常用，只是下次记不住位置。"""
    from chrometrans.gui.settings import GuiSettings, save

    blocker = tmp_path / "blocker"
    blocker.write_text("我是个文件，不是目录", encoding="utf-8")

    save(GuiSettings(width=800), blocker / "gui.json")   # 不应抛异常


def test_position_inside_a_screen_is_kept_as_is():
    from chrometrans.gui.settings import GuiSettings, restore_position

    s = GuiSettings(x=100, y=100, width=400, height=200)
    assert restore_position(s, [(0, 0, 1920, 1080)]) == s


def test_position_on_a_secondary_screen_is_kept():
    from chrometrans.gui.settings import GuiSettings, restore_position

    s = GuiSettings(x=2000, y=100, width=400, height=200)
    screens = [(0, 0, 1920, 1080), (1920, 0, 1920, 1080)]

    assert restore_position(s, screens) == s


def test_position_on_an_unplugged_screen_is_centered_on_the_first_one():
    """拔掉副屏之后，保存的坐标会落在不存在的显示器上 —— 窗口就此消失。"""
    from chrometrans.gui.settings import GuiSettings, restore_position

    s = GuiSettings(x=3000, y=100, width=400, height=200)
    got = restore_position(s, [(0, 0, 1920, 1080)])

    assert (got.x, got.y) == ((1920 - 400) // 2, (1080 - 200) // 2)


def test_missing_position_is_centered_on_the_first_screen():
    from chrometrans.gui.settings import GuiSettings, restore_position

    got = restore_position(GuiSettings(width=400, height=200),
                           [(0, 0, 1920, 1080)])

    assert (got.x, got.y) == (760, 440)


def test_no_screens_at_all_leaves_the_position_unset():
    """没有可用屏幕信息时不要瞎编一个坐标。"""
    from chrometrans.gui.settings import GuiSettings, restore_position

    got = restore_position(GuiSettings(x=1, y=2), [])

    assert got.x is None and got.y is None
