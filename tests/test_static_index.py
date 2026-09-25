"""index.html 的源码级断言。

**这些断言很弱**，它们只证明「那段代码还在文件里」，不证明它跑得对。留着它们是
因为 index.html 此前零覆盖，而这一轮要往里加两个分支（单语、丢弃提示）——
为三个 JS 分支引入 playwright 不划算，但让它们可以被无声删掉更不划算。

判断依据写在这里，免得日后有人误以为它比实际更强。
"""
from pathlib import Path

import pytest

INDEX = (Path(__file__).resolve().parents[1]
         / "src" / "chrometrans" / "static" / "index.html")


@pytest.fixture(scope="module")
def source() -> str:
    return INDEX.read_text(encoding="utf-8")


def test_monolingual_branch_exists(source):
    """C44：单语会话（tgt_lang 为 null）只出原文，不出「（未翻译）」。

    断言的是**严格相等那一行**，不是「源码里出现过 tgt_lang」—— 后者在
    `cue.tgt_lang == null` 这种宽松比较下也照样通过，而宽松比较在 JS 里会把
    `undefined`（键缺失）也算成单语，正好是我们要避免的兜底方向。
    """
    assert "cueIsMonolingual" in source
    assert "cue.tgt_lang === null" in source


def test_failed_translation_still_has_its_placeholder(source):
    """双语会话里翻译真挂了，那条提示必须还在。"""
    assert "（未翻译）" in source


def test_dropped_notices_have_a_branch(source):
    """C45：丢弃提示要在页面上有落点。"""
    assert "addNotice" in source


def test_dropped_audio_goes_through_the_notice_branch(source):
    """排空缓冲溢出丢掉的音频也要走提示分支。

    它是「刚发生的一件事」而不是「当前状态」：塞进页眉会被下一条 status 顶走，
    而这条信息的全部意义就是让人看见。
    """
    assert "'audio_dropped'" in source
    assert "'dropped'" in source or '"dropped"' in source


def test_the_bilingual_branch_still_renders_both_lines(source):
    """双语路径没被误删：缺译时分支持有的那个类名还在。"""
    assert "tgt missing" in source


def test_the_running_line_reports_the_session_mode(source):
    """C44：单语会话下「不翻译」要在页面上说出来，与 cli / GUI 三处一致（C43）。"""
    assert "翻译中" in source
    assert "不翻译" in source
    assert "data.bilingual" in source


def test_the_page_is_still_a_single_self_contained_file(source):
    """网页是只读显示，不开新资源依赖 —— 一个 <script src> 就能让它在离线时白屏。"""
    assert "<script src=" not in source
