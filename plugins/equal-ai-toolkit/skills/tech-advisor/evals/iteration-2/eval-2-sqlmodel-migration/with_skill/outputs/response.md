# SQLModel vs Raw SQLAlchemy: Should You Migrate?

So I've spent a good amount of time digging through all five of your database-backed services, reading every model file, every repository, every manager, the db.py/database.py session setup files, and the architecture LLD docs. I've also looked at the latest state of SQLModel and SQLAlchemy in the ecosystem. Here's what I'm seeing -- and honestly, the picture is more nuanced than I expected going in.

---

## Doc-Code Discrepancies (Before We Dive In)

A few things I noticed where the architecture docs don't match reality:

1. **User-services LLD says pool_size=30, max_overflow=50.** The actual `app/db.py` code has `pool_size=_settings.database_pool_size` (defaulting to 40) and `max_overflow=_settings.database_max_overflow` (defaulting to 60). The doc is stale -- total max is 100, not 80 as documented.

2. **The user-services LLD references `DBTranscriptManager` as "sync only"**, but `app/db_transcript_manager.py` uses `session.execute()` from SQLAlchemy (not `session.exec()` from SQLModel) for its core operations -- it's actually using raw SQLAlchemy insert/select/delete statements already. This is a pattern worth noting for our discussion.

3. **Memory-service docs say "all PostgreSQL access goes through typed manager classes"**, but `CallerMemoryManager` manages its own sessions internally (short-lived connections opened and closed per method call, not injected). This is actually the right pattern for that service's LLM-heavy workload, but the doc framing is misleading.

---

## Current State: You're Already Running a Hybrid

This is the key insight that changes the entire framing of the question. You're not actually choosing between SQLModel and SQLAlchemy -- **you're already using both, deeply interleaved, across every service**. Let me show you exactly what I mean.

### What SQLModel Actually Gives You Today

Across all five services, SQLModel is being used for exactly these things:

1. **Model definitions** (`class CallLog(SQLModel, table=True):`) -- the `SQLModel` base class for table models
2. **`Session` import** -- `from sqlmodel import Session` (sync only)
3. **`session.exec()`** -- the SQLModel-specific query runner method (sync managers only)
4. **`select()` import** -- some files import `select` from sqlmodel, others from sqlalchemy
5. **`Field()`** -- for model field definitions

### What SQLAlchemy Gives You Today

Meanwhile, you're importing heavily from SQLAlchemy directly:

- **All column types**: `Column, DateTime, JSON, String, Boolean, Integer, Index, UniqueConstraint` from `sqlalchemy`
- **All async infrastructure**: `create_async_engine, AsyncSession, async_sessionmaker` from `sqlalchemy.ext.asyncio`
- **All query building**: `select, text, and_, desc, asc, func, or_, insert, delete, update` from `sqlalchemy`
- **PostgreSQL-specific types**: `UUID` from `sqlalchemy.dialects.postgresql`
- **ORM utilities**: `attributes.flag_modified`, `Relationship` kwargs use SQLAlchemy's `sa_relationship_kwargs`
- **Connection pooling**: All pool configuration is pure SQLAlchemy (`pool_size`, `max_overflow`, `pool_pre_ping`, `pool_recycle`, `pool_use_lifo`)

### The Numbers Tell the Story

In user-services alone:
- **102 occurrences** of `session.exec()` (SQLModel's query API) across 18 files
- **35 occurrences** of `session.execute()` (SQLAlchemy's native API) across 11 files
- Almost every model file imports from **both** `sqlmodel` and `sqlalchemy`

In memory-service, the split is even more stark: the repository layer (`postgres_caller_memory_repository.py`) uses **pure SQLAlchemy** query patterns (`session.execute()`, `result.scalars().all()`, `result.scalar_one_or_none()`) with models that happen to inherit from `SQLModel`.

In post-processing-service, the `DBRepository` uses `session.exec()` (SQLModel) but the `BaseRepository` in memory-service uses `session.execute()` (SQLAlchemy). Same concept, different APIs, because there's no consistent standard across services.

### What This Means

**SQLModel is essentially acting as a model definition layer** -- a thin veneer over SQLAlchemy that gives you Pydantic-compatible table classes. The actual query running, session management, connection pooling, async support, and advanced features are all pure SQLAlchemy already.

The question isn't really "should we migrate from SQLModel to raw SQLAlchemy" -- it's more like **"should we stop using SQLModel's model base class and session.exec() wrapper, given that we're already 70% on raw SQLAlchemy?"**

---

## What You'd Actually Gain from Migrating

### 1. Elimination of the `sa_column` Tax (Moderate Value)

This is the most tangible pain point. Look at nearly every model in your codebase:

```python
# Current: SQLModel with sa_column escape hatches everywhere
class CallLog(SQLModel, table=True):
    created_on: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime, default=datetime.utcnow, nullable=False),
    )
    initial_call_context: dict | None = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )
```

Every time you need a `JSON` column, a `DateTime` with `onupdate`, a PostgreSQL `UUID`, or any non-trivial column type, you're dropping down to `sa_column=Column(...)`. This means you're writing the type definition twice -- once for Python/Pydantic (`dict | None`) and once for SQLAlchemy (`Column(JSON, nullable=True)`).

With raw SQLAlchemy 2.0's `Mapped` annotations:

```python
# What it would look like with SQLAlchemy 2.0 Mapped
class CallLog(Base):
    __tablename__ = "calllog"
    created_on: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    initial_call_context: Mapped[dict | None] = mapped_column(JSON)
```

This is cleaner, yes, but it's not dramatically less code. The real win is that you stop having two parallel type systems fighting each other.

### 2. Better Type Checking (High Value for Your Codebase)

Look at memory-service's `pyproject.toml` -- it has **27 pyright rules disabled**. That's not normal. A significant chunk of those suppressions exist because SQLModel's type system doesn't play well with pyright:

- `reportUnknownParameterType = false`
- `reportUnknownArgumentType = false`
- `reportUnknownMemberType = false`
- `reportAttributeAccessIssue = false`
- `reportArgumentType = false`
- `reportReturnType = false`
- `reportAssignmentType = false`

SQLModel's `Field()` function has known type-checking issues with pyright -- the `sa_column` parameter, `UndefinedType`, and various overloads create a mess that forces you to either suppress errors globally or litter `# type: ignore` comments throughout your code. This is a well-documented community issue (see GitHub discussions #828, #797, #1228 on the sqlmodel repo).

SQLAlchemy 2.0's `Mapped[]` + `mapped_column()` was specifically designed with type checkers in mind and works properly with pyright out of the box.

### 3. Consistent Async Story (High Value)

This is where the current codebase has the most friction. SQLModel doesn't have its own async session -- you're already using SQLAlchemy's `AsyncSession` everywhere:

```python
# memory-service database.py -- no SQLModel in the async path
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel  # Only used for SQLModel.metadata.create_all
```

Your async managers (e.g., `DBCallerManagerAsync`) accept `AsyncSession` from SQLAlchemy, use `session.execute()` from SQLAlchemy, and only reference SQLModel for the model class. If you migrated models to `DeclarativeBase`, the entire async path would be consistent.

### 4. No More Import Confusion (Low-Medium Value)

Right now, developers have to remember:
- Import `select` from `sqlmodel` for sync `session.exec()` calls
- Import `select` from `sqlalchemy` for async `session.execute()` calls
- Import `Session` from `sqlmodel` for sync
- Import `AsyncSession` from `sqlalchemy.ext.asyncio` for async
- `user-services/app/db_user_manager.py` even imports both: `from sqlalchemy import select` AND `from sqlmodel import select as sqlmodel_select`

This is a constant source of confusion and bugs. With pure SQLAlchemy, there's one import path for everything.

---

## What You'd Lose (or Risk)

### 1. Pydantic Model Integration (The Biggest Concern)

SQLModel models are simultaneously Pydantic models. If you use them as FastAPI response models or for validation anywhere, you'd need to either:
- Create separate Pydantic schemas alongside your SQLAlchemy models (more code, but better separation)
- Use SQLAlchemy's built-in `dataclass` integration (newer, less mature)

Looking at your code, I see `CallLog` has a `to_call_log_details()` method that manually converts to a response model. `ProcessedSessionContext` has a `Config` with `json_encoders`. This suggests you're **already not relying heavily on SQLModel's Pydantic integration** for API serialization -- you have dedicated response models.

However, memory-service's `MemoryData` uses `model_config = ConfigDict(arbitrary_types_allowed=True)`, which is a Pydantic config on a SQLModel table. You'd need to verify whether anything calls `.model_dump()` or `.model_validate()` on table models.

### 2. Migration Effort vs. Risk

This is a **large, cross-cutting change** touching every service. The models need to change, every `session.exec()` needs to become `session.execute()` (with slightly different result handling), and Alembic migrations need to use the new metadata. That's 193 files with SQLModel references. Not all need changing, but it's a lot of surface area for bugs.

The risk-reward here depends entirely on: are you hitting actual pain from SQLModel today, or is this a "cleanliness" motivation?

### 3. SQLModel Is Still Actively Developed

SQLModel 0.0.24 (memory-service and evaluations) and 0.0.22 (backend, user-services, PPS) are the current versions. The library is on version 0.0.x, which signals it's still pre-1.0, but it's maintained by the same creator as FastAPI and has a clear roadmap. That said, the async documentation is still marked as "gradually growing" in the official docs -- a worrying gap for production codebases.

---

## My Take

Here's where I land: **Don't do a big-bang migration. Instead, standardize the direction and migrate incrementally.**

The strongest argument against migrating is the effort-to-risk ratio. You have 5 services in production, 193 files touching SQLModel, and the system is working. A wholesale migration introduces risk for a codebase that's functional.

But the strongest argument *for* migrating is that **you're already maintaining the complexity of two ORMs without getting the benefit of either one fully.** The hybrid state is the worst of both worlds:
- You don't get SQLModel's clean ergonomics (because you need `sa_column` everywhere)
- You don't get SQLAlchemy's type safety (because you suppress pyright errors to make SQLModel work)
- You confuse developers with inconsistent patterns (exec vs execute, sqlmodel.select vs sqlalchemy.select)

**My recommendation: Freeze new code on SQLAlchemy patterns, migrate models incrementally per-service.**

Concretely:
1. **Immediately**: For any new code, use `session.execute()` (not `session.exec()`), import `select` from `sqlalchemy`, and stop adding new `sa_column` hacks.
2. **Next service refactor**: When you next do significant work on a service, migrate its models to `DeclarativeBase` + `Mapped`. Memory-service is the best candidate -- it's already 90% SQLAlchemy and has the worst pyright situation.
3. **Don't touch stable services**: Backend service and PPS are working fine. Don't migrate them unless you're already in there for other reasons.
4. **Create a shared base**: Define your base model pattern once (with UUID PK, created_at, updated_at) as a `DeclarativeBase` subclass and use it across new services.

---

## Scale and Performance Considerations

A few things I noticed that are worth flagging regardless of which ORM layer you choose:

### Connection Pool Sizing Inconsistency

- **Backend**: `pool_size=40, max_overflow=60` (total 100), `pool_timeout=30`
- **User-services**: `pool_size=40, max_overflow=60` (total 100), `pool_timeout=10`
- **PPS**: `pool_size=20, max_overflow=40` (total 60), `pool_recycle=3600`
- **Memory-service**: configurable via settings, `pool_recycle=1800`, `pool_use_lifo=True`

The PPS service has a 3600s pool_recycle (1 hour) vs memory-service's 1800s (30 min) and user-services' 300s (5 min). At scale, the PPS connections could go stale between RDS maintenance windows or NAT gateway timeouts. The user-services pattern with the "three-layer defense" (TCP keepalive + pool recycle + pre-ping) is the most production-hardened.

### The PPS Sync-Async Mismatch

The PPS `DBRepository` declares async methods (`async def save`, `async def batch_save`) but uses synchronous `self.db_session.commit()`, `self.db_session.refresh()`, etc. These are blocking calls. If PPS runs behind an async FastAPI server, these synchronous ORM calls will block the event loop during database I/O. Under concurrent load, this creates head-of-line blocking where one slow commit stalls all other requests in the same async task group.

This is a bigger issue than the SQLModel-vs-SQLAlchemy question and should be addressed regardless.

### The `flag_modified` Pattern

Both `db_call_log_manager.py` and `db_caller_manager_async.py` use `attributes.flag_modified(entity, "field_name")` to force SQLAlchemy to detect changes to JSON/dict fields. This is a well-known SQLAlchemy gotcha with mutable column types. Neither SQLModel nor raw SQLAlchemy "solves" this -- but SQLAlchemy provides `MutableDict.as_mutable(JSON)` as a column type wrapper that automates change detection. If you migrate models, that's an opportunity to adopt it.

---

## Questions to Drive This Deeper

1. **Are you actually hitting pain from SQLModel today?** I see the evidence of friction (27 disabled pyright rules, dual imports, `sa_column` everywhere), but is this causing bugs or just developer annoyance? The answer changes the urgency.

2. **What's happening with your Pydantic model boundary?** I see `CallLog.to_call_log_details()` suggests you have separate response models, but are there places where table models are passed directly as FastAPI response types? If so, that's the hardest thing to untangle in a migration and needs mapping first.

3. **The PPS `DBRepository.save()` method is async but calls `self.db_session.commit()` synchronously** (it uses `sqlmodel.Session`, not `AsyncSession`). That's a blocking call on the event loop. Is this a known issue? It looks like it could cause latency spikes under load when the PPS service is processing multiple sessions concurrently.

4. **Memory-service's `CallerMemoryManager` pattern of opening/closing sessions per method call** -- have you measured the connection establishment overhead? With `pool_pre_ping=True` and `pool_recycle=1800`, each method call is potentially paying for a validation round-trip. The `warm_connection_pool` at startup helps, but at burst load with many concurrent LLM-driven operations, you could be churning through connections faster than the pool can recycle them.

5. **What's the plan for the user-services async migration?** You have sync+async manager pairs for 9 entities. Are you migrating toward full async, or will you maintain both indefinitely? If full async is the goal, that's the natural point to also switch the model base class.

6. **Have you considered the hybrid-standardization approach?** Instead of migrating models, you could keep SQLModel for definitions but standardize all query usage on `session.execute()` and stop using `session.exec()`. This is less disruptive and gets you 80% of the consistency benefits without touching model definitions.

7. **The conversation manager (`db_conversation_manager.py`) drops to raw SQL `text()` for the conversations list query.** That's a sign that the ORM -- whether SQLModel or SQLAlchemy -- isn't expressive enough for your window function needs. How many more of these raw SQL escape hatches exist, and does that change the calculus of which ORM layer you invest in?

---

## Key Files Referenced

| File | Service | What It Shows |
|------|---------|--------------|
| `myequal-ai-user-services/app/db.py` | User Services | Dual sync+async engine setup, pool config |
| `myequal-ai-user-services/app/models/call_log.py` | User Services | Typical SQLModel model with heavy sa_column usage |
| `myequal-ai-user-services/app/models/user.py` | User Services | SQLModel with Relationships and sa_relationship_kwargs |
| `myequal-ai-user-services/app/db_call_log_manager.py` | User Services | Sync manager using session.exec() |
| `myequal-ai-user-services/app/db_caller_manager_async.py` | User Services | Async manager using pure SQLAlchemy |
| `myequal-ai-user-services/app/db_base_manager.py` | User Services | Custom transaction context manager |
| `myequal-ai-user-services/app/db_conversation_manager.py` | User Services | Raw SQL text() for window functions |
| `myequal-ai-user-services/app/db_user_manager.py` | User Services | Dual select imports from both libraries |
| `memory-service/app/database.py` | Memory Service | Pure SQLAlchemy async with SQLModel metadata |
| `memory-service/app/models/memory.py` | Memory Service | SQLModel with heavy SQLAlchemy column types |
| `memory-service/app/models/base.py` | Memory Service | Base model pattern |
| `memory-service/app/db/memory_data_context.py` | Memory Service | MemoryData model with ConfigDict |
| `memory-service/app/repositories/postgres_caller_memory_repository.py` | Memory Service | Pure SQLAlchemy query patterns |
| `memory-service/app/repositories/base.py` | Memory Service | Generic async base repo using SQLAlchemy |
| `memory-service/pyproject.toml` | Memory Service | 27 disabled pyright rules |
| `myequal-post-processing-service/app/database/session.py` | PPS | Sync SQLModel session setup |
| `myequal-post-processing-service/app/database/db_repository.py` | PPS | Async methods with sync session (mismatch) |
| `myequal-post-processing-service/app/database/base_repository.py` | PPS | Abstract async repository interface |
| `myequal-post-processing-service/app/database/models/processed_session_context.py` | PPS | Large model with many sa_column fields |
| `myequal-ai-backend/backend/db/db.py` | Backend | Sync-only SQLModel session setup |
| `myequal-ai-backend/backend/models/call_log.py` | Backend | Simpler SQLModel model |
| `myequal-evaluations/app/database.py` | Evaluations | Database setup |
| `myequal-ai-user-services/docs/architecture/LLD-data-layer.md` | User Services | Data layer architecture doc |
| `memory-service/docs/architecture/LLD-data-layer.md` | Memory Service | Data layer architecture doc |

---

## Sources

- [SQLModel Official Documentation](https://sqlmodel.tiangolo.com/)
- [SQLModel vs SQLAlchemy Choices with Real Benchmarks](https://medium.com/@sparknp1/10-sqlmodel-vs-sqlalchemy-choices-with-real-benchmarks-dde68459d88f)
- [SQLModel vs SQLAlchemy: Navigating the Python ORM Landscape](https://www.oreateai.com/blog/sqlmodel-vs-sqlalchemy-navigating-the-python-orm-landscape/44a103f16d0e599638e9216570f41037)
- [SQLModel Async Support - DeepWiki](https://deepwiki.com/fastapi/sqlmodel/5.4-async-support)
- [Async Without Tears: 10 Patterns for asyncpg + SQLModel](https://medium.com/@bhagyarana80/async-without-tears-10-patterns-for-asyncpg-sqlmodel-72c68aa68f0d)
- [SQLModel Roadmap - GitHub Issue #654](https://github.com/fastapi/sqlmodel/issues/654)
- [SQLModel Pyright Type Checking Issues - GitHub Discussion #828](https://github.com/fastapi/sqlmodel/discussions/828)
- [SQLModel Field Type Errors - GitHub Discussion #1598](https://github.com/fastapi/sqlmodel/discussions/1598)
- [SQLModel Release Notes](https://sqlmodel.tiangolo.com/release-notes/)
- [Best Practices for FastAPI with SQLModel - GitHub Discussion #9936](https://github.com/fastapi/fastapi/discussions/9936)
- [Using SQLModel Asynchronously with FastAPI](https://daniel.feldroy.com/posts/til-2025-08-using-sqlmodel-asynchronously-with-fastapi-and-air-with-postgresql)
