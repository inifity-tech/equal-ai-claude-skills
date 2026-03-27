# Deep Dive: WebSocket Handling in the Backend Service

So I've spent a good while digging through the WebSocket handling code, the architecture docs, the incident reports, and the production metrics. Here's what I'm seeing -- and I have some strong opinions about some of it.

---

## Doc-Code Discrepancies

Before we dive into the analysis, I found a few places where the docs don't quite match reality:

1. **HLD says PostgreSQL is "dead code"** -- the `backend/db/db.py` file exists and the lifespan handler runs `run_migrations()` on every startup. The HLD claims "no active query managers call it," but migrations are running. Either the DB is being used somewhere downstream (Alembic migrations imply active schema management) or these migrations are vestigial. Worth clarifying.

2. **HLD describes 4 WebSocket endpoints but doesn't distinguish v1 vs v2** -- The code has four WebSocket endpoints (`/stream/caller/{session_id}`, `/stream/caller_v2/{session_id}`, `/stream/user/{session_id}`, `/stream/user_v2/{session_id}`), but the HLD only mentions the caller WebSocket path generically. The v2 endpoint has fundamentally different behavior (requires pre-resolved dependencies, fail-fast, no fallback). This architectural distinction matters.

3. **HLD mentions "Ultravox" as the voice AI but the code now has three providers** -- The codebase supports Ultravox (legacy WebSocket to cloud), Pipecat with Gemini Live (local pipeline), and GeminiLiveNativeAgent (direct SDK, no Pipecat). The HLD's component topology only shows Ultravox and Pipecat. The native Gemini agent is a significant architectural addition that bypasses Pipecat entirely.

---

## Current State: How WebSocket Handling Actually Works

The WebSocket architecture in `myequal-ai-backend` is fundamentally a **bidirectional audio bridge** pattern. Here's what's actually happening:

### The Four WebSocket Endpoints

The system has four WebSocket endpoints in `/backend/api/v1/calls.py` (this file is massive -- over 4700 lines):

| Endpoint | Purpose | Provider Support |
|---|---|---|
| `/stream/caller/{session_id}` | Inbound caller audio (v1) | Ultravox + Pipecat + Native Gemini |
| `/stream/caller_v2/{session_id}` | Optimized caller audio (v2) | Same, but requires pre-resolved deps |
| `/stream/user/{session_id}` | User/agent audio (v1) | Ultravox only |
| `/stream/user_v2/{session_id}` | Optimized user audio (v2) | Ultravox + Pipecat |

The `caller_stream_v2` endpoint is the interesting one -- it requires all dependencies to be pre-resolved before the WebSocket connection, cutting setup time from 800-1500ms down to 50-150ms. That's a significant optimization for real-time voice.

### Architecture Per Voice Provider

The WebSocket handling differs radically depending on the voice provider:

**Ultravox (Legacy):**
```
Exotel WS (8kHz) --> caller_stream --> [manual bridge] --> Ultravox WS (cloud)
                                           |
                                           +--> listeners (STT, recording, participants)
```
Two separate WebSocket connections managed in the same endpoint. Audio is manually forwarded between them via `send_to_bot()` and `send_to_caller()` closures. The `Caller` participant runs a receive loop on the Exotel WS, and the `UltravoxAgent` runs a receive loop on the Ultravox WS. Both run concurrently in an `anyio.create_task_group()`.

**Pipecat (Gemini Live):**
```
Exotel WS (8kHz) --> FastAPIWebsocketTransport --> [Pipecat Pipeline] --> Gemini (16/24kHz)
                         (handles WS natively)
```
The Pipecat `FastAPIWebsocketTransport` takes ownership of the WebSocket. The `Caller.run()` loop is skipped entirely -- instead, the code polls `caller.websocket.client_state` every 100ms to detect disconnection. Audio resampling (8kHz <-> 16/24kHz) happens inside the `ExotelFrameSerializer`.

**GeminiLiveNativeAgent (Direct SDK):**
```
Exotel WS (8kHz) --> [3 async loops] --> Gemini Live SDK (16/24kHz)
                     audio_send_loop
                     message_receive_loop
                     audio_output_loop
```
Bypasses Pipecat entirely. Three async loops handle the bidirectional bridge. Audio resampling via `audioop.ratecv`.

### The Task Group Pattern

All concurrent work within a call session runs inside an `anyio.create_task_group()`. This is the core concurrency primitive:

```python
async with anyio.create_task_group() as tg:
    tg.start_soon(run_caller)         # Exotel WebSocket receive loop
    tg.start_soon(run_voice_agent)    # Ultravox/Pipecat/Native Gemini
    tg.start_soon(participants.live_transcript.run)  # STT
    tg.start_soon(participants.live_call_participant.run)  # Nudges, summary
    tg.start_soon(participants.text_category_manager.run)  # Category detection
    tg.start_soon(participants.text_language_manager.run)  # Language detection
    tg.start_soon(participants.sarvam_streaming_stt.run)  # Streaming STT
    tg.start_soon(live_audio_participant.run)  # Audio recording
```

When any task in the group raises an unhandled exception, ALL other tasks get cancelled. This is structured concurrency at work -- and it's mostly the right choice for a call session where everything lives and dies together.

### Exception Handling: The Hard-Won Lessons

The exception handling in the task group uses Python 3.11's `except*` syntax:

```python
except* WebSocketDisconnect as eg:  # Caller disconnected
except* ConnectionClosedOK:          # Ultravox closed normally
except* ConnectionClosedError:       # Ultravox closed unexpectedly
except* Exception as e:              # Catch-all
```

There's a well-documented incident (`docs/websocket-disconnect-cleanup-issue.md`) where a `WebSocketDisconnect` in `send_to_caller()` was propagating to the task group, killing all participants and preventing proper `BotDisconnected` event emission. The fix was a two-layer defense:

1. Catch `WebSocketDisconnect` at the source (in `send_to_caller()`)
2. Safety net in the `finally` block to explicitly disconnect the voice agent

This is a good pattern. The lesson here: **in a task group handling real-time audio, you must be extremely careful about which exceptions propagate**. Any unhandled exception kills the entire call.

### Cleanup: The Triple-Defense Pattern

The cleanup logic has three layers, which reflects hard-won production experience:

1. **In `run_caller()` / `run_voice_agent()`**: When either side completes, it disconnects the other and calls `disconnect_agents()` to stop all participants.
2. **In `except*` handlers**: Metrics and logging for different disconnect scenarios.
3. **In `finally` block**: Comprehensive cleanup of voice agent, all participants, Krisp processor, audio recording, and session state. Every resource gets its own try/except to prevent cascading failures during cleanup.

This triple defense is necessary but creates a lot of code duplication. The `finally` block in `caller_stream_v2` alone is about 200 lines of cleanup code.

---

## Production Reality

### Connection Rates

From Datadog metrics over the last 24 hours, `myequal.websocket.connections.opened` shows:

- **Peak rate**: ~0.18 connections/second (roughly 650/hour during peak)
- **Trough**: ~0.01 connections/second (during off-hours, likely nighttime India)
- **Clear diurnal pattern**: Traffic follows Indian business hours

This is moderate load. At these rates, the per-connection resource model is fine. But the question is what happens at 10x.

### Monitoring Gaps

There are **zero Datadog monitors** configured for WebSocket-related metrics. No alerts on:
- WebSocket connection failure rates
- Duplicate connection attempts
- Audio send latency spikes
- Task group exception rates
- Event loop blocking duration

This is a significant gap. The WebSocket metrics infrastructure exists (the `WebSocketMetrics` class is comprehensive) but nobody has wired up monitors. You're flying blind on WebSocket health in production.

---

## External Context: Current Best Practices

### FastAPI WebSocket Patterns (2025-2026)

FastAPI's WebSocket handling is intentionally minimal -- it's essentially a thin wrapper around Starlette's WebSocket support. The official docs recommend:
- Catching `WebSocketDisconnect` explicitly
- Using a connection manager for multi-client scenarios
- Dependency injection works in WebSocket endpoints (which this codebase uses heavily)

The codebase is already following these patterns correctly.

### Pipecat's `FastAPIWebsocketTransport`

Pipecat (v0.0.95+) provides `FastAPIWebsocketTransport` specifically for telephony WebSocket integrations. It handles the WebSocket lifecycle internally and provides frame serializers for different telephony providers. The codebase's `ExotelFrameSerializer` extends this correctly, adding Exotel-specific JSON protocol handling and audio resampling.

The key insight from Pipecat's docs: **for telephony integrations, the transport owns the WebSocket**. This means when using Pipecat, you shouldn't also run a `Caller.run()` loop on the same WebSocket -- which the code correctly handles by skipping `caller.run()` for interface-based providers.

### AnyIO Task Groups

AnyIO's task groups follow structured concurrency principles -- when one task fails, all siblings are cancelled. The `except*` syntax (Python 3.11+) allows catching specific exception types from the group while letting others propagate. This is exactly the right tool for this use case.

### Redis Streams for Event Publishing

The `SessionStream` using Redis XADD with `MAXLEN=1000` is a solid pattern for bounded event streaming. The `approximate=True` flag lets Redis optimize trimming (it may keep slightly more than 1000 entries). This is the recommended approach for real-time event distribution.

Key versions in use:
- **FastAPI**: >=0.115.8 (current stable is 0.128.0 -- a few minor versions behind)
- **redis-py**: Pinned to 5.2.1 (there's a documented asyncio.Lock contention bug in 5.3.0+)
- **Pipecat**: >=0.0.95 (current stable is ~0.0.108)
- **websockets**: Used for the Ultravox client connection (separate from FastAPI's WebSocket)
- **anyio**: Used for task groups and locks

---

## My Take

### What's Strong

1. **The per-session lock architecture is well-designed.** Using `anyio.Lock` per session (not a global lock) was a deliberate choice documented in commit `d8ad1aa7`. This lets independent sessions run in parallel while serializing operations within each session. The safety note about sync-only dict mutations being atomic in asyncio is correct.

2. **The fire-and-forget metrics pattern is correct.** Audio metrics use `asyncio.create_task()` to avoid blocking the hot path. DogStatsD is UDP-based, so metric emission is already fast, but the extra decoupling ensures zero impact on audio latency. The `_background_tasks` set with `add_done_callback(discard)` prevents GC of running tasks.

3. **The idempotency check for WebSocket connections using atomic Redis SET NX** (`try_connect_caller_stream`) is the right pattern for preventing duplicate Exotel connections.

4. **The pre-resolved dependency pattern in v2** is excellent. Pre-warming all participant dependencies before the WebSocket connects (Ultravox call creation, prompt loading, Statsig checks, blob manager initialization) eliminates the cold-start latency that plagued v1.

5. **The task leak debugging infrastructure** is impressive. Having an `EventLoopMonitor`, `TaskLeakDebugger`, and `MemoryMetricsEmitter` built into the service shows operational maturity.

### What Concerns Me

1. **The 4700-line `calls.py` file is a maintenance hazard.** Four WebSocket endpoints, each with ~500+ lines of setup, task group management, and cleanup code. The v1 and v2 endpoints are ~80% identical with subtle differences. This is where bugs hide. The duplication between `send_to_caller()` in v1 and v2, the duplicated `AudioPacketTracker` dataclass defined inside each function, the duplicated cleanup logic -- this cries out for extraction into a shared `CallSession` class or similar abstraction.

2. **The 100ms polling loop for interface-based provider disconnect detection is wasteful:**
   ```python
   while caller.websocket.client_state != WebSocketState.DISCONNECTED:
       await asyncio.sleep(0.1)
   ```
   When using Pipecat, the `Caller.run()` loop is skipped because Pipecat owns the WebSocket. But instead of being notified of disconnection, the code polls every 100ms. At 100 concurrent calls, that's 1000 wakeups/second for nothing. A better pattern would be using an `asyncio.Event` that's set when the WebSocket disconnects, or having the Pipecat transport notify via a callback.

3. **No backpressure on the audio bridge.** When sending audio from Exotel to Ultravox (or vice versa), there's no backpressure mechanism. If Ultravox's WebSocket is slow to accept data:
   ```python
   await bot_ws.send(decoded_audio, text=False)  # Blocks on slow consumer
   ```
   This `await` blocks the caller's receive loop, meaning Exotel audio packets queue up in the kernel buffer. Under load, this could cause Exotel to time out and disconnect. A bounded queue or circuit breaker pattern would be safer.

4. **The `send_to_caller()` closure captures `caller_ws` by reference without a liveness check before each send.** While there's a `WebSocketState.DISCONNECTED` check, there's a well-documented TOCTOU race between the check and the actual send (see `docs/websocket-disconnect-cleanup-issue.md`). The fix catches `WebSocketDisconnect`, but the pattern is fundamentally fragile. A better approach would be a `SafeWebSocket` wrapper that handles this atomically.

5. **Thread pool sizing at 64 threads for LLM calls** seems arbitrary. The comment says "min(32, cpu_count + 4) which is ~12 threads on 8-core" and bumps it to 64. But there's no analysis of how many concurrent LLM calls are expected per instance. At 100 concurrent calls, each with multiple participants making LLM calls (category detection, NER, language detection, nudges), 64 threads could be a bottleneck. The pool would fill and subsequent `asyncio.to_thread()` calls would queue. This could cascade into event loop blocking if the queue wait is long.

6. **No WebSocket connection timeout on the Ultravox side.** The Ultravox WebSocket connection (`websockets.connect(session.ultravox_join_url)`) has no explicit timeout:
   ```python
   bot_ws = await websockets.connect(session.ultravox_join_url)
   ```
   If the Ultravox service is slow to respond, this hangs indefinitely, blocking the caller's audio. The `websockets` library has `open_timeout` parameter (default 10s) but the codebase doesn't configure it explicitly. Worth making this configurable.

7. **The redis-py pin to 5.2.1** is a ticking time bomb. The pinned version avoids an asyncio.Lock contention bug in 5.3.0+, but this means you're accumulating security and performance patches you're not getting. The investigation doc (`docs/investigations/redis-py-latency-bug.md`) should be checked periodically to see if the upstream fix has landed.

---

## Questions to Drive the Discussion

1. **The polling loop for Pipecat disconnect detection** -- I noticed the 100ms sleep loop where you wait for `WebSocketState.DISCONNECTED` when using interface-based providers. At current scale this is fine, but at 10x load that's thousands of unnecessary wakeups per second. Has there been any thought about switching to an event-driven notification pattern? The Pipecat `FastAPIWebsocketTransport` has `on_client_disconnected` event handlers -- are those being leveraged?

2. **The 4700-line calls.py file** -- This is the single biggest risk surface in the service. Has there been discussion about extracting the WebSocket endpoint logic into a `CallSession` class that encapsulates setup, task group management, and cleanup? The v1/v2 duplication is particularly concerning -- a bug fixed in v2's `send_to_caller()` could easily be missed in v1's identical copy.

3. **What's the plan for the Ultravox WebSocket connection timeout?** The `websockets.connect()` call to Ultravox has no explicit timeout. If Ultravox is degraded and connections take 30+ seconds, your caller is hearing dead air. What's the intended degradation path -- drop the call? Retry? Fall back to direct patch?

4. **The audio bridge has no backpressure.** When `await bot_ws.send(decoded_audio)` blocks because Ultravox is slow, Exotel packets buffer in the kernel. Have you measured what happens when Ultravox latency spikes to 500ms+? How many Exotel packets can queue before Exotel drops the connection? A bounded async queue with a drop-oldest policy might be worth considering for the Ultravox bridge.

5. **Task leak history is concerning.** The January 29th incident showed 567 leaked sessions with 3,973 pending tasks. The fix added proper BlobManager and VoiceAgent cleanup in `delete_session()`. But the pattern of resources that need explicit `stop()` calls being silently leaked suggests there's no enforcement mechanism. Has there been thought about a `ResourceRegistry` pattern where all session-scoped resources register themselves and are automatically cleaned up?

6. **No Datadog monitors for WebSocket health.** The `WebSocketMetrics` class emits comprehensive metrics (connections opened/closed, message rates, send latency, backpressure), but there are zero monitors configured. What's the alerting strategy for WebSocket failures? If connection failure rate spikes to 10%, how quickly would the team know?

7. **The redis-py 5.2.1 pin** -- The asyncio.Lock contention bug that caused the pin is documented, but is anyone tracking the upstream fix? Redis-py 5.x has had multiple security patches since 5.2.1. What's the plan for unblocking the upgrade?

---

## Sources

- [FastAPI WebSocket Documentation](https://fastapi.tiangolo.com/advanced/websockets/)
- [Streaming Audio with WebSockets in FastAPI](https://hexshift.medium.com/top-ten-tips-for-streaming-real-time-audio-with-websockets-in-fastapi-21eaea6c7bde)
- [Pipecat FastAPIWebsocketTransport Documentation](https://docs.pipecat.ai/server/services/transport/fastapi-websocket)
- [AnyIO Task Groups Documentation](https://anyio.readthedocs.io/en/stable/tasks.html)
- [AnyIO Task Groups and Structured Concurrency - DeepWiki](https://deepwiki.com/agronholm/anyio/2.2-task-groups-and-structured-concurrency)
- [Python Structured Concurrency Guide](https://applifting.io/blog/python-structured-concurrency)
- [Pipecat GitHub Repository](https://github.com/pipecat-ai/pipecat)
- [Building Real-Time Voice AI Agents with Pipecat](https://medium.com/@ganapathinalam4/building-a-real-time-voice-ai-agent-using-pipecat-11f633fc3405)
- [FastAPI WebSocket Audio Streaming Discussion](https://github.com/fastapi/fastapi/discussions/12906)
