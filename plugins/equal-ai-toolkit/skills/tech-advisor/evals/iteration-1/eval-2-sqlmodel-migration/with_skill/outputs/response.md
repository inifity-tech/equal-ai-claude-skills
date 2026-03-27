# Tech Advisor: SQLModel to Raw SQLAlchemy Migration

So I've spent a good while digging through the database layer across all four services and done some research on where SQLModel and SQLAlchemy stand today. Here's what I'm seeing -- and honestly, the picture is more nuanced than "should we migrate or not."

---

## Doc-Code Discrepancies

Before we dive into the migration question, I found some things worth flagging:

1. **The user-services data layer doc** (`myequal-ai-user-services/docs/architecture/LLD-data-layer.md`) describes the data flow as returning "SQLModel result" objects, but in practice, **many of the async managers are already using raw SQLAlchemy** -- `sqlalchemy.select`, `sqlalchemy.ext.asyncio.AsyncSession`, `sqlalchemy.orm.attributes`, etc. The docs paint a picture of a clean SQLModel-based layer, but the code tells a different story: it's a hybrid.

2. **The memory-service architecture doc** (`memory-service/docs/architecture/LLD-data-layer.md`) likely describes SQLModel patterns, but the actual `database.py` is using `sqlalchemy.ext.asyncio` directly for engine creation, session factories, etc. SQLModel is really only used for model definitions and metadata.

---

## Current State (Verified Against Code)

Here's what your codebase *actually* does today, service by service:

### The Real Usage Pattern

**SQLModel is used for exactly two things across all services:**
1. **Model definitions** -- `class MyModel(SQLModel, table=True)` with `Field()` annotations
2. **Synchronous session/select** -- `from sqlmodel import Session, select` in older sync managers

**Raw SQLAlchemy is used for everything else**, and it's already the majority of the database infrastructure:
- All async engines (`create_async_engine`)
- All async session factories (`async_sessionmaker`, `AsyncSession`)
- Complex queries (`func`, `text`, `desc`, `asc`, `or_`, `and_`, `insert`, `delete`)
- Column type definitions (`Column`, `DateTime`, `JSON`, `Boolean`, `String`, `Index`, `UniqueConstraint`)
- ORM utilities (`attributes.flag_modified`, `Session.in_transaction()`)
- Connection pooling, pool monitoring, warm-up -- all SQLAlchemy

Let me quantify this across services:

| Service | SQLModel Models | Uses `session.exec()` | Uses `session.execute()` | Async? | Relationships? |
|---------|----------------|----------------------|-------------------------|--------|---------------|
| **myequal-ai-backend** | 1 (CallLog) | Yes (sync only) | No | No (sync only) | No |
| **myequal-ai-user-services** | 18+ models | 102 calls (sync) | 35 calls (async+sync) | Hybrid (sync + async) | Yes (User, Device, Exophone, Verification) |
| **myequal-post-processing-service** | 4 models | Yes (sync only) | No | No (sync only) | No |
| **memory-service** | 5+ models | Some (older code) | 17 calls (async) | Fully async | No |

### The Dangerous Hybrid: `select` Confusion

Here's the most concerning finding. In `myequal-ai-user-services`, there's an active, *recently-bitten* bug pattern: **mixing `sqlalchemy.select` and `sqlmodel.select`**.

On March 2, 2026, commit `d5167866` fixed a production 500 error caused by exactly this:

> `sqlalchemy.select` returns immutable Row objects from `session.exec().first()`, causing `AttributeError` when code tries to mutate fields. `sqlmodel.select` returns proper model instances that support attribute assignment.

The fix was a one-line change from `from sqlalchemy import select` to `from sqlmodel import select`. But here's the thing -- **14 manager files in user-services still import `sqlalchemy.select`**, and some files import BOTH:

```python
# db_user_manager.py -- imports both!
from sqlalchemy import select
from sqlmodel import Session, select as sqlmodel_select
```

This is a landmine. Any developer who writes a new query using `sqlalchemy.select` with `session.exec()` will get subtle bugs where returned objects look correct but mutations silently fail or crash.

### The `sa_column` Escape Hatch is Everywhere

Another pattern I noticed: almost every model in the codebase uses `sa_column=Column(...)` for anything beyond trivial fields. Look at `ProcessedSessionContext` in PPS -- out of roughly 40 fields, the vast majority use `sa_column=Column(String, nullable=True)` or similar. At that point, you're writing SQLAlchemy column definitions anyway, just wrapped in SQLModel's `Field()`.

The `Caller` model, `User` model, `VerificationSession`, `StaticCallerMemory` -- they all do this. The only fields that benefit from pure SQLModel syntax are simple non-nullable strings and UUIDs.

### Version Spread

| Service | SQLModel Version | Notes |
|---------|-----------------|-------|
| myequal-ai-backend | `>=0.0.22` | 15 versions behind latest |
| myequal-ai-user-services | `>=0.0.22` | 15 versions behind latest |
| myequal-post-processing-service | `>=0.0.22` | 15 versions behind latest |
| memory-service | `>=0.0.24` | 13 versions behind latest |
| myequal-evaluations | `>=0.0.24` | 13 versions behind latest |

Latest SQLModel is **0.0.37** (released February 2026). The services are running versions that are 13-15 releases behind. Notable: SQLModel 0.0.37 now requires Python >= 3.10 and is built on SQLAlchemy 2.0+. Memory-service already explicitly depends on `sqlalchemy[asyncio]>=2.0.41`.

---

## External Context

### Where SQLModel Stands in 2026

SQLModel has matured significantly since your versions were pinned. Key developments:

- **Async support exists but is incomplete.** SQLModel's official docs have an "Advanced User Guide" that's still "gradually growing" and promises async documentation. In practice, everyone using SQLModel async is using SQLAlchemy's async infrastructure directly -- exactly what your memory-service already does.

- **The `session.exec()` vs `session.execute()` split is a known pain point.** `session.exec()` provides better type hints and returns model instances directly. `session.execute()` (SQLAlchemy's method) returns `Row` objects that need `.scalars()`. Your codebase uses both, inconsistently, which is the source of actual bugs.

- **Relationship lazy loading + async = pain.** This is a well-documented issue (GitHub issues #74, #129, #130, #643). Accessing relationship attributes in async context throws `MissingGreenlet` errors. Your user-services uses `Relationship()` for User-Device-Exophone-Verification chains, but only in sync code. If you ever migrate those managers to async (which seems to be the direction, given the `*_async.py` manager pairs), you'll hit this wall.

- **`sa_column` usage defeats the purpose.** SQLModel's value proposition is simpler model definitions. When you're writing `sa_column=Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)` -- which is *every timestamp field across every model* -- you've already opted out of that simplicity.

- **Pydantic 2.12+ compatibility issue.** There's a known issue where SQLModel fails to create DB constraints when using `Annotated` types after Pydantic 2.12.0. This could bite you on upgrade.

### What SQLAlchemy 2.0+ Offers Natively

SQLAlchemy 2.0 introduced:
- **Mapped annotations** (`Mapped[str]`, `mapped_column()`) that give you type safety comparable to SQLModel
- **First-class async** with `AsyncSession`, `async_sessionmaker` -- which you're already using
- **Better Pydantic integration** through community libraries and native patterns
- **No abstraction leakage** -- what you write is what you get

---

## My Take

Here's where I land on this: **you're already 70% migrated to raw SQLAlchemy, you just haven't acknowledged it yet.**

The honest assessment is that SQLModel is only providing value in two narrow areas for your codebase: (1) the `SQLModel` base class for model definitions, and (2) `session.exec()` with `sqlmodel.select` for sync queries in user-services. Everything else -- async engines, async sessions, complex queries, column definitions, indexes, constraints -- is already raw SQLAlchemy.

The real question isn't "should we migrate" -- it's "should we formalize what's already happening and eliminate the confusing hybrid state that's causing actual production bugs?"

I'd lean toward **yes, but not as a big-bang migration**. The highest-value move is standardizing the query layer (pick ONE `select` and ONE session execution method) rather than rewriting all model definitions. The model definitions are the least problematic part -- `SQLModel(table=True)` is a thin wrapper and causes no real issues. The query/session confusion is where the bugs live.

---

## Questions to Drive the Discussion

1. **The `select` confusion is causing real bugs today.** You had a production 500 just three weeks ago from `sqlalchemy.select` vs `sqlmodel.select`. There are 14 manager files still importing `sqlalchemy.select` that use `session.exec()` -- have you audited whether any of those have the same latent mutation bug? The ones doing read-only queries would be fine, but any that fetch-then-update are at risk.

2. **What's the async migration trajectory for user-services?** You have sync/async manager pairs for 9 entity types, but the async versions use `sqlalchemy.select` + `session.execute()`, while the sync versions use `sqlmodel.select` + `session.exec()`. Are you planning to deprecate the sync managers? Because if so, you're naturally moving toward pure SQLAlchemy anyway, and SQLModel's `exec()` convenience becomes irrelevant.

3. **The Relationship usage concerns me.** User-services has `Relationship()` definitions on `User`, `UserDevice`, `Exophone`, and `VerificationSession`. These only work in sync context today. If any of the async managers ever need to traverse those relationships (e.g., loading a user's devices in an async endpoint), you'll hit the `MissingGreenlet` error. Are those relationships actually used in query paths, or are they just defined but accessed manually via separate queries?

4. **The `sa_column` pattern suggests SQLModel isn't buying you much on models.** I counted and nearly every field with any complexity (timestamps, JSON, booleans with defaults, strings with length constraints) uses `sa_column=Column(...)`. The only fields that use pure SQLModel `Field()` syntax are simple strings and UUIDs. Is the `SQLModel(table=True)` base class giving you enough value to justify the extra abstraction layer, or would `DeclarativeBase` with `Mapped[]` annotations be cleaner?

5. **Version gap risk.** You're on SQLModel `>=0.0.22` in most services, current is `0.0.37`. That's 15 releases. There's a known Pydantic 2.12+ compatibility issue with constraint creation. What Pydantic version are you running, and have you tested whether upgrading SQLModel (or Pydantic) would break anything? This matters because staying on old versions means you're accumulating security and compatibility debt.

6. **Memory-service is your template.** It's the most modern of your services -- fully async, clean repository pattern, `BaseRepository(Generic[T])`, uses `sqlalchemy.select` + `session.execute()` consistently. If you were to standardize a database pattern across services, this is the one I'd point at. Have you considered using it as the reference architecture and gradually bringing other services in line?

7. **The backend service barely uses a database at all.** It has one model (`CallLog`) and a simple sync session. Is there any plan to add more database complexity there, or is it intentionally lightweight? Because if it stays simple, there's no urgency to change anything -- but if it grows, you'd want to start with the right patterns.

---

## What I'd Actually Recommend (If Asked)

**Phase 1 (Immediate, high ROI):** Standardize the `select` import across user-services. Every manager should use `sqlmodel.select` with `session.exec()` (sync) OR `sqlalchemy.select` with `session.execute()` (async). Never mix. This prevents the class of bug you hit on March 2. This is a half-day of work.

**Phase 2 (Next quarter):** As you naturally build new async managers, follow the memory-service pattern (`sqlalchemy.select` + `session.execute()` + `AsyncSession`). Don't rewrite existing sync managers unless you need to.

**Phase 3 (If/when it matters):** Consider migrating model definitions from `SQLModel(table=True)` to SQLAlchemy 2.0 `DeclarativeBase` with `Mapped[]` annotations. This is lowest priority because the model layer is the least problematic. Only do this if you hit a real pain point (like the Pydantic version incompatibility) or if you want to simplify the dependency tree.

The key insight is: **don't migrate for migration's sake**. Migrate the pieces that are causing actual pain (the query layer confusion) and leave the pieces that are working fine (model definitions).

---

What do you think? Want to dig into any of these areas -- especially the `select` audit, the async Relationship risk, or the memory-service-as-template idea?

---

Sources:
- [SQLModel Official Documentation](https://sqlmodel.tiangolo.com/)
- [SQLModel Features - Based on SQLAlchemy](https://sqlmodel.tiangolo.com/features/)
- [SQLModel PyPI - Latest Version 0.0.37](https://pypi.org/project/sqlmodel/)
- [SQLModel Async Relationship Bug - GitHub Issue #643](https://github.com/fastapi/sqlmodel/issues/643)
- [SQLModel Lazy Loading Issue - GitHub Issue #74](https://github.com/fastapi/sqlmodel/issues/74)
- [SQLModel Preload Relationships Outside Session - GitHub Issue #130](https://github.com/fastapi/sqlmodel/issues/130)
- [SQLModel Pydantic 2.12 Constraint Bug - GitHub Discussion #1597](https://github.com/fastapi/sqlmodel/discussions/1597)
- [SQLModel Async Support - DeepWiki](https://deepwiki.com/fastapi/sqlmodel/5.4-async-support)
- [Async-SQLModel Extension for Lazy Loading](https://github.com/2jun0/async-sqlmodel)
- [Is SQLModel Still Worth It in 2025?](https://python.plainenglish.io/sqlmodel-in-2025-the-hidden-gem-of-fastapi-backends-20ee8c9bf8a6)
- [SQLAlchemy 2.0 Async Documentation](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html)
- [SQLModel vs SQLAlchemy Comparison - Oreate AI](https://www.oreateai.com/blog/sqlmodel-vs-sqlalchemy-navigating-the-python-orm-landscape/44a103f16d0e599638e9216570f41037)
