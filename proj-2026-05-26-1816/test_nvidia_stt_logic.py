import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1] / "server"
sys.path.insert(0, str(SERVER_DIR))

from pipecat.frames.frames import (  # noqa: E402
    AudioRawFrame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection  # noqa: E402

from nvidia_stt import NVidiaWebSocketSTTService, _strip_committed_prefix  # noqa: E402


def _make_service():
    # strip_interim_prefix defaults False now; the Part 1 tests exercise the
    # stripping path, so opt in explicitly (as bot-wip.py does for the live endpoint).
    return NVidiaWebSocketSTTService(
        url="ws://localhost:1", sample_rate=16000, strip_interim_prefix=True
    )


def _audio(b: bytes) -> AudioRawFrame:
    return AudioRawFrame(audio=b, sample_rate=16000, num_channels=1)


# --------------------------------------------------------------------------
# Part 1: _strip_committed_prefix — deterministic strip-by-COUNT
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("interim_text", "committed_count", "expected"),
    [
        ("the stale smell", 0, "the stale smell"),  # nothing committed -> full
        ("the stale smell a salt pickle", 3, "a salt pickle"),  # drop 3
        ("the stale smell", 3, ""),  # exactly committed -> nothing new
        ("the stale", 3, None),  # interim shorter than count -> fall back
        ("  the   stale smell   a   salt  ", 3, "a salt"),  # whitespace normalized
        # boundary revision: committed had artifact "ZAC."; interim shows "zest." in
        # that slot, but strip-by-count drops 3 regardless of the revised value.
        ("the stale zest. a salt", 3, "a salt"),
        ("", 0, ""),  # empty interim
        ("   ", 0, ""),  # whitespace-only -> normalized to ''
    ],
)
def test_strip_committed_prefix(interim_text, committed_count, expected):
    assert _strip_committed_prefix(interim_text, committed_count) == expected


# --------------------------------------------------------------------------
# Part 1: _handle_transcript — strip, count accumulation, skips, fallback
# --------------------------------------------------------------------------
def test_handle_transcript_strips_interims_and_counts_committed_tokens():
    async def run_test():
        service = _make_service()
        pushed = []
        service.push_frame = AsyncMock(side_effect=lambda f, d=FrameDirection.DOWNSTREAM: pushed.append(f))

        async def interim(text):
            await service._handle_transcript({"text": text, "is_final": False})

        async def final(text, finalize=True):
            await service._handle_transcript({"text": text, "is_final": True, "finalize": finalize})

        await interim("the stale smell")
        assert isinstance(pushed[-1], InterimTranscriptionFrame) and pushed[-1].text == "the stale smell"
        assert service._committed_token_count == 0

        await final("the stale smell")
        assert isinstance(pushed[-1], TranscriptionFrame)
        assert pushed[-1].text == "the stale smell"  # final text UNCHANGED
        assert pushed[-1].finalized is True
        assert service._committed_token_count == 3

        await interim("the stale smell a salt pickle")  # cumulative -> stripped by count
        assert isinstance(pushed[-1], InterimTranscriptionFrame) and pushed[-1].text == "a salt pickle"

        # empty-remainder interim (== committed) must NOT emit a frame
        n = len(pushed)
        await interim("the stale smell")
        assert len(pushed) == n

        await final("a salt pickle")
        assert pushed[-1].text == "a salt pickle"
        assert service._committed_token_count == 6

        # soft final (finalize=False): no frame, no count change, _waiting_for_final preserved
        n = len(pushed)
        service._waiting_for_final = True
        await final("the stale smell a salt pickle soft", finalize=False)
        assert len(pushed) == n
        assert service._committed_token_count == 6
        assert service._waiting_for_final is True

        # empty hard final: no frame, no count change, clears the wait
        await final("")
        assert len(pushed) == n
        assert service._committed_token_count == 6
        assert service._waiting_for_final is False

        # cumulative interim with revised prior tokens -> stripped by count (no leak)
        await interim("the stale zest. a brine pickle and pepper")
        assert isinstance(pushed[-1], InterimTranscriptionFrame)
        assert pushed[-1].text == "and pepper"

        # interim SHORTER than committed (only without a cumulative session) -> emit unchanged
        await interim("the stale smell")  # 3 < count 6 -> fallback
        assert isinstance(pushed[-1], InterimTranscriptionFrame)
        assert pushed[-1].text == "the stale smell"

    asyncio.run(run_test())


def test_multi_turn_artifact_accumulation_no_leak():
    """Regression guard: with forced-finalization artifacts on every turn's last
    token, strip-by-count must keep working across many turns (the value-matching
    approach leaked the full cumulative text after a few turns)."""

    async def run_test():
        service = _make_service()
        pushed = []
        service.push_frame = AsyncMock(side_effect=lambda f, d=FrameDirection.DOWNSTREAM: pushed.append(f))

        async def interim(text):
            await service._handle_transcript({"text": text, "is_final": False})

        async def final(text):
            await service._handle_transcript({"text": text, "is_final": True, "finalize": True})

        # Each final's LAST token is an artifact ("ZAC.", "PIK.", ...) that the next
        # cumulative interim corrects ("zest.", "pickle.", ...).
        await final("the stale ZAC.")  # count=3
        await interim("the stale zest. a salt")
        assert pushed[-1].text == "a salt" and "stale" not in pushed[-1].text

        await final("a salt PIK.")  # count=6
        await interim("the stale zest. a salt pickle. and pepper")
        assert pushed[-1].text == "and pepper" and "stale" not in pushed[-1].text  # no leak after 2 turns

        await final("and pepper")  # count=8
        await interim("the stale zest. a salt pickle. and pepper now what")
        assert pushed[-1].text == "now what" and "stale" not in pushed[-1].text  # no leak after 3 turns

    asyncio.run(run_test())


# --------------------------------------------------------------------------
# Part 2: VAD gating, pre-roll ring, matched-stop effects, direction
# --------------------------------------------------------------------------
def test_ring_trim_and_vad_gating_with_matched_stop_effects():
    async def run_test():
        service = _make_service()
        service._preroll_bytes = 8
        sent = []
        pushed = []

        async def fake_run_stt(audio):
            sent.append(audio)
            service._audio_bytes_sent += len(audio)
            yield None

        service.run_stt = fake_run_stt
        service.push_frame = AsyncMock(side_effect=lambda f, d=FrameDirection.DOWNSTREAM: pushed.append((f, d)))
        service._send_reset = AsyncMock()
        service.start_ttfb_metrics = AsyncMock()

        # idle: audio accumulates in the ring, trimmed to last preroll_bytes, nothing sent
        for b in (b"aaaa", b"bbbb", b"cccc"):
            await service.process_audio_frame(_audio(b), FrameDirection.DOWNSTREAM)
        assert bytes(service._audio_ring) == b"bbbbcccc"
        assert sent == []

        # VAD start: flush pre-roll once, clear ring, start streaming; frame pushed once UPSTREAM
        await service.process_frame(VADUserStartedSpeakingFrame(start_secs=0.2), FrameDirection.UPSTREAM)
        assert service._user_speaking is True
        assert service._audio_ring == bytearray()
        assert sent == [b"bbbbcccc"]
        assert [(type(f), d) for f, d in pushed] == [(VADUserStartedSpeakingFrame, FrameDirection.UPSTREAM)]

        # semantic UserStartedSpeaking must NOT clear VAD speaking state (regression guard)
        await service.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        assert service._user_speaking is True

        # live audio streams while speaking (not re-ringed)
        await service.process_audio_frame(_audio(b"dddd"), FrameDirection.DOWNSTREAM)
        assert sent == [b"bbbbcccc", b"dddd"]

        # matched VAD stop (audio streamed): finalize + ttfb + waiting flag, frame pushed UPSTREAM.
        # stop_secs=0.0 so base _handle_vad_user_stopped_speaking returns before its own
        # start_ttfb/create_task; our override is the sole start_ttfb_metrics call.
        await service.process_frame(VADUserStoppedSpeakingFrame(stop_secs=0.0), FrameDirection.UPSTREAM)
        assert service._user_speaking is False
        service._send_reset.assert_awaited_once_with(finalize=True)
        service.start_ttfb_metrics.assert_awaited_once()
        assert service._waiting_for_final is True
        assert (VADUserStoppedSpeakingFrame, FrameDirection.UPSTREAM) in [(type(f), d) for f, d in pushed]

        # after stop, audio is buffered again (not sent)
        await service.process_audio_frame(_audio(b"eeee"), FrameDirection.DOWNSTREAM)
        assert sent == [b"bbbbcccc", b"dddd"]
        assert bytes(service._audio_ring) == b"eeee"

    asyncio.run(run_test())


def test_unmatched_vad_stop_skips_finalize_and_base_ttfb():
    """No audio streamed since last finalize -> skip reset, clear ring, propagate,
    and do NOT start TTFB (stop_secs>0 would otherwise let base start one)."""

    async def run_test():
        for user_speaking in (True, False):  # 'mid' and 'no prior start' variants
            service = _make_service()
            service._audio_ring += b"preroll"
            service._user_speaking = user_speaking
            assert service._audio_bytes_sent == 0
            pushed = []
            service.push_frame = AsyncMock(side_effect=lambda f, d=FrameDirection.DOWNSTREAM: pushed.append((f, d)))
            service._send_reset = AsyncMock()
            service.start_ttfb_metrics = AsyncMock()

            await service.process_frame(
                VADUserStoppedSpeakingFrame(stop_secs=0.2), FrameDirection.UPSTREAM
            )

            assert service._user_speaking is False
            assert service._audio_ring == bytearray()
            assert service._waiting_for_final is False
            service._send_reset.assert_not_awaited()
            service.start_ttfb_metrics.assert_not_awaited()
            assert [(type(f), d) for f, d in pushed] == [
                (VADUserStoppedSpeakingFrame, FrameDirection.UPSTREAM)
            ]

    asyncio.run(run_test())


# --------------------------------------------------------------------------
# Part 2: process_frame(AudioRawFrame) preserves base behaviors
# --------------------------------------------------------------------------
def test_audio_passthrough_mute_and_reconnect_preserved():
    async def run_test():
        # passthrough: audio frame still pushed downstream while gated (not speaking)
        service = _make_service()
        service._preroll_bytes = 1024
        pushed = []
        service.push_frame = AsyncMock(side_effect=lambda f, d=FrameDirection.DOWNSTREAM: pushed.append(f))
        frame = _audio(b"xxxx")
        await service.process_frame(frame, FrameDirection.DOWNSTREAM)
        assert frame in pushed  # audio_passthrough kept the analyzer fed
        assert bytes(service._audio_ring) == b"xxxx"

        # mute: dropped from STT processing (not ringed)
        service = _make_service()
        service._preroll_bytes = 1024
        service._muted = True
        service.push_frame = AsyncMock()
        await service.process_audio_frame(_audio(b"yyyy"), FrameDirection.DOWNSTREAM)
        assert service._audio_ring == bytearray()

        # reconnect: buffered into the replay buffer, not the ring, not sent
        service = _make_service()
        service._preroll_bytes = 1024
        service._reconnecting = True
        service.push_frame = AsyncMock()
        f = _audio(b"zzzz")
        await service.process_audio_frame(f, FrameDirection.DOWNSTREAM)
        assert service._audio_ring == bytearray()
        assert service._reconnect_audio_buffer == [(f, FrameDirection.DOWNSTREAM)]

    asyncio.run(run_test())
