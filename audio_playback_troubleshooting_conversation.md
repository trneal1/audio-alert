# Audio Playback Troubleshooting Conversation

Date: 2026-06-06
Project: audio alert

## User
No code change. I have noticed some broken up audio between send_text_service and the firmware. Also some playback timeouts.

## Codex
Inspected the project read-only, including platformio.ini, send_text_service.py, send_google_tts.py, send_tone.py, send_radio.py, send_clock.py, and src/audio alert.cpp.

Key observations:

1. The transport declares a fixed AUD1 payload length from Python and the firmware reads TCP data while writing to I2S.
2. Firmware has a 5 second client idle timeout via CLIENT_IDLE_TIMEOUT_MS.
3. Python send_pcm uses a 10 second socket timeout and treats a missing final DONE reply as non-fatal.
4. send_radio.py removes the socket timeout after the initial OK, which suggests long playback timeout behavior may differ between radio streaming and TTS/tone sends.
5. Firmware calls i2s_zero_dma_buffer() immediately after the final write, which may clip audio still queued in DMA.

Suggested tests:

```powershell
python send_tone.py audio-alert.local 440:20
```

If tone breaks up, focus on firmware/Wi-Fi/I2S/power. If tone is clean but TTS breaks up, focus on TTS conversion, sample rate, volume, or clip-tail handling.

Also suggested trying lower bandwidth:

```powershell
python send_text_service.py audio-alert.local --sample-rate 16000
```

## User
I occasionally hear breakup and pauses during audio playback.

## Codex
Likely causes:

1. Wi-Fi/TCP jitter: firmware plays as bytes arrive, so network stalls can become audible gaps.
2. I2S buffer underrun: there is no larger prebuffer before playback.
3. Power instability: the MAX98357A amp may crackle/drop out if the 5V/USB supply sags during loud sections.
4. Sender-side timeout/reporting: longer clips can finish playing but still exceed Python's 10 second final DONE wait.

Recommended isolation test:

```powershell
python send_tone.py audio-alert.local 440:30 -v 0.25
```

Also test:

```powershell
python send_text_service.py audio-alert.local --sample-rate 16000 --volume 0.65
```

If lower sample rate/volume improves playback, that points toward buffering, Wi-Fi bandwidth, or power/clipping.

## User
Would the firmware fully buffering the message from the TTS service before playback be useful?

## Codex
Yes. Fully buffering finite TTS messages before playback would likely help with breakup and pauses.

Current behavior:

```text
TCP receive -> I2S playback immediately
```

Buffered behavior:

```text
TCP receive complete PCM payload -> then I2S playback from RAM
```

This removes Wi-Fi jitter from the audible playback path.

RAM cost at 24000 Hz mono 16-bit PCM:

```text
24000 samples/sec * 2 bytes = 48 KB/sec
5 sec  = 240 KB
10 sec = 480 KB
20 sec = 960 KB
30 sec = 1.44 MB
```

Recommendation:

- Short TTS alerts: full-buffer before playback is useful and simple.
- Long messages: use a max buffer limit or fall back to streaming.
- Radio/live streams: keep streaming.
- A middle-ground ring buffer with 0.5-2 seconds of prebuffer would also help while limiting RAM use.

Also noted that i2s_zero_dma_buffer() immediately after the final write may clip the tail of playback and should be investigated before or alongside full buffering.
