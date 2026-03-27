# Tech Advisor: WebSocket Handling Deep Dive

So I've dug through the WebSocket handling code pretty thoroughly -- read the architecture docs, the actual implementation, the incident reports, the latency investigations, and cross-referenced everything against what the community is doing in 2025-2026. Here's what I'm seeing.

---

## Doc-vs-Code Discrepancies

Before we get into the substance, I found a few places where the architecture docs don't match reality:

1. **WebSocket URL path mismatch**: The HLD and LLD-api-layer docs reference the WebSocket endpoint as `/ws/{session_id}/caller`, but the actual code in `backend/api/v1/calls.py` uses `/stream/caller/{session_id}` and `/stream/caller_v2/{session_id}`. The docs appear to reflect an older URL scheme that's been changed in code.

2. **"Binary frames carrying raw PCM audio at 8000 Hz"** -- the LLD-api-layer says this, but looking at `Caller.run()` in `backend/participants/caller.py`, the actual protocol is **JSON text frames** containing a `media` object with base64-encoded audio payloads. The Exotel protocol wraps PCM in a JSON envelope (`{"event": "media", "media": {"payload": "<base64>"}}`), not raw binary frames. This is a meaningful distinction because it means you're paying a ~33% bandwidth overhead from base64 encoding plus JSON parsing on every packet.

3. **PostgreSQL "dead code" claim**: The HLD says `backend/db/db.py` is dead code, but the `pyproject.toml` includes `sqlmodel`, `alembic`, and `psycopg2-binary` as runtime dependencies. If the DB layer is truly dead, those dependencies are dead weight in the container image. Worth confirming whether something is quietly using them or if this is accurate tech debt.

4. **V1 vs V2 endpoint documentation gap**: The architecture docs don't distinguish between `caller_stream` (v1) and `caller_stream_v2` at all. The v2 endpoint is a significant architectural change -- it requires pre-resolved dependencies and uses a different participant creation path. This undocumented divergence is a risk for new engineers.

---

## Current State: How WebSocket Handling Actually Works

The backend manages **four distinct WebSocket connections** per active call session, all defined in a single 5,136-line file (`backend/api/v1/calls.py`):

- **`/stream/caller/{session_id}`** (v1) and **`/stream/caller_v2/{session_id}`** (v2) -- Inbound from Exotel, carrying bidirectional telephony audio
- **`/stream/user/{session_id}`** and **`/stream/user_v2/{session_id}`** -- From the user's device when they pick up

The core audio flow works like this: Exotel connects a WebSocket after `leg_action("start_bot_stream")`, sending JSON-wrapped base64 PCM audio at 8kHz. The `Caller` participant runs a receive loop, dispatches each packet to registered listeners (send_to_bot, send_to_live_transcript, send_to_live_audio) via `asyncio.gather()`, and optionally applies Krisp noise suppression before dispatch. The `UltravoxAgent` (or Pipecat voice agent) receives audio from a listener callback, forwards it to the external AI service over a separate `websockets` library connection, and receives bot audio responses which get forwarded back to the Exotel WebSocket.

The v2 endpoint (`caller_stream_v2`) is the optimized path: it requires dependencies to be pre-resolved via `ParticipantDependencyService` (cutting connection setup from 800-1500ms to 50-150ms). It also uses `anyio.move_on_after()` for a hard 30-minute timeout -- a pattern the v1 endpoint doesn't have.

**Session lifecycle**: Each WebSocket endpoint function is essentially a "god function" that handles connection setup, session validation, idempotency checking (via atomic Redis SET NX), participant creation, the main processing loop (via `anyio.create_task_group`), exception group handling (`except*`), and an extensive `finally` block for cleanup of all participants, WebSocket connections, audio recordings, and Krisp processors.

The participant model is well-designed in principle -- `Caller`, `UltravoxAgent`, `SarvamStreamingSTT`, `LiveTranscript`, `NER`, `CategoryManager`, `LanguageManager`, `LiveCall`, `LiveAudio`, `ToolManager` all run as concurrent tasks in the task group, communicating via listener callbacks. This is a solid pattern for extensibility.

**Key dependencies and versions**:
- FastAPI `>=0.115.8` (current stable is 0.128.0 -- a few versions behind)
- `websockets` library (used for outbound connections to Ultravox/Sarvam)
- `anyio` for structured concurrency (task groups, locks, timeouts)
- `redis==5.2.1` (deliberately pinned due to asyncio.Lock contention bug in 5.3.0+)
- `pipecat-ai>=0.0.95` for the Pipecat voice pipeline variant

---

## External Context: What the Latest Thinking Says

**The `asyncio.gather()` pattern for audio dispatch is a known anti-pattern for latency-sensitive paths.** Your own investigation (`docs/investigations/audio-latency/backend-pipeline-analysis.md`) identified this: mean end-to-end latency of 72.3ms per packet with 57ms of blocked time (78.8% overhead). The problem is that `gather()` waits for ALL listeners to complete before the next packet can be processed, meaning the slowest listener gates everything. For real-time audio at 8kHz (a packet every ~20ms), 72ms of processing per packet means you're falling behind 3-4x.

The recommended pattern for real-time audio fanout is **fire-and-forget dispatch** where each listener gets its own bounded queue, and the main receive loop never blocks waiting for listeners to process. The `websockets` library (which you're already using for outbound connections) has built-in backpressure handling that propagates correctly to TCP -- it's one of the few Python WebSocket libraries that does this right.

**The 5,136-line god file is a structural risk.** `calls.py` contains all four WebSocket endpoints plus all the REST endpoints, each WebSocket endpoint being 1000+ lines. The current recommended pattern for real-time voice systems (seen in Twilio/OpenAI integrations, Pipecat's own `FastAPIWebsocketTransport`, and similar systems) is to separate the WebSocket transport layer from the participant orchestration layer. Your `ConversationFlowHandler` in `backend/domain/calls/flows/conversation.py` exists but only handles the HTTP/gRPC setup flow -- the WebSocket processing logic hasn't been extracted.

**Backpressure handling is missing.** When the Exotel WebSocket sends audio faster than listeners can process, or when Ultravox sends bot audio faster than the Exotel WebSocket can accept, there's no explicit flow control. The `asyncio.gather()` with `return_exceptions=True` silently swallows listener errors, meaning a persistently slow listener won't crash anything but will degrade latency for all other listeners. The industry recommendation is bounded per-consumer queues with explicit load shedding for non-critical consumers (e.g., drop audio packets for NER/category detection before dropping them for the voice AI).

**WebSocket reconnection for outbound connections**: The `UltravoxAgent` uses `websockets.asyncio.client.connect()` without any reconnection logic. If the Ultravox WebSocket drops mid-call, the `ConnectionClosed` exception breaks out of the receive loop and triggers disconnect. There's no retry or reconnection attempt. The `websockets` library v14+ supports automatic reconnection via `async for` patterns, which you're not using.

---

## My Take

The core architecture -- participant model, listener pattern, session management split between Redis (shared state) and in-memory (live participants), sharding via MD5 -- is solid and well-thought-out. The fact that you've successfully A/B tested between Ultravox (cloud) and Pipecat (local pipeline) voice providers through a unified interface shows good abstraction.

Where things get shaky is in the implementation details of the hot path. The `asyncio.gather()` bottleneck in the audio dispatch loop is the biggest latency concern -- you've already identified it in your investigations but haven't resolved it. The god-file problem isn't just aesthetics; it makes it genuinely hard to reason about cleanup correctness (the `finally` blocks are 100+ lines of defensive try/except) and creates merge conflict hell when multiple engineers work on different WebSocket-related features.

The v1/v2 split is pragmatic but concerning long-term. Having two full implementations of the caller stream means bug fixes need to be applied twice, and the divergence will grow. The v2 endpoint with pre-resolved dependencies is clearly better -- I'd prioritize migrating fully to v2 and removing v1.

If I were prioritizing improvements, I'd focus on: (1) replacing `asyncio.gather()` with per-listener queues in the audio hot path, (2) extracting the WebSocket endpoint logic into a proper session handler class, and (3) adding structured backpressure handling for the audio pipeline.

---

## Questions to Drive the Discussion

1. **The `asyncio.gather()` bottleneck -- what's the status?** Your backend pipeline analysis from January 2026 identified 72ms mean latency with 78.8% blocked time. The document lists recommendations but I don't see evidence they were implemented. Is the team still experiencing choppy audio in production? What's the current P99 packet processing time?

2. **Why are v1 and v2 both still alive?** The v2 endpoint is strictly better (faster connection, has timeout enforcement, uses pre-resolved dependencies). Is there a specific reason v1 hasn't been deprecated? Are there Exotel configurations or call flows that still hit v1? Every day both exist is a day where a bug fix might miss one of them -- like the `WebSocketDisconnect` handling fix that had to be applied to both.

3. **What happens when Ultravox drops the WebSocket mid-call?** Looking at `UltravoxAgent.run()`, a `ConnectionClosed` exception triggers `disconnect()` which sends a `BotDisconnected` event and closes the socket -- but the caller is still connected and hears silence. Is that the intended degradation? Have you considered reconnecting to Ultravox and resuming the conversation, or at least playing a fallback message via the Exotel WebSocket?

4. **The listener model has a subtle priority inversion problem.** `Caller.run()` dispatches to all listeners in parallel via `gather()`, but all listeners share the same event loop. A slow NER or category detection LLM call (even if it's off the critical path via `asyncio.to_thread()`) still consumes an event loop slot during `gather()`. Have you measured whether the `send_to_bot` listener (the latency-critical path) is being delayed by non-critical listeners? The packet timing metrics should show this.

5. **The `send_to_caller` race condition -- is the two-layer fix holding?** The `docs/websocket-disconnect-cleanup-issue.md` describes a race where `send_to_caller()` tries to write to a closed Exotel WebSocket, causing `WebSocketDisconnect` to propagate to the task group. The fix catches `WebSocketDisconnect` at the source plus a failsafe in `finally`. But I notice the `send_to_caller` function checks `caller_ws.client_state != WebSocketState.DISCONNECTED` before sending -- this is a TOCTOU race. Between the state check and the `send_text()`, the WebSocket can transition to disconnected. Is this still happening in production logs?

6. **No heartbeat/keepalive on the Exotel WebSocket.** The `UltravoxAgent` has `periodic_ping()` for latency monitoring, but the `Caller` participant doesn't send or expect any keepalive on the Exotel WebSocket. If Exotel's side goes silent (network partition, load balancer timeout), you'd only detect it when the next `receive()` hangs indefinitely. The `Caller.run()` loop has no timeout on `await self.websocket.receive()`. Have you seen zombie sessions where the caller disconnected but the backend didn't notice?

7. **The 5,136-line calls.py -- is there a plan to break it up?** I see `ConversationFlowHandler` exists for the HTTP/gRPC setup path, but the WebSocket processing logic is still monolithic. The v2 endpoint alone is ~1,800 lines. Extracting a `CallerStreamHandler` class (setup, processing loop, cleanup) would make the cleanup logic testable in isolation, which right now it isn't -- the `finally` block is coupled to 20+ closure variables from the outer scope.

Sources:
- [WebSocket Best Practices - WebSocket.org](https://websocket.org/guides/best-practices/)
- [Backpressure in WebSocket Streams](https://skylinecodes.substack.com/p/backpressure-in-websocket-streams)
- [Streaming Audio with WebSockets in FastAPI](https://hexshift.medium.com/top-ten-tips-for-streaming-real-time-audio-with-websockets-in-fastapi-21eaea6c7bde)
- [FastAPI WebSocket Documentation](https://fastapi.tiangolo.com/advanced/websockets/)
- [Python WebSocket Best Practices 2025](https://www.videosdk.live/developer-hub/websocket/python-websocket-library)
- [WebSocket Best Practices for Production - LatteStream](https://lattestream.com/blog/websocket-best-practices)
- [Pipecat FastAPIWebsocketTransport](https://docs.pipecat.ai/server/services/transport/fastapi-websocket)
