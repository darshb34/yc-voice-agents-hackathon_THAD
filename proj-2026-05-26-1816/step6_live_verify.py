#!/usr/bin/env python3
"""Step 6 (automated portion): validate the CLIENT-side per-turn fix against REAL
server output.

We can't drive a full headless WebRTC client here, so instead we:
  1. Connect to the live Nemotron STT server and send two real utterances the way
     the bot does (gated: pre-roll + stream during speech, hard reset at each VAD
     stop), with NO `end` between them — reproducing the warm-session condition
     that caused cumulative interims.
  2. Capture the raw transcript messages the server emits.
  3. Replay them through the ACTUAL NVidiaWebSocketSTTService._handle_transcript
     (push_frame captured), exactly as the receive task would.
  4. Assert the EMITTED frames a UI would receive are per-turn: utterance-2
     interims contain NONE of utterance-1's text, and finals are per-turn deltas.

Covers plan Step 6 criteria (a) per-turn interims / no cumulative, (b) per-turn
finals, (e) no spurious cross-turn text. Does NOT cover (c) turn-taking latency,
(d) idle survival in the full pipeline, (f) quiet-utterance turn-start, (g)
barge-in — those need the live bot + a human listen-test.

Run: cd server && uv run python ../proj-2026-05-26-1816/step6_live_verify.py
"""

import asyncio
import json
import sys
import wave
from pathlib import Path
from unittest.mock import AsyncMock

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from pipecat.frames.frames import (  # noqa: E402
    InterimTranscriptionFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection  # noqa: E402

from nvidia_stt import NVidiaWebSocketSTTService  # noqa: E402

URL = "ws://192.168.7.228:8081"
WAV = "/home/khkramer/src/nemotron-january-2026/tests/fixtures/harvard_16k.wav"
PREROLL_BYTES = 16000 * 2  # ~1s


def load_two_utterances():
    w = wave.open(WAV, "rb")
    pcm = w.readframes(w.getnframes())
    w.close()
    half = (len(pcm) // 2) & ~1
    return pcm[:half], pcm[half:]


async def drain(ws, idle_s=1.5):
    msgs = []
    while True:
        try:
            m = await asyncio.wait_for(ws.recv(), timeout=idle_s)
        except TimeoutError:
            break
        d = json.loads(m)
        if d.get("type") == "transcript":
            msgs.append(d)
    return msgs


async def send_gated(ws, pcm, preroll_bytes=PREROLL_BYTES, chunk=640):
    """Mimic the bot: hold a pre-roll while 'idle', flush it at speech start, then
    stream live. (Here we just send pre-roll then the rest — the server sees one
    contiguous speech segment, same as the gated client produces.)"""
    await ws.send(pcm[:preroll_bytes])  # pre-roll flush
    for i in range(preroll_bytes, len(pcm), chunk):
        await ws.send(pcm[i:i + chunk])
        await asyncio.sleep(0.004)


async def capture_real_server_messages():
    u1, u2 = load_two_utterances()
    seq = []  # list of ("u1"|"u2", msg) in arrival order
    async with websockets.connect(URL, ping_interval=20, ping_timeout=20) as ws:
        ready = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        assert ready.get("type") == "ready", ready
        await send_gated(ws, u1)
        await ws.send(json.dumps({"type": "reset", "finalize": True}))
        for m in await drain(ws):
            seq.append(("u1", m))
        # NO end between turns -> warm session -> cumulative interims expected
        await send_gated(ws, u2)
        await ws.send(json.dumps({"type": "reset", "finalize": True}))
        for m in await drain(ws):
            seq.append(("u2", m))
    return seq


async def replay_through_client(seq):
    """Feed captured server messages through the real service and capture frames."""
    service = NVidiaWebSocketSTTService(url="ws://unused:1", sample_rate=16000)
    emitted = []  # (turn_tag, frame)
    current_tag = {"t": None}

    async def cap(frame, direction=FrameDirection.DOWNSTREAM):
        emitted.append((current_tag["t"], frame))

    service.push_frame = AsyncMock(side_effect=cap)
    for tag, msg in seq:
        current_tag["t"] = tag
        await service._handle_transcript(msg)
    return emitted


def first_tokens(s, n=3):
    return " ".join(s.split()[:n]).lower()


async def main():
    print(f"Connecting to live server {URL} and capturing real two-utterance output...")
    seq = await capture_real_server_messages()
    raw_u1_finals = [m["text"] for t, m in seq if t == "u1" and m.get("is_final") and m.get("finalize")]
    raw_u2_interims = [m["text"] for t, m in seq if t == "u2" and not m.get("is_final")]
    print(f"raw U1 finals: {raw_u1_finals}")
    print(f"raw U2 interims (server, expected CUMULATIVE): "
          f"{raw_u2_interims[-1][:80] if raw_u2_interims else None!r} ...")

    emitted = await replay_through_client(seq)
    u1_final_text = next((f.text for t, f in emitted
                          if t == "u1" and isinstance(f, TranscriptionFrame)), "")
    u2_finals = [f.text for t, f in emitted if t == "u2" and isinstance(f, TranscriptionFrame)]
    u2_interims = [f.text for t, f in emitted if t == "u2" and isinstance(f, InterimTranscriptionFrame)]

    print("\n--- EMITTED to UI (after client stripping) ---")
    print(f"U1 final frame:        {u1_final_text!r}")
    print(f"U2 final frame(s):     {u2_finals}")
    print(f"U2 last interim frame: {(u2_interims[-1] if u2_interims else None)!r}")

    # Assertions
    ok = True
    u1_head = first_tokens(u1_final_text)
    # (a)/(e): no U2-emitted interim may contain U1's opening words
    leaked = [i for i in u2_interims if u1_head and u1_head in i.lower()]
    if leaked:
        ok = False
        print(f"\nFAIL (a/e): U2 interim still contains U1 text {u1_head!r}: {leaked[-1][:80]!r}")
    else:
        print(f"\nPASS (a/e): no U2 interim contains U1 opening {u1_head!r} (cumulative text stripped)")

    # Verify the SERVER really was cumulative (else the test proves nothing)
    server_cumulative = bool(raw_u2_interims and u1_head and u1_head in raw_u2_interims[-1].lower())
    print(f"{'PASS' if server_cumulative else 'WARN'} (precondition): raw server U2 interim "
          f"{'was' if server_cumulative else 'was NOT'} cumulative")

    # (b): U2 final is a per-turn delta (non-empty, doesn't start with U1 head)
    if u2_finals and u2_finals[0].strip() and u1_head not in u2_finals[0].lower():
        print(f"PASS (b): U2 final is a per-turn delta: {u2_finals[0][:60]!r}")
    else:
        ok = False
        print(f"FAIL (b): U2 final unexpected: {u2_finals!r}")

    print(f"\n=== AUTOMATED RESULT: {'PASS' if ok and server_cumulative else 'CHECK'} ===")
    print("NOT covered here (need live bot + human): (c) turn-taking latency / TTFB,"
          " (d) >25s idle survival in full pipeline, (f) quiet-utterance turn-start,"
          " (g) barge-in onset capture.")


if __name__ == "__main__":
    asyncio.run(main())
