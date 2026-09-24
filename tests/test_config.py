from chrometrans.config import load_config


def test_defaults_match_spec():
    cfg = load_config()
    assert cfg.capture.target_sample_rate == 16000
    assert cfg.segmenter.frame_samples == 512          # C7
    assert cfg.segmenter.max_segment_s == 15.0         # C6
    assert cfg.asr.language == "en"
    assert cfg.translate.src == "en"
    assert cfg.translate.tgt == "zh-Hans"


def test_normalization_has_a_silence_floor():
    """C27：归一化必须带静音门限，否则会放大底噪诱发幻觉。"""
    cfg = load_config()
    assert cfg.segmenter.norm_floor_rms > 0


import pytest

from chrometrans.config import DEFAULT_LANGUAGE, LANGUAGES, load_config


def test_every_profile_is_fully_derived_from_itself():
    """C39 的锁：源语言两侧必须来自同一份 profile。

    asr.language 与 translate.src 不同步的后果是用英语模型听俄语音频 ——
    Whisper 不报错，它会输出一段通顺但完全编造的英文。
    """
    for code, profile in LANGUAGES.items():
        cfg = load_config(code)
        assert cfg.asr.language == profile.asr_language
        assert cfg.translate.src == profile.translate_src
        assert cfg.asr.initial_prompt == profile.initial_prompt
        assert cfg.asr.no_speech_prob_threshold == profile.no_speech_prob_threshold
        assert cfg.asr.avg_logprob_threshold == profile.avg_logprob_threshold
        assert (cfg.asr.compression_ratio_threshold
                == profile.compression_ratio_threshold)


def test_every_profile_has_calibration_evidence():
    """C40：每门语言的阈值都要有人为它负责。加语言忘标定 = 这条红。"""
    for code, profile in LANGUAGES.items():
        assert profile.calibrated_on.strip(), f"{code} 没有标定依据"
        for name in ("no_speech_prob_threshold", "avg_logprob_threshold",
                     "compression_ratio_threshold"):
            assert isinstance(getattr(profile, name), float), f"{code}.{name}"
        assert profile.code == code
        assert profile.label.strip()


def test_unknown_language_is_rejected_not_defaulted():
    """C38：未知值不得退回默认语言 —— 那等于静默用了另一门语言的阈值。"""
    with pytest.raises(ValueError) as exc:
        load_config("klingon")

    message = str(exc.value)
    assert "klingon" in message
    assert "en" in message, "要列出可用值"


def test_default_language_is_english():
    assert DEFAULT_LANGUAGE == "en"
    assert load_config().asr.language == "en"


from chrometrans.config import LanguageProfile  # noqa: E402（补测需要，保持上文原样）


def test_invariant_holds_for_a_non_english_profile(monkeypatch):
    """C39 的非空洞锁（brief 之外补的，理由见下）。

    LANGUAGES 目前只有 en，而 en 的每个字段值都恰好等于 AsrConfig/TranslateConfig
    的默认值 —— 于是上面那条按 profile 迭代的测试，把 load_config 里任何一处硬编码
    成 "en" / 0.6 / -1.0 / 2.4 都抓不到（实测：把 language=profile.asr_language 换成
    language="en"，它照样全绿）。Task 4 加进 ru/zh 之后那条测试才真正生效。

    这里塞一份合成 profile 进白名单（monkeypatch 用完即撤，不改变 en-only 的封闭
    语言集），用彼此不同、且都偏离默认值的字段，把"两侧同源"这条不变量现在就钉住：
    asr_language 与 translate_src 故意取不同的字符串（zh 上它们分叉的真实形状）。
    """
    synthetic = LanguageProfile(
        code="zz",
        label="合成语言",
        asr_language="zz",            # != translate_src，专治"三个 code 合并成一个字段"
        translate_src="zz-Hans",
        initial_prompt="一个只属于 zz 的提示词",
        no_speech_prob_threshold=0.11,
        avg_logprob_threshold=-0.22,
        compression_ratio_threshold=3.33,
        calibrated_on="合成值：只为验证联动，不用于任何真实语言",
    )
    monkeypatch.setitem(LANGUAGES, "zz", synthetic)

    cfg = load_config("zz")

    assert cfg.asr.language == "zz"
    assert cfg.translate.src == "zz-Hans"
    assert cfg.asr.initial_prompt == "一个只属于 zz 的提示词"
    assert cfg.asr.no_speech_prob_threshold == 0.11
    assert cfg.asr.avg_logprob_threshold == -0.22
    assert cfg.asr.compression_ratio_threshold == 3.33
    # 未标定的字段不该被 profile 顺手改掉：tgt 仍是默认目标语言
    assert cfg.translate.tgt == "zh-Hans"


def test_is_bilingual_derives_from_translate_src():
    """is_bilingual 只由 translate.src 是否为 None 派生 —— C44 的单一来源。"""
    from chrometrans.config import Config, TranslateConfig, is_bilingual

    assert is_bilingual(Config()) is True
    assert is_bilingual(Config(translate=TranslateConfig(src=None))) is False


def test_chinese_is_monolingual():
    """C43 / C44：中文不翻译，且这件事是从 profile 派生的，不是散在各处的 if。"""
    cfg = load_config("zh")

    assert cfg.translate.src is None
    assert cfg.asr.language == "zh"
    assert cfg.asr.initial_prompt, "C46：靠提示词定向简体"


def test_russian_translates_from_russian():
    cfg = load_config("ru")

    assert cfg.asr.language == "ru"
    assert cfg.translate.src == "ru", "C39：两侧必须同步，否则是英语模型听俄语"
