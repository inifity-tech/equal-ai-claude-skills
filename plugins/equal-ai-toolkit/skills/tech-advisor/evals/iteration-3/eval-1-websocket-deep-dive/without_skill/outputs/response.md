# WebSocket Handling Deep Dive: Backend Service Analysis

## Architecture Overview

The backend service manages real-time voice calls through a multi-WebSocket architecture with three distinct connection types:

1. **Caller Stream** (`/stream/caller/{session_id}` and `/stream/caller_v2/{session_id}`) -- Exotel telephony provider connects here, sending caller audio as base64-encoded media packets over a FastAPI WebSocket.

2. **User Stream** (`/stream/user/{session_id}` and `/stream/user_v2/{session_id}`) -- The mobile app user connects here to listen/talk when taking over a call from the AI.

3. **Bot WebSocket** (outbound to Ultravox) -- The server connects outward to Ultravox's API using the `websockets` library (`websockets.asyncio.client`), forming a bridge between the caller and the AI voice agent.

The data flow is: `Exotel (caller) -> FastAPI WebSocket -> Server -> Ultravox WebSocket (bot)` for caller audio, and the reverse for bot audio back to the caller.

---

## What Is Working Well

### 1. Listener Pattern for Audio Routing

The `Caller` and `User` participants use a listener/observer pattern (`Dict[str, Callable]`) that allows dynamic registration and removal of audio consumers. This is clean and flexible -- when a user patches in, the system simply removes `send_to_bot` and adds `caller_to_user` / `user_to_caller` listeners. This avoids complex state machines for call routing.

### 2. Parallel Listener Execution with asyncio.gather

In `Caller.run()`, all listeners are executed in parallel via `asyncio.gather()` with `return_exceptions=True`. This means a slow listener (e.g., blob recording) does not block the critical path (sending audio to Ultravox). The `PacketTimer` infrastructure then identifies which listener is the bottleneck.

### 3. Comprehensive Metrics and Observability

The `WebSocketMetrics` class and `PacketTimer`/`PacketTimingMetrics` infrastructure provide excellent observability:
- Per-packet end-to-end latency tracking
- Per-listener duration breakdown
- Inter-packet timing (detecting audio rate anomalies)
- Backpressure detection for both send and receive directions
- Low-cardinality tag design (avoiding session_id in metric tags)
- Fire-and-forget metrics emission to avoid blocking the audio hot path

### 4. Idempotent Connection Handling

The `try_connect_caller_stream()` call uses atomic Redis `SET NX` to prevent duplicate WebSocket connections for the same session -- a good defense against race conditions from Exotel retries.

### 5. Per-Session Locking

The `SessionManager` uses per-session `anyio.Lock` instances instead of a global lock, enabling independent sessions to run fully in parallel while serializing operations within each session. The timeout-based lock acquisition with `anyio.fail_after()` prevents indefinite hangs.

### 6. Background Task Lifecycle Management

The `Caller` participant properly tracks background tasks in `_background_tasks: set[asyncio.Task]` and cancels them with a timeout during `disconnect()`. This prevents task leaks.

---

## Issues and Concerns

### 1. Massive Endpoint Functions (Critical)

The `caller_stream` endpoint in `calls.py` is approximately 1,500 lines long as a single function. The v2 endpoint (`caller_stream_v2`) duplicates much of this logic. This creates several problems:

- **Duplication**: `AudioPacketTracker`, `_track_audio_metrics_caller_stream`, `send_to_caller`, `send_to_bot`, and the entire cleanup flow are duplicated between v1 and v2. Bug fixes must be applied in two places.
- **Testability**: The deeply nested closures (`send_to_caller`, `send_to_bot`, `disconnect_agents`, `run_caller`, `run_voice_agent`) capture state via `nonlocal` variables, making them impossible to unit test in isolation.
- **Cognitive load**: A developer modifying cleanup behavior must trace through 400+ lines of finally-block logic scattered across both `disconnect_agents()` and the `finally` block itself.

**Recommendation**: Extract the call session lifecycle into a dedicated class (e.g., `CallSession` or `CallerStreamHandler`) that encapsulates all the participant wiring, audio routing callbacks, and cleanup logic. The WebSocket endpoint should be a thin wrapper that instantiates this class and calls `await session.run()`.

### 2. Duplicated Cleanup Logic (High)

Cleanup of participants happens in two places:
1. Inside `disconnect_agents()` (called when caller or bot disconnects)
2. Inside the `finally` block of the endpoint

Both iterate over the same participants (live_transcript, live_audio, live_call, category_manager, language_manager, tool_manager, blob_manager, krisp_processor) and call `.stop()` on each. This means participants may get `.stop()` called twice. While most have guards (`is_active` checks), this is fragile and error-prone.

**Recommendation**: Implement a `ParticipantRegistry` that tracks all active participants and provides a single `cleanup_all()` method with idempotent stop semantics. Each participant registers itself on creation and is automatically cleaned up once.

### 3. Asymmetric Caller vs User Participant Design (Medium)

`Caller` has sophisticated timing infrastructure (PacketTimer, background task tracking, Krisp preprocessing, per-listener parallel execution) while `User` executes listeners sequentially:

```python
# User.run() - sequential
for _, listener in listeners_copy.items():
    await listener(user_text)

# Caller.run() - parallel
await asyncio.gather(
    *[self._timed_listener(name, listener, caller_text, timer)
      for name, listener in listeners_copy.items()],
    return_exceptions=True,
)
```

The `User` participant also lacks:
- Background task tracking and cleanup
- Packet timing/metrics
- Any observability into listener performance

**Recommendation**: Extract the parallel-listener-with-timing pattern into the base `Participant` class or a mixin. Both `Caller` and `User` should benefit from the same observability.

### 4. Fire-and-Forget Tasks Without Tracking in Closures (Medium)

Inside the `caller_stream` endpoint's closure functions, several `asyncio.create_task()` calls are made without tracking the task reference:

```python
# In send_to_caller closure (line ~1332):
asyncio.create_task(
    ws_metrics.emit_send_timing_async("exotel", inter_send_ms)
)
```

These tasks are not added to `_background_tasks` and will not be awaited or cancelled on shutdown. If the event loop is shutting down, these orphaned tasks will generate warnings. The `Caller` class itself handles this correctly, but the closure-based callbacks in the endpoint do not.

**Recommendation**: Either use a shared task tracker that both the participant and endpoint closures reference, or funnel all fire-and-forget work through the participant's task tracking mechanism.

### 5. No Heartbeat/Keepalive on Caller WebSocket (Medium)

The Ultravox agent implements `periodic_ping()` for keepalive, but the caller-side WebSocket (Exotel connection) has no heartbeat mechanism. The integration test client explicitly disables pings (`ping_interval=None`). If the network silently drops the connection, the server will hang on `await self.websocket.receive()` indefinitely until a TCP timeout (which can be minutes).

**Recommendation**: Add a configurable receive timeout on the caller WebSocket. FastAPI/Starlette WebSockets can be wrapped with `asyncio.wait_for()` or `anyio.fail_after()` to detect stale connections. Consider implementing application-level heartbeats.

### 6. No Backpressure Handling on Audio Pipeline (Medium)

When the Ultravox WebSocket or the caller WebSocket is slow to accept data, the current design will block the `await bot_ws.send()` or `await caller_ws.send_text()` call. Since listeners are executed in parallel for the `Caller`, a slow `send_to_bot` does not block other listeners, but it still ties up a slot in the `asyncio.gather()`.

There is no queue or buffering between the caller receive loop and the bot send -- audio is forwarded synchronously within the listener callback. If Ultravox experiences a transient slowdown, this will cause caller packets to queue up in the asyncio event loop, increasing memory usage and latency.

**Recommendation**: Consider adding a bounded `asyncio.Queue` between the caller receive loop and the bot send path. If the queue fills up (indicating sustained backpressure), drop the oldest audio frames rather than accumulating unbounded latency. The existing backpressure metrics (`on_audio_send_backpressure`) detect this but do not act on it.

### 7. Inconsistent Error Handling in User Participant (Low)

The `User.disconnect()` method has a redundant exception handling pattern:

```python
except RuntimeError as e:
    if 'Cannot call "send" once a close message has been sent' in str(e):
        logger.debug(f"WebSocket already closed for user: {e}")
    else:
        logger.debug(f"Error closing user websocket: {e}, as its already closed")
```

Both branches log the same message at the same level. The else branch assumes "already closed" even for unexpected RuntimeErrors. The `Caller.disconnect()` has the same issue.

**Recommendation**: Log unexpected RuntimeErrors at a higher level and avoid assuming they are always benign.

### 8. Session Stream Redis MAXLEN (Low)

The `SessionStream` uses `MAXLEN=1000` with `approximate=True` for Redis stream capping. This is reasonable as a safety net, but the comment notes P99 is 174 messages. If a session generates more than 1000 stream entries (e.g., during a long call with many state changes), older entries will be silently evicted. This could cause issues if a consumer falls behind.

---

## Pattern Assessment Summary

| Aspect | Rating | Notes |
|--------|--------|-------|
| Connection lifecycle | Good | Proper accept/close, idempotency guards |
| Audio forwarding | Good | Parallel listeners, critical-path awareness |
| Observability | Excellent | Per-packet timing, backpressure detection, low-cardinality metrics |
| Error handling | Fair | Comprehensive but inconsistent between participants |
| Code organization | Poor | 1500-line endpoints, heavy duplication between v1/v2 |
| Resource cleanup | Fair | Dual cleanup paths risk double-stop or missed cleanup |
| Backpressure | Fair | Detected but not mitigated |
| Testability | Poor | Closure-heavy design prevents unit testing of audio routing |

---

## Recommended Improvements (Priority Order)

1. **Extract CallSession class** -- Move participant wiring, audio routing callbacks, and cleanup into a testable class. This is the highest-leverage change.

2. **Unify v1/v2 endpoints** -- Share the core logic; let v2 only differ in the dependency resolution path (pre-resolved vs inline).

3. **Implement ParticipantRegistry** -- Single cleanup path, idempotent stop, automatic resource tracking.

4. **Add receive timeout on caller WebSocket** -- Detect stale connections without relying on TCP timeouts.

5. **Add bounded audio queue with drop policy** -- Protect against Ultravox slowdowns causing unbounded memory growth.

6. **Promote parallel-listener-with-timing to base class** -- Both Caller and User should have symmetric observability.

7. **Track all fire-and-forget tasks** -- Prevent orphaned tasks during shutdown.

---

## Overall Assessment

The WebSocket handling is **operationally solid** -- the metrics, timing, and observability infrastructure is notably well-designed. The audio forwarding pattern (parallel listeners, fire-and-forget metrics, critical path tracking) shows careful performance thinking. The per-session locking with timeout is a good concurrency design.

The main weakness is **code organization**. The 1500-line endpoint functions with deeply nested closures and duplicated v1/v2 paths make the system fragile to change. The dual cleanup paths (disconnect_agents + finally block) are a latent source of bugs. Extracting a proper session lifecycle class would make the already-good runtime behavior much easier to maintain, test, and evolve.
