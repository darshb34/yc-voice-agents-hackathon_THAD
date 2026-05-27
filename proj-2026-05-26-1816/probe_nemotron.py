#!/usr/bin/env python3
"""Throwaway probe for the Nemotron STT WebSocket server (PLAN Step 1).

Confirms, against the live server:
  (a) interim (is_final:false) text is cumulative across turns while final
      (finalize:true) text is a per-turn delta  -> CUMULATIVE_INTERIMS_CONFIRMED
  (b) `end` vs `reset{finalize:true}` at a turn boundary: does `end` cold-reset
      (next utterance interim starts fresh?) and the next-turn-readiness latency
  (c) keepalive: connection survives >25 s of no audio (WS ping/pong)

Run: uv run python proj-2026-05-26-1816/probe_nemotron.py
"""

import asyncio
import json
import time
import wave

import websockets

URL = "ws://192.168.7.228:8081"
WAV = "/home/khkramer/src/nemotron-january-2026/tests/fixtures/harvard_16k.wav"
CHUNK_MS = 20


def load_pcm():
    w = wave.open(WAV, "rb")
    assert w.getframerate() == 16000 and w.getsampwidth() == 2 and w.getnchannels() == 1
    n = w.getnframes()
    pcm = w.readframes(n)
    w.close()
    # split into two "utterances" (halves), byte-aligned to samples
    half = (len(pcm) // 2) & ~1
    return pcm[:half], pcm[half:]


async def recv_until_idle(ws, idle_s=1.2, tag=""):
    """Drain messages until no message for idle_s; return list of (kind, text)."""
    out = []
    while True:
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=idle_s)
        except TimeoutError:
            break
        d = json.loads(msg)
        if d.get("type") == "transcript":
            kind = (
                "FINAL" if d.get("is_final") and d.get("finalize")
                else "final-soft" if d.get("is_final")
                else "interim"
            )
            out.append((kind, d.get("text", "")))
    return out


async def stream(ws, pcm, chunk_ms=CHUNK_MS, pace=0.005):
    step = int(16000 * chunk_ms / 1000) * 2
    first_resp_t = {}
    for i in range(0, len(pcm), step):
        await ws.send(pcm[i:i + step])
        await asyncio.sleep(pace)
    return first_resp_t


async def boundary(ws, kind):
    await ws.send(json.dumps({"type": kind, "finalize": True} if kind == "reset" else {"type": "end"}))


async def main():
    u1, u2 = load_pcm()
    print(f"loaded utterances: u1={len(u1)}B (~{len(u1)/32000:.1f}s) u2={len(u2)}B")

    # ---- Session A: reset-only (cumulative check) ----
    print("\n=== SESSION A: reset-only (cumulative vs delta) ===")
    async with websockets.connect(URL, ping_interval=20, ping_timeout=20) as ws:
        ready = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        print("ready:", ready)
        await stream(ws, u1)
        await boundary(ws, "reset")
        a1 = await recv_until_idle(ws, tag="u1")
        await stream(ws, u2)
        await boundary(ws, "reset")
        a2 = await recv_until_idle(ws, tag="u2")
        f1 = [t for k, t in a1 if k == "FINAL"]
        f2 = [t for k, t in a2 if k == "FINAL"]
        i2 = [t for k, t in a2 if k == "interim"]
        print(f"U1 FINAL(s): {f1}")
        print(f"U2 FINAL(s): {f2}")
        print(f"U2 first interim: {i2[0] if i2 else None!r}")
        print(f"U2 last  interim: {i2[-1] if i2 else None!r}")
        u1_head = (f1[0].split()[:3] if f1 and f1[0].split() else [])
        cumulative = bool(i2 and u1_head and " ".join(u1_head).lower() in i2[-1].lower())
        print(f">>> CUMULATIVE_INTERIMS_CONFIRMED = {cumulative} "
              f"(U2 interim contains U1 head {u1_head})")

    # ---- Session B: end at boundary (cold-reset + next-turn readiness) ----
    print("\n=== SESSION B: end-at-boundary (cold reset? readiness latency) ===")
    async with websockets.connect(URL, ping_interval=20, ping_timeout=20) as ws:
        json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        await stream(ws, u1)
        # measure reset-path finalize latency
        t0 = time.time()
        await boundary(ws, "reset")
        rb = await recv_until_idle(ws, tag="u1")
        reset_final_latency = next((time.time() - t0 for k, t in rb if k == "FINAL"), None)
        # now send end, then U2, measure time-to-first-interim (readiness)
        await boundary(ws, "end")
        await recv_until_idle(ws, idle_s=0.6)
        t1 = time.time()
        step = int(16000 * CHUNK_MS / 1000) * 2
        first_interim_dt = None
        for i in range(0, len(u2), step):
            await ws.send(u2[i:i + step])
            await asyncio.sleep(0.005)
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=0.001)
                d = json.loads(msg)
                if d.get("type") == "transcript" and not d.get("is_final") and first_interim_dt is None:
                    first_interim_dt = time.time() - t1
                    fresh = d.get("text", "")
            except TimeoutError:
                pass
        await boundary(ws, "reset")
        bb = await recv_until_idle(ws, tag="u2")
        i2b = [t for k, t in bb if k == "interim"]
        print(f"reset-path finalize latency: "
              f"{reset_final_latency*1000:.0f}ms" if reset_final_latency else "n/a")
        print(f"post-end U2 first interim after audio start: "
              f"{first_interim_dt*1000:.0f}ms" if first_interim_dt else "n/a")
        u1_head = (rb and [t for k, t in rb if k == 'FINAL'])
        head_tokens = u1_head[0].split()[:3] if u1_head else []
        post_end_fresh = bool(i2b and head_tokens
                              and " ".join(head_tokens).lower() not in i2b[-1].lower())
        print(f">>> post-`end` U2 interim FRESH (no U1 text) = {post_end_fresh}")
        print(f"    U2 last interim after end: {i2b[-1] if i2b else None!r}")

    # ---- Session C: keepalive idle >25s ----
    print("\n=== SESSION C: keepalive (idle >25s) ===")
    async with websockets.connect(URL, ping_interval=20, ping_timeout=20) as ws:
        json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        await stream(ws, u1[: 32000 * 2])  # ~2s
        await boundary(ws, "reset")
        await recv_until_idle(ws)
        print("idling 26s with no audio...")
        await asyncio.sleep(26)
        try:
            await ws.send(u2[:6400])  # send a little audio post-idle
            await boundary(ws, "reset")
            post = await recv_until_idle(ws)
            print(f">>> connection ALIVE after 26s idle; post-idle msgs: {len(post)}")
        except Exception as e:
            print(f">>> connection DIED after idle: {e!r}")


if __name__ == "__main__":
    asyncio.run(main())
