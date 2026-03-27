# SQLModel to Raw SQLAlchemy Migration: Deep Technical Analysis

So I have dug through the database layer across all four services and done extensive research on the current state of SQLModel vs SQLAlchemy. Here is what I am seeing -- and honestly, the picture is more nuanced than a simple "migrate or don't" answer.

---

## Doc-Code Discrepancies

Before we dive into the core question, I noticed a few things while reading the code that are worth surfacing:

1. **PPS uses synchronous SQLModel sessions inside async def methods.** The DBRepository in myequal-post-processing-service/app/database/db_repository.py calls self.db_session.exec(), self.db_session.commit(), and self.db_session.rollback() synchronously within methods marked async def. This is a blocking anti-pattern -- these synchronous DB calls will block the event loop. The PPS session module (app/database/session.py) creates engines with sqlmodel.create_engine (sync), not create_async_engine. This means every DB operation in PPS blocks the asyncio event loop for the duration of the query + network round-trip.

2. **Memory service imports SQLModel from sqlmodel but uses sqlalchemy.ext.asyncio for all actual session management.** The from sqlmodel import SQLModel in memory-service/app/database.py is only used for SQLModel.metadata.create_all in the init_db() function. Everything else -- engine creation, session factories, session lifecycle -- is pure SQLAlchemy async. SQLModel is essentially a thin model layer here.

3. **User-services has a hybrid sync/async split.** The sync managers (db_call_log_manager.py, db_base_manager.py) use sqlmodel.Session and sqlmodel.select, while the async managers (db_base_manager_async.py, db_caller_manager_async.py) use sqlalchemy.ext.asyncio.AsyncSession and sqlalchemy.select. The two systems coexist, creating an inconsistent developer experience -- you have to know which pattern to use depending on whether you are in a sync or async context.

4. **The CLAUDE.md says all services use SQLModel, but the actual SQLAlchemy usage is far deeper than documented.** Every service already heavily uses raw SQLAlchemy constructs (select, func, desc, asc, and_, Column, Index, JSON, DateTime, dialect-specific types like UUID). SQLModel is primarily used for model definitions and the Session/select convenience wrappers.

---

## Current State: How the Data Layer Actually Works

### Dependency Versions

| Service | SQLModel | SQLAlchemy | Alembic | Async? |
|---|---|---|---|---|
| myequal-ai-backend | >=0.0.22 | (transitive) | >=1.16.2 | No (sync only) |
| myequal-ai-user-services | >=0.0.22 | (transitive) | >=1.14.1 | Hybrid (sync + async) |
| memory-service | >=0.0.24 | >=2.0.41 (explicit) | >=1.14.0 | Yes (fully async) |
| myequal-post-processing-service | >=0.0.22 | (transitive) | >=1.14.1 | Sync (blocking in async) |

### What SQLModel Actually Gives You Today

Looking at the actual code across all four services, SQLModel is used for exactly three things:

1. **Model definitions**: class CallLog(SQLModel, table=True) -- the table/Pydantic hybrid class pattern.
2. **Session wrapper**: from sqlmodel import Session -- a thin wrapper over sqlalchemy.orm.Session.
3. **select()**: from sqlmodel import select -- a thin wrapper over sqlalchemy.select that adds some type-hint sugar.

That is it. Every non-trivial query already uses raw SQLAlchemy constructs. Look at postgres_caller_memory_repository.py in memory-service:

```python
from sqlalchemy import String, and_, cast, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col  # <-- only thing from sqlmodel in queries
```

Or db_call_log_manager.py in user-services:

```python
from sqlalchemy import select, asc, desc, func, text, or_
from sqlmodel import Session  # <-- just the session wrapper
```

The models themselves lean heavily on sa_column=Column(...) escape hatches for anything non-trivial:

```python
# From user-services User model (app/models/user.py)
created_at: datetime = Field(
    default_factory=datetime.utcnow,
    sa_column=Column(DateTime, default=datetime.utcnow, nullable=False),
)
user_metadata: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
```

```python
# From memory-service StaticCallerMemory (app/models/memory.py)
entity_id: str = Field(
    sa_column=Column(String(50), nullable=False),
)
memory_data: dict[str, Any] = Field(
    default_factory=dict,
    sa_column=Column(JSON, nullable=False),
)
created_by: uuid.UUID | None = Field(
    default=None,
    sa_column=Column(UUID(as_uuid=True)),
)
```

When you are using sa_column on most of your fields, you are not really getting the "write one model" benefit that SQLModel promises. You are writing SQLAlchemy column definitions inside SQLModel syntax, which is actually more verbose than pure SQLAlchemy's mapped_column().

### Connection Pool Architecture

The connection pooling is already entirely SQLAlchemy -- and it has been battle-tested through multiple production incidents:

- **Backend**: 40 pool + 60 overflow = 100 max, 30s timeout, pre-ping, TCP keepalive
- **User-services**: Configurable (default 40+60), pre-ping, 300s recycle, TCP keepalive on sync, statement timeout
- **Memory-service**: 40 pool + 80 overflow, 5s timeout (fail-fast), LIFO reuse, 1800s recycle, pool warming at startup
- **PPS**: 20 pool + 40 overflow, 3600s recycle, pre-ping

The git history shows significant production work on pool tuning -- memory-service alone has commits for pool warming, cold-start fixes, DISTINCT ON bug fixes, and session handling consolidation. All of this is pure SQLAlchemy.

---

## External Context: The Current State of SQLModel

SQLModel is at version 0.0.24 (memory-service) / 0.0.22 (other services). After 3+ years, it is still pre-1.0. The "0.0.x" versioning is not just cosmetic -- the Advanced User Guide on sqlmodel.tiangolo.com literally says:

> "The Advanced User Guide is gradually growing... At some point it will include: How to use async and await with the async session. How to run migrations. How to combine SQLModel models with SQLAlchemy. ...and more."

This is telling. Async support, migrations, and SQLAlchemy interop are documented as future advanced topics, yet your codebase already needs all three extensively.

**SQLAlchemy 2.0**, on the other hand, is mature (2.0.41+), has first-class async support, mapped_column() with type-hint-driven column inference (similar to what SQLModel promised), and Pydantic-compatible serialization via MappedAsDataclass or the newer orm_mode patterns.

SQLAlchemy 2.0's mapped_column() syntax is now quite close to SQLModel's model syntax:

```python
# SQLAlchemy 2.0 style
class User(Base):
    __tablename__ = "user"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
```

vs.

```python
# SQLModel style (what you have today)
class User(SQLModel, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str | None = Field(default=None, nullable=True)
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime, default=datetime.utcnow, nullable=False),
    )
```

The SQLAlchemy 2.0 version is actually less boilerplate for non-trivial columns because you do not need the sa_column escape hatch.

---

## My Take

Here is where I will be direct: **you are already 80% on raw SQLAlchemy.** SQLModel is a thin veneer on top, and in many places it is actively getting in the way rather than helping. But a full migration carries real risk for a system handling live phone calls.

### What SQLModel is costing you right now:

1. **The PPS sync-in-async problem**: PPS uses sqlmodel.Session (sync) inside async handlers. This blocks the event loop during every DB operation. With raw SQLAlchemy, you would use AsyncSession natively -- no ambiguity.

2. **Confusion about which select to use**: User-services commit d5167866 was literally "fix: use sqlmodel.select instead of sqlalchemy.select in exophone migration manager." This confusion is a symptom of having two overlapping APIs.

3. **sa_column boilerplate**: When most fields need sa_column=Column(...), you are paying the cost of learning two APIs without the benefit of either. SQLAlchemy 2.0's mapped_column is cleaner.

4. **Version lag**: SQLModel 0.0.22 pins you to specific SQLAlchemy versions. Memory-service explicitly adds sqlalchemy[asyncio]>=2.0.41 to work around this, but the other services do not, which means you are potentially running different SQLAlchemy versions across services.

5. **No async guidance**: SQLModel's docs still do not cover async patterns. Your team has had to figure these out independently (and inconsistently) across services.

### What SQLModel gives you that is actually valuable:

1. **Pydantic model + DB model in one class**: This is the original value proposition. But in practice, you already have separate response models (e.g., CallLogDetails in user-services). The dual model benefit is largely unused.

2. **Relationship() syntax**: Slightly cleaner than SQLAlchemy's relationship(). Used in user-services for User.devices, User.verification_sessions, etc.

3. **Familiarity**: Your team knows the pattern. Changing it has a learning curve cost.

### My recommendation: Incremental migration, not a big bang.

**Do not** do a service-wide migration right now. Instead:

1. **Fix PPS first** (highest impact): Migrate PPS's DBRepository and session.py to use AsyncSession with create_async_engine. This fixes the event-loop blocking problem, which is a real production issue. Since PPS already uses async def everywhere, the session code is the only thing that needs to change. The models can stay as SQLModel for now.

2. **Standardize new code on SQLAlchemy 2.0 patterns**: For any new models or managers, use mapped_column() with DeclarativeBase instead of SQLModel. This is backward-compatible -- SQLAlchemy 2.0 models and SQLModel models can coexist in the same database via shared MetaData.

3. **Migrate memory-service models opportunistically**: Memory-service is already 95% SQLAlchemy. The models just need to switch from class X(SQLModel, table=True) to class X(Base) with mapped_column. Since it is the service with the most complex queries and the most mature async setup, it is the easiest win.

4. **Leave user-services and backend for later**: These have the most models and the most code depending on sqlmodel.Session. The risk/reward of migrating them now is not great. When you next do a major refactor (e.g., consolidating the sync/async manager split in user-services), fold in the SQLAlchemy migration.

---

## Scale and Performance Concerns

A few things I noticed that are relevant whether you migrate or not:

1. **PPS's sync DB access is your biggest bottleneck.** With pool_size=20 and synchronous queries, under concurrent SQS message processing (up to 10 concurrent tasks per config), you are blocking 10 event loop threads on DB I/O simultaneously. This creates head-of-line blocking for any HTTP requests that come in during processing.

2. **Memory-service's 5-second pool timeout is aggressive.** With pool_size=40 and max_overflow=80, if all 120 connections are in use, new requests fail in 5 seconds. The git history shows this was a deliberate "fail fast" choice, but at 10x traffic you would see a lot of 503s. Worth watching.

3. **User-services' sync managers in an async service.** The sync DBCallLogManager calls self.db_session.exec() synchronously. In the async code paths (SQS consumer, call events stream processor), this blocks the event loop. The async managers (BaseAsyncDBManager) are the right pattern, but the sync ones are still widely used.

4. **No statement-level timeouts in PPS or backend.** User-services and memory-service both set statement_timeout=60000 (60s). PPS and backend do not. A slow query in PPS could hold a connection indefinitely, contributing to pool exhaustion.

---

## Questions to Drive This Discussion

1. **What is the actual production impact of PPS's sync-in-async pattern?** Have you seen event loop blocking in Datadog traces? If PPS is processing Gemini transcriptions (which are CPU/IO-heavy), the sync DB calls during those workflows could be creating significant latency spikes for any concurrent HTTP health checks or API calls. This is the strongest argument for an immediate (targeted) change.

2. **Are you planning to move user-services fully async?** The hybrid sync/async manager split is the biggest source of complexity in user-services' data layer. If you are going to unify on async (which the trajectory suggests -- you are adding async managers while the sync ones accumulate tech debt), that is the natural time to drop SQLModel.

3. **Have you considered the Alembic migration risk?** The models define SQLModel.metadata, which Alembic uses for migration generation. If you switch to SQLAlchemy DeclarativeBase, you will use a different metadata object. You need to ensure Alembic's target_metadata is updated correctly. This is straightforward but must not be missed -- a wrong metadata reference means Alembic thinks every table is new and tries to recreate them.

4. **What is your testing strategy for the data layer?** I noticed the test setup uses SQLite (conftest.py patterns), but your production is PostgreSQL with dialect-specific features (DISTINCT ON, JSON operators, UUID columns). Any migration needs to maintain the test infrastructure. Are you confident the tests catch issues that only manifest with PostgreSQL-specific behavior?

5. **How do you handle model serialization to API responses?** SQLModel's Pydantic integration means you can theoretically use the DB model directly as an API response. But your code already uses separate response models (e.g., CallLogDetails). If you are not actually using the SQLModel-as-Pydantic feature, that removes the last major argument for keeping SQLModel. Are there any endpoints that return SQLModel instances directly?

6. **What is the connection pool headroom across services during peak load?** The git history shows multiple pool exhaustion incidents. With 4 services each maintaining their own connection pools (user-services: 100 max, memory-service: 120 max, PPS: 60 max, backend: 100 max), you could have up to around 380 connections to PostgreSQL. What are the RDS instance connection limits, and how close are you to them during peak?

7. **Have you considered the MappedAsDataclass pattern in SQLAlchemy 2.0?** This gives you dataclass-like behavior (init, repr, eq) on your SQLAlchemy models, which is a lighter-weight alternative to SQLModel's Pydantic integration. It might be the best of both worlds for services that do not need Pydantic validation on the DB model.

---

## Sources

- [SQLAlchemy vs SQLModel: Which Should You Choose?](https://tapanbasuli.medium.com/sqlalchemy-vs-sqlmodel-which-should-you-choose-for-your-python-project-7ea0b040af14)
- [SQLModel vs. SQLAlchemy: Navigating the Python ORM Landscape](https://www.oreateai.com/blog/sqlmodel-vs-sqlalchemy-navigating-the-python-orm-landscape/44a103f16d0e599638e9216570f41037)
- [SQLModel Official Site](https://sqlmodel.tiangolo.com/)
- [SQLModel Features](https://sqlmodel.tiangolo.com/features/)
- [SQLModel Advanced User Guide](https://sqlmodel.tiangolo.com/advanced/)
- [Will the project be updated with SQLAlchemy 2.0? (GitHub Discussion #547)](https://github.com/fastapi/sqlmodel/discussions/547)
- [SQLModel Releases on GitHub](https://github.com/fastapi/sqlmodel/releases)
- [Async Database Operations with SQLModel](https://arunanshub.hashnode.dev/async-database-operations-with-sqlmodel)
- [FastAPI with Async SQLAlchemy, SQLModel, and Alembic (TestDriven.io)](https://testdriven.io/blog/fastapi-sqlmodel/)
- [SQLModel vs SQLAlchemy: Cleaner CRUD with Metrics](https://medium.com/@bhagyarana80/sqlmodel-vs-sqlalchemy-cleaner-crud-with-metrics-9d50956f1015)
- [10 SQLModel vs SQLAlchemy Choices with Real Benchmarks](https://medium.com/@sparknp1/10-sqlmodel-vs-sqlalchemy-choices-with-real-benchmarks-dde68459d88f)
- [Async Support in SQLModel (DeepWiki)](https://deepwiki.com/fastapi/sqlmodel/5.4-async-support)
