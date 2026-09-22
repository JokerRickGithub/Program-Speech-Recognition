import numpy as np
import pytest

from chrometrans.audio.capture import ProcessAudioStream, find_target_pid


@pytest.mark.integration
def test_captures_real_audio_from_chrome():
    """需要 Chrome 正在播放有声内容。"""
    pid = find_target_pid(("chrome.exe",))
    stream = ProcessAudioStream(pid=pid)
    stream.start()
    try:
        total = []
        for _ in range(40):                 # 约 400ms 的块 ×N
            chunk = stream.read()
            if chunk.size:
                total.append(chunk)
            if sum(len(c) for c in total) > 16000 * 3:
                break
        audio = np.concatenate(total) if total else np.zeros(0, dtype=np.float32)
    finally:
        stream.stop()

    assert audio.size > 16000, "没读到足够的音频"
    rms = float(np.sqrt(np.mean(audio ** 2)))
    assert rms > 1e-4, f"读到的是静音（rms={rms}）—— Chrome 在播放吗？"
