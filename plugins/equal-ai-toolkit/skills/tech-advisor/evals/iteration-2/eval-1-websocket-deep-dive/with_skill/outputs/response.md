# WebSocket Deep Dive: Current State, Patterns, and Concerns

So I've dug through the entire WebSocket handling code in the backend service --- every major file, the architecture docs, the incident reports, and the investigation documents. I also looked at current best practices for production WebSocket audio streaming in Python. Here's what I'm seeing.

---

## Doc-Code Discrepancies

Before we dive in, I found a few places where the architecture docs don't match reality, and these are worth flagging:

1. **The HLD says PostgreSQL is "dead code"** (`backend/db/db.py`), but `pyproject.toml` includes `sqlmodel>=0.0.22`, `psycopg2-binary>=2.9.0`, and `alembic>=1.16.2` as production dependencies. There's also a `postgres_call_log_manager` import in `calls.py`. So either the DB is being used for something the docs haven't caught up with, or these are vestigial dependencies adding surface area. Worth clarifying.

2. **The HLD documents "Exotel (applet)" as DEPRECATED** and says the current flow is "entirely gRPC-driven via `LegEventProcessor`." But `calls.py` still has active `caller_stream` and `caller_stream_v2` WebSocket endpoints (lines 920 and 3233 respectively) with substantial code for the old flow. The deprecated applet endpoints are still in the router. This isn't wrong per se --- the WebSocket endpoints are still used for the audio stream --- but the doc makes it sound like everything old is dead, when in reality the v1 WebSocket endpoint is still ~1200 lines of actively maintained code running in production alongside v2.

3. **The Voice AI LLD documents the Ultravox WebSocket connection as a clean two-loop model** ("Send loop reads audio, receive loop reads messages"). In reality, the audio send path (`send_to_bot`) is a closure defined inside `caller_stream()` that gets registered as a listener on the `Caller` participant, and it runs inside `asyncio.gather()` alongside other listeners. It's not an independent loop --- it's a callback invoked per-packet inside the Caller's receive loop. This is an important architectural distinction that the docs gloss over, because it means **all listeners share the Caller's event loop tick** and any slow listener blocks audio forwarding to all others.

4. **The HLD says "ThreadPoolExecutor with 64 workers"** for LLM calls. I couldn't find this configured in the WebSocket code paths. The `LiveCallParticipant` uses `asyncio.to_thread()` for Gemini calls (line 389), which uses the default executor, but I don't see explicit pool sizing. If the default is being used, Python's default is `min(32, os.cpu_count() + 4)` --- on a typical ECS container with 2-4 vCPUs, that's 6-8 threads, not 64. This could be a bottleneck under load if many concurrent calls hit the Gemini sync SDK simultaneously.

---

## Current State: How WebSocket Handling Actually Works

The backend manages **three distinct categories of WebSocket connections** per call session, all orchestrated from a single monolithic function:

### 1. Inbound: Exotel Caller WebSocket (`/ws/{session_id}/caller`)

This is the primary connection. Exotel opens a WebSocket to the backend carrying bidirectional 8kHz PCM audio for a live phone call. The implementation lives in `backend/api/v1/calls.py` in the `caller_stream()` function, which is approximately **1300 lines long** for v1 alone (v2 starting at line 3233 is similarly sized).

The flow:
- FastAPI accepts the WebSocket
- Validates the session exists in Redis, checks idempotency via `try_connect_caller_stream()` (atomic Redis SET NX)
- Creates a `Caller` participant wrapping the WebSocket
- Creates an `UltravoxAgent` or `IVoiceAgent` (Pipecat/Gemini Native) as the voice AI backend
- Initializes 6-8 additional participants (LiveTranscript, LiveAudio, CategoryManager, LanguageManager, LiveCallParticipant, ToolManager)
- Registers closures (`send_to_bot`, `send_to_caller`, `send_to_live_transcript`, etc.) as listeners
- Launches everything in an `anyio.create_task_group()` with `tg.start_soon()`
- Handles cleanup in a `finally` block

### 2. Outbound: Ultravox Cloud WebSocket

For the Ultravox provider, the backend opens an outbound WebSocket to Ultravox's cloud using `websockets.asyncio.client.connect()`. The `UltravoxAgent` participant manages this connection with a receive loop that processes binary audio frames and JSON control messages (state changes, transcripts, tool calls).

### 3. Outbound: Gemini Live Native WebSocket

For the Gemini Native provider, `GeminiLiveNativeAgent` uses Google's Gen AI SDK to establish a live session, which internally manages a WebSocket. Three async loops run: `_audio_send_loop` (8kHz->16kHz resampling + send), `_message_receive_loop` (receive + queue), and `_message_processor_task` (dequeue + process). This is architecturally cleaner than the Ultravox path --- it decouples receive from processing using an `asyncio.Queue(maxsize=100)`.

### The Listener/Dispatch Pattern

The core audio pipeline uses a **listener pattern**. The `Caller` participant has a `dict[str, Callable]` of listeners. On each WebSocket message:

1. Acquire a lock, copy the listeners dict
2. `asyncio.gather()` all listeners in parallel
3. Track per-listener timing with `PacketTimer`

This means every audio packet (arriving at ~50 packets/second for 8kHz/20ms chunks) triggers a `gather()` of all registered listeners. For Ultravox, the listeners are: `send_to_bot`, `send_to_live_transcript`, `send_caller_audio_to_live_audio`. That's 3 coroutines gathered per packet, plus timing metrics.

---

## External Context: What Best Practices Say

### Binary frames, not text

Current best practices strongly recommend using **binary WebSocket frames** for audio data. The Exotel integration uses JSON text frames with base64-encoded audio (`{"event": "media", "media": {"payload": "<base64>"}}`). This adds ~33% overhead for base64 encoding plus JSON parsing on every packet. This is likely an Exotel protocol constraint you can't change, but it's worth noting that the `send_to_caller` path (line 1328) does `json.dumps()` + `send_text()` for every bot audio packet. The Ultravox outbound connection correctly uses binary frames (`bot_ws.send(decoded_audio_raw, text=False)`).

### Backpressure handling

This is the area where current best practices are most emphatic and where your codebase has the biggest gap. The `websockets` library handles incoming backpressure via StreamReader + bounded queues, but **outbound backpressure is entirely unmanaged**. When `send_to_caller()` calls `await caller_ws.send_text(message_str)`, if the caller's network is slow, this `await` blocks, which blocks the UltravoxAgent's listener dispatch, which means no other listeners can process that packet. There's no bounded queue, no drop-oldest strategy, no circuit breaker.

The Gemini Native agent is better here --- it uses `asyncio.Queue(maxsize=100)` for message processing, providing a natural backpressure boundary. But even there, the audio send path (`_audio_send_loop`) sends directly to the Gemini WebSocket without queuing.

### Connection lifecycle management

FastAPI's WebSocket handling is fairly basic --- there's no built-in heartbeat, no automatic reconnection, no connection quality monitoring. Your code does implement Ultravox ping/pong (30-second interval, line 924 in settings), which is good. But the Exotel caller WebSocket has **no heartbeat mechanism** --- you rely entirely on detecting `WebSocketState.DISCONNECTED` or catching `WebSocketDisconnect` exceptions.

### The `websockets` library version

Your `pyproject.toml` doesn't pin `websockets` directly --- it comes in transitively. The `websockets` library (used for the Ultravox outbound connection) has undergone significant changes. Versions 10+ restructured the API, and versions 12+ changed the default behavior around close handshakes. Given that `websockets.asyncio.client` is used explicitly, you're on at least v12, which is good.

---

## My Take

The WebSocket architecture is **functionally solid but architecturally strained**. It works, it handles the happy path well, and you've built impressive observability around it (the `PacketTimer`, `WebSocketMetrics`, per-listener timing, inter-packet timing --- this is genuinely good engineering for debugging latency issues). The recent work on per-session locks (replacing the global lock) was the right call.

However, there are several structural concerns I'd prioritize:

### 1. The Monolithic `caller_stream()` Function (Critical)

At ~1300 lines, `caller_stream()` is doing too much. It handles session setup, voice provider selection, participant initialization, listener registration, task group management, and cleanup --- all in one function with deeply nested closures that capture state via `nonlocal`. This makes it extremely difficult to reason about the lifecycle of any individual component.

The `TODO[P0]` comment at line 1916 acknowledges this: *"Refactor the code to make it modular so that adding and disconnecting new participants is easier."* This has been a known issue. The v2 endpoint duplicates most of this logic, which means bug fixes need to be applied twice (as evidenced by the WebSocket disconnect cleanup fix being applied to both `caller_stream` and `caller_stream_v2`).

### 2. Missing Backpressure on the Hot Path (High Risk at Scale)

The `send_to_caller` and `send_to_bot` closures do direct WebSocket sends without queuing. At current scale, WebSocket sends likely complete in <1ms. But at 10x load, or when a caller is on a degraded network connection, a single slow `send_text()` blocks the entire listener pipeline for that session. The lock contention investigation (`docs/investigations/audio-latency/lock-contention-rca.md`) already identified that `asyncio.gather()` latency was hitting P95=47ms with blocked time of 57ms --- and that was from lock contention in `LiveAudioParticipant`, not even from network backpressure.

The Gemini Native agent's queue-based architecture is the right pattern. The Ultravox path should adopt something similar for the caller-facing WebSocket.

### 3. Fire-and-Forget Task Accumulation (Partially Fixed)

I see the team has been actively fixing task leaks (commits `9c4a1c7d`, `69b9bca4`, `bbfa9a60`). The pattern of `asyncio.create_task()` for fire-and-forget metrics is used extensively throughout `caller.py`, `ultravox_agent.py`, and the closures in `caller_stream()`. The `_background_tasks` set with `add_done_callback(discard)` pattern is correct for preventing GC, but I noticed that in `caller_stream()` itself (lines 1332-1334, 1435-1438, 1447-1449), several `asyncio.create_task()` calls are **not tracked** in any set --- they're truly orphaned fire-and-forget tasks. If the event loop shuts down while these are pending, you'll get `Task was destroyed but it is still pending!` warnings, and more importantly, the metrics they're trying to emit will be lost silently.

### 4. The Dual v1/v2 Endpoint Problem

Having both `caller_stream` and `caller_stream_v2` with substantially duplicated logic is a maintenance hazard. Every fix needs to be applied twice. The WebSocket disconnect cleanup doc explicitly shows fixes being applied to both. Either consolidate to one endpoint or extract the shared logic into a reusable session orchestrator class.

### 5. Error Handling in the Task Group

The `except*` pattern (ExceptionGroups) at lines 2096-2116 catches `WebSocketDisconnect`, `ConnectionClosedOK`, `ConnectionClosedError`, and generic `Exception`. But when an exception occurs in one task, AnyIO cancels all other tasks in the group. This means if the voice agent crashes, the caller participant is cancelled mid-packet, and any in-flight Redis writes or S3 uploads may be interrupted. The `disconnect_agents()` function in the task handlers tries to clean up, but it's racing against cancellation from the task group.

The `finally` block (starting at line 2134) is the safety net, but it duplicates cleanup that should have already happened in `disconnect_agents()`. This dual-cleanup approach works but is fragile --- if the ordering of cleanup changes, you could get double-close errors or missed events.

---

## Questions to Drive the Discussion

1. **The `caller_stream()` monolith**: You have a P0 TODO to modularize this. Has there been any design work on what the target architecture looks like? The Gemini Native agent's approach (separate class with `prepare() -> connect() -> run() -> close()` lifecycle) seems like a good model. Would it make sense to extract a `SessionOrchestrator` class that manages the participant lifecycle, leaving the WebSocket endpoint as a thin wrapper?

2. **Backpressure on the caller WebSocket**: At your current call volume, have you seen `send_to_caller` latencies spike during degraded network conditions? The `ultravox.audio.receive_latency.ms` metric tracks this, but is anyone monitoring it? At 10x scale with, say, 4 shards each handling 50 concurrent calls, a single slow caller could cascade into degraded audio for all listeners on that session. The lock contention RCA suggests adding a queue-based ExotelAudioQueue --- is that still on the roadmap?

3. **The v1/v2 endpoint split**: What's the plan for `caller_stream` vs `caller_stream_v2`? Is v2 the active path for all new traffic, or are both serving production calls? The idempotency check implementation differs slightly between them (v1 uses `try_connect_caller_stream()`, the doc for v2 mentions checking `session.caller_stream_connected` directly). If both are active, there's a risk of behavior divergence.

4. **Task group cancellation semantics**: When the voice agent disconnects (bot hangs up), `run_voice_agent()` calls `caller.disconnect()` which closes the Exotel WebSocket. But the `run_caller()` task is likely blocked on `await self.websocket.receive()` at that point. Does AnyIO's cancellation correctly interrupt this blocking receive? Or could there be a window where the caller task is stuck? I ask because the investigation docs mention cases where the "Caller Participant run completed" log is missing.

5. **Untracked fire-and-forget tasks in closures**: Lines 1332-1334 and 1447-1449 in `caller_stream()` create tasks with `asyncio.create_task()` that aren't added to any tracking set. The `Caller` and `UltravoxAgent` classes properly track their background tasks in `_background_tasks`, but the closures in `caller_stream()` don't. Is this intentional (accepting the risk of lost metrics on shutdown), or an oversight? At scale, if these pile up, they could contribute to the task leak patterns you've been debugging.

6. **Redis as the event bus**: SessionStream does `XADD` with `maxlen=1000` to Redis streams. Under burst conditions (e.g., many concurrent sessions producing events), have you measured the Redis latency for these writes? The `redis==5.2.1` pinning note references a contention bug in 5.3.0+, suggesting Redis operations have been a pain point. If a Redis hiccup causes `send_model()` to block, it would cascade to the participants that call it --- LiveTranscript, NER, CategoryManager, etc. --- all of which run as tasks in the same task group. A slow Redis write in one participant could indirectly affect audio latency through event loop contention.

7. **The biggest architectural risk**: The fact that a single Python process handles both the real-time audio hot path (8kHz audio relay with <50ms latency target) AND multiple LLM inference calls (LiveCallParticipant Gemini calls, category detection, NER, language detection) in the same event loop is inherently fragile. The `asyncio.to_thread()` usage for Gemini helps, but the thread pool size question from the HLD discrepancy remains. At 100 concurrent calls, each with 3-4 LLM participants doing periodic inference, you could exhaust the thread pool, causing `to_thread()` calls to queue up, which blocks the event loop. Have you instrumented thread pool utilization? What's the actual pool size in production?

---

Sources:
- [FastAPI WebSockets Documentation](https://fastapi.tiangolo.com/advanced/websockets/)
- [Top Ten Tips for Streaming Real-Time Audio with WebSockets in FastAPI](https://hexshift.medium.com/top-ten-tips-for-streaming-real-time-audio-with-websockets-in-fastapi-21eaea6c7bde)
- [Backpressure in WebSocket Streams - What Nobody Talks About](https://skylinecodes.substack.com/p/backpressure-in-websocket-streams)
- [Managing WebSocket Backpressure in FastAPI Applications](https://hexshift.medium.com/managing-websocket-backpressure-in-fastapi-applications-893c049017d4)
- [Armin Ronacher: I'm Not Feeling the Async Pressure](https://lucumr.pocoo.org/2020/1/1/async-pressure/)
- [Python WebSocket Servers: Real-Time Communication Patterns (2026)](https://dasroot.net/posts/2026/02/python-websocket-servers-real-time-communication-patterns/)
- [AnyIO Task Groups and Structured Concurrency](https://anyio.readthedocs.io/en/stable/tasks.html)
- [Structured Concurrency in Python with AnyIO](https://mattwestcott.org/blog/structured-concurrency-in-python-with-anyio)
- [Pipecat FastAPIWebsocketTransport](https://docs.pipecat.ai/server/services/transport/fastapi-websocket)
