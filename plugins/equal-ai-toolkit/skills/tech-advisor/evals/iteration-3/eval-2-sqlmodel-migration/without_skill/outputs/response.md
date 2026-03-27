# SQLModel to Raw SQLAlchemy Migration Analysis

## Current State of SQLModel Usage

After analyzing the four services, here is what I found:

### Scope of Usage

**Total SQLModel table models across the four services:**
- **myequal-ai-user-services**: ~20 models (User, CallLog, CallTranscript, UserDevice, Exophone, VerificationSession, Caller, etc.)
- **memory-service**: ~6 models (StaticCallerMemory, BackfillJob, CallerInfoHistory, MemoryData, ProcessedSessionContext, CallerNameVolatilityMetricData)
- **myequal-post-processing-service**: ~4 models (ProcessedSessionContext, BatchProcessingJob, BatchProcessingJobItem, PostProcessingConfig)
- **myequal-ai-backend**: 1 model (CallLog)

That is roughly **31 table models** across all services.

### What SQLModel Features You Actually Use

**1. Model definitions (`SQLModel, table=True`)** -- This is the primary usage. Every model inherits from `SQLModel` and uses `Field()` for column definitions. Example from the backend's `CallLog`:

```python
class CallLog(SQLModel, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(...)
    session_id: uuid.UUID = Field(..., unique=True)
```

**2. `Relationship()` declarations** -- Used in user-services for User -> UserDevice, User -> Exophone, User -> VerificationSession, UserDevice -> SimCarriers linkages. This is SQLModel's wrapper around SQLAlchemy's `relationship()`.

**3. `Session` import** -- The backend and PPS use `sqlmodel.Session` (which is a thin wrapper around `sqlalchemy.orm.Session`). Memory-service and user-services async paths use SQLAlchemy's `AsyncSession` directly.

**4. `create_engine`** -- Imported from sqlmodel in backend and PPS, but this is literally a re-export of SQLAlchemy's `create_engine`.

**5. Base model inheritance** -- Memory-service has a `BaseModel(SQLModel, table=False)` pattern for shared fields.

### What SQLModel Features You Do NOT Use

- **No Pydantic read/create schemas**: You are not using SQLModel's signature feature of creating `table=False` Pydantic models that share fields with table models. Your API response models are separate Pydantic `BaseModel` classes.
- **No `model_validate` on SQLModel classes for API input**: The `.model_dump()` calls are on regular Pydantic models, not SQLModel table models.
- **No SQLModel-specific query API**: All queries use SQLAlchemy's `select()`, `insert()`, `delete()`, `text()`, `and_()`, `func`, `desc`, etc. directly. Every single manager file imports query constructs from `sqlalchemy`, not from `sqlmodel`.

### Heavy SQLAlchemy Direct Usage Already

This is the most telling finding. Your codebase already uses raw SQLAlchemy extensively:

- **Column types**: `Column`, `DateTime`, `JSON`, `Index`, `UniqueConstraint`, `String`, `Boolean` -- all imported from `sqlalchemy`
- **Dialect-specific features**: `sqlalchemy.dialects.postgresql` for `UUID`, `insert` (upsert)
- **Async infrastructure**: `create_async_engine`, `AsyncSession`, `async_sessionmaker` -- all from `sqlalchemy.ext.asyncio`
- **ORM utilities**: `sqlalchemy.orm.attributes.flag_modified`, `sqlalchemy.orm.Session`
- **`sa_column` overrides**: Many fields use `sa_column=Column(...)` to get SQLAlchemy behavior that SQLModel's `Field()` cannot express natively

The `sa_column` pattern is particularly revealing. When you write:
```python
created_on: datetime = Field(
    default_factory=datetime.utcnow,
    sa_column=Column(DateTime, default=datetime.utcnow, nullable=False),
)
```
...you are bypassing SQLModel's abstraction entirely and writing raw SQLAlchemy column definitions. This happens on virtually every timestamp field and every JSON field across all services.

## Analysis: Should You Migrate?

### Arguments FOR migrating to raw SQLAlchemy

**1. You are already 80% there.** Your query layer is pure SQLAlchemy. Your session management is pure SQLAlchemy. Your async infrastructure is pure SQLAlchemy. SQLModel is only used for model class definitions, and even those lean heavily on `sa_column` overrides.

**2. Removes a dependency that adds complexity without proportionate value.** SQLModel sits between your code and SQLAlchemy, and its type system interacts awkwardly with SQLAlchemy's. The `# type: ignore` comments scattered across the codebase (51 occurrences in user-services alone, 17 in memory-service) are partly caused by SQLModel's incomplete type stubs for SQLAlchemy column expressions like `CallLog.session_id == session_id`.

**3. SQLModel's maintenance cadence is a risk.** SQLModel is maintained primarily by one person (the FastAPI author). SQLAlchemy 2.0 has excellent typing support built-in, and SQLModel sometimes lags behind SQLAlchemy releases.

**4. Eliminates the `sa_column` antipattern.** In raw SQLAlchemy, you would define columns naturally with `mapped_column()` instead of the awkward `Field(sa_column=Column(...))` double-wrapping.

**5. Better async story.** SQLAlchemy 2.0's `MappedAsBase` and `mapped_column` work seamlessly with async sessions. You are already using `AsyncSession` everywhere in memory-service and user-services.

### Arguments AGAINST migrating

**1. It works today.** There are no reported bugs or performance issues caused by SQLModel itself. The abstraction cost is mostly aesthetic.

**2. Migration risk across 31 models in 4 production services.** Each model change requires verifying that Alembic does not generate spurious migration diffs. SQLAlchemy's `DeclarativeBase` defines columns differently than SQLModel, and Alembic's autogenerate may detect phantom changes (column type differences, default changes, etc.). This requires careful testing per-service.

**3. Relationship definitions would need rewriting.** User-services has ~10 `Relationship()` declarations that would need to become SQLAlchemy `relationship()` calls with explicit `Mapped[]` type annotations.

**4. Team familiarity.** If the team is comfortable with SQLModel's `Field()` syntax, switching to `mapped_column()` is a learning curve, even if modest.

**5. No blocking issue.** SQLModel 0.0.22-0.0.24 (your current versions) works with SQLAlchemy 2.x. There is no immediate compatibility pressure.

## My Recommendation

**Do not migrate all at once. The cost-benefit does not justify a big-bang rewrite.**

However, I would recommend a pragmatic middle path:

**1. For new services or major rewrites**: Use raw SQLAlchemy 2.0 with `DeclarativeBase` and `mapped_column()`. There is no reason to add the SQLModel dependency to new code given how little of its unique value you use.

**2. For the backend service (1 model)**: This is the easiest candidate if you want to try it. One model, no relationships, already using `sa_column` everywhere. Low risk, good learning exercise.

**3. For existing services**: Leave them on SQLModel unless you are doing a significant refactor of the data layer anyway. The `# type: ignore` comments are annoying but not harmful.

**4. Freeze the SQLModel version**: Pin to a specific version (e.g., `sqlmodel==0.0.22`) rather than `>=` to avoid surprise breakage from upstream changes.

### What a Single Model Migration Would Look Like

For reference, here is what the backend's `CallLog` would look like in raw SQLAlchemy 2.0:

```python
# Before (SQLModel)
from sqlmodel import Field, SQLModel
from sqlalchemy import Column, DateTime, Index

class CallLog(SQLModel, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(...)
    session_id: uuid.UUID = Field(..., unique=True)
    created_on: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime, default=datetime.utcnow, nullable=False),
    )

# After (SQLAlchemy 2.0)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import DateTime, Index, Uuid

class Base(DeclarativeBase):
    pass

class CallLog(Base):
    __tablename__ = "call_log"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, unique=True, nullable=False)
    created_on: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
```

The SQLAlchemy 2.0 version is cleaner, has proper type inference (no `# type: ignore` needed for `.where()` clauses), and does not require the `sa_column` escape hatch.

### Migration Effort Estimate

| Service | Models | Relationships | Effort | Risk |
|---------|--------|---------------|--------|------|
| myequal-ai-backend | 1 | 0 | ~1 hour | Low |
| myequal-post-processing-service | 4 | 0 | ~3 hours | Low-Medium |
| memory-service | 6 | 0 | ~4 hours | Medium |
| myequal-ai-user-services | 20 | ~10 | ~2-3 days | Medium-High |

The effort includes updating models, verifying Alembic generates no spurious diffs, updating imports across all manager/repository files, and running the full test suite. User-services is the riskiest due to the relationship declarations and the sheer number of manager files (20+) that import from SQLModel.

## Summary

SQLModel gives you two things: (1) a combined Pydantic + SQLAlchemy model class, and (2) a slightly simpler API for simple cases. You are not using (1) at all, and you have outgrown (2) -- your column definitions all use `sa_column` overrides, and your queries are pure SQLAlchemy. The abstraction is not hurting you today, but it is not helping you either. The pragmatic move is to use raw SQLAlchemy for any new work and migrate existing services opportunistically when you are already refactoring their data layers.
