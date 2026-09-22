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
