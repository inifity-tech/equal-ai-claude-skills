# Advanced Integration Test Patterns

## Database Fixtures

### Transaction Rollback Pattern
The gold standard for DB test isolation — wrap each test in a transaction that gets rolled back:

```python
@pytest.fixture
async def db_session(test_engine):
    async with test_engine.connect() as conn:
        trans = await conn.begin()
        session = AsyncSession(bind=conn)
        yield session
        await session.close()
        await trans.rollback()
```

### Two-Database Pattern
For services that connect to multiple databases:

```python
@pytest.fixture(scope="session")
def primary_engine():
    return create_engine(os.environ["TEST_PRIMARY_DATABASE_URL"])

@pytest.fixture(scope="session")
def secondary_engine():
    return create_engine(os.environ["TEST_SECONDARY_DATABASE_URL"])

@pytest.fixture
def primary_session(primary_engine):
    # ... transaction rollback pattern

@pytest.fixture
def secondary_session(secondary_engine):
    # ... transaction rollback pattern
```

### Test Data Factories
Use factory functions instead of raw SQL for readable, maintainable test data:

```python
# factories/user_factory.py
from uuid import uuid4
from datetime import datetime

def create_user(
    user_id: str | None = None,
    email: str = "test@example.com",
    status: str = "active",
    **overrides,
) -> dict:
    data = {
        "id": user_id or str(uuid4()),
        "email": email,
        "status": status,
        "created_at": datetime.utcnow(),
        **overrides,
    }
    return data
```

## Redis Fixtures

### Isolated Redis DB
Use a separate Redis database number for tests:

```python
@pytest.fixture
def redis_client():
    client = redis.Redis(host="localhost", port=6379, db=15)  # DB 15 for tests
    yield client
    client.flushdb()  # Clean after each test
```

### Verifying Redis State
```python
async def test_cache_key_created(redis_client, ...):
    # Act: perform operation
    await process_request(request)

    # Assert: cache key exists with correct TTL
    cache_key = f"user:{user_id}"
    assert redis_client.exists(cache_key)
    ttl = redis_client.ttl(cache_key)
    assert 3500 < ttl <= 3600  # ~1 hour
```

## Event/Message Testing

### SNS/SQS Mock Pattern
Mock the transport but validate the message format:

```python
@pytest.fixture
def mock_publisher():
    publisher = AsyncMock()
    published_messages = []

    async def capture_publish(topic, message, **kwargs):
        published_messages.append({
            "topic": topic,
            "message": json.loads(message),
            "attributes": kwargs.get("message_attributes", {}),
        })

    publisher.publish.side_effect = capture_publish
    publisher.published = published_messages
    return publisher

async def test_publishes_event(mock_publisher, ...):
    # Act
    await process_request(request, publisher=mock_publisher)

    # Assert
    assert len(mock_publisher.published) == 1
    msg = mock_publisher.published[0]["message"]
    assert msg["type"] == "request_processed"
    assert msg["data"]["id"] == request_id
```

## Log Validation

Capture and assert on log output:

```python
@pytest.fixture
def capture_logs(caplog):
    import logging
    with caplog.at_level(logging.INFO):
        yield caplog

async def test_logs_operation(capture_logs, ...):
    await process_request(request)

    log_messages = [r.message for r in capture_logs.records]
    assert any("Processing" in m for m in log_messages)
    assert any("completed" in m.lower() for m in log_messages)

    # No error logs
    error_logs = [r for r in capture_logs.records if r.levelno >= logging.ERROR]
    assert len(error_logs) == 0
```

## Parallel Test Execution

For faster integration test runs:

```toml
# pyproject.toml
[tool.pytest.ini_options]
addopts = "-n auto"  # requires pytest-xdist
```

Ensure test isolation when running in parallel:
- Each test uses its own transaction (rolled back)
- Redis keys include test-specific prefixes
- No shared mutable state between tests

## Timeout and Retry Testing

```python
@pytest.mark.anyio
async def test_handles_database_timeout(db_session, ...):
    """Service handles database timeout gracefully."""
    with patch("app.database.engine") as mock_engine:
        mock_engine.connect.side_effect = OperationalError("timeout", None, None)

        result = await process_request(request)

        assert result.status == "failed"
        assert "timeout" in result.error_message.lower()
```

## Test Markers

Always use these markers for integration tests:

```python
@pytest.mark.integration      # Identifies as integration test
@pytest.mark.anyio            # Async test support (NOT asyncio)
@pytest.mark.slow             # If test takes >5s
@pytest.mark.requires_db      # If test needs database
```

Configure in pyproject.toml:
```toml
[tool.pytest.ini_options]
markers = [
    "integration: marks tests as integration tests",
    "slow: marks tests as slow",
    "requires_db: marks tests requiring database",
]
```

## conftest.py Template

```python
import os
import pytest
from sqlmodel import create_engine, Session

@pytest.fixture(scope="session")
def test_engine():
    """Create test database engine."""
    url = os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql://localhost:5432/test_db"
    )
    engine = create_engine(url, echo=False)

    # Run migrations if using Alembic
    # from alembic.config import Config
    # from alembic import command
    # alembic_cfg = Config("alembic.ini")
    # command.upgrade(alembic_cfg, "head")

    yield engine
    engine.dispose()

@pytest.fixture
def db_session(test_engine):
    """Transactional database session."""
    connection = test_engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)

    yield session

    session.close()
    transaction.rollback()
    connection.close()

@pytest.fixture
def redis_client():
    """Test Redis client with cleanup."""
    import redis
    client = redis.Redis(
        host=os.environ.get("TEST_REDIS_HOST", "localhost"),
        port=int(os.environ.get("TEST_REDIS_PORT", "6379")),
        db=15,  # Isolated test DB
    )
    yield client
    client.flushdb()
```
