# Advanced Integration Test Patterns

## Real Service Setup

The most important pattern: bring up the actual FastAPI application and test against it.

```python
from httpx import AsyncClient, ASGITransport
from app.main import app  # The REAL application

@pytest.fixture
async def app_client(db_session, redis_client):
    """Start the real application and override only infrastructure dependencies."""
    from app.dependencies import get_db_session, get_redis_client

    app.dependency_overrides[get_db_session] = lambda: db_session
    app.dependency_overrides[get_redis_client] = lambda: redis_client

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        yield client

    app.dependency_overrides.clear()
```

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
For services that connect to multiple databases (e.g., PPS owns its DB but reads from user-services):

```python
@pytest.fixture(scope="session")
def pps_engine():
    return create_engine(os.environ["TEST_PPS_DATABASE_URL"])

@pytest.fixture(scope="session")
def user_services_engine():
    return create_engine(os.environ["TEST_USER_SERVICES_DATABASE_URL"])

@pytest.fixture
def pps_session(pps_engine):
    # ... transaction rollback pattern

@pytest.fixture
def user_services_session(user_services_engine):
    # Seed read-only test data here
    # ... transaction rollback pattern
```

### Test Data Factories
Use factory functions instead of raw SQL for readable, maintainable test data:

```python
# factories/call_log_factory.py
from uuid import uuid4
from datetime import datetime

def create_call_log(
    session,
    user_id: str | None = None,
    session_id: str | None = None,
    caller_number: str = "+919876543210",
    status: str = "completed",
    duration: int = 120,
    **overrides,
) -> CallLog:
    data = {
        "id": uuid4(),
        "user_id": user_id or uuid4(),
        "session_id": session_id or str(uuid4()),
        "caller_number": caller_number,
        "status": status,
        "duration": duration,
        "created_at": datetime.utcnow(),
        **overrides,
    }
    call_log = CallLog(**data)
    session.add(call_log)
    session.commit()
    session.refresh(call_log)
    return call_log
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
async def test_idempotency_lock_created(app_client, redis_client, seed_call_log):
    # Act: process an event via the real application
    await app_client.post("/api/v1/process", json={"session_id": seed_call_log.session_id})

    # Assert: idempotency lock exists with correct TTL
    lock_key = f"call_completed:{seed_call_log.session_id}"
    assert redis_client.exists(lock_key)
    ttl = redis_client.ttl(lock_key)
    assert 86000 < ttl <= 86400  # ~24 hours
```

## Downstream Event Verification

### Redis Stream Consumer
Set up a real consumer to read events the service publishes to Redis streams:

```python
@pytest.fixture
def redis_stream_consumer(redis_client):
    class StreamConsumer:
        def __init__(self, client):
            self.client = client

        def consume(self, stream_name: str, count: int = 10):
            return self.client.xrange(stream_name, count=count)

        def consume_latest(self, stream_name: str):
            entries = self.client.xrange(stream_name, count=1)
            return entries[0] if entries else None

        def assert_event_published(self, stream_name: str, expected_fields: dict):
            entries = self.client.xrange(stream_name)
            assert len(entries) > 0, f"No events found in stream {stream_name}"
            # Check the latest event matches
            _, data = entries[-1]
            for key, value in expected_fields.items():
                assert data.get(key.encode()) == str(value).encode(), \
                    f"Expected {key}={value}, got {data.get(key.encode())}"

    return StreamConsumer(redis_client)


# Usage in tests:
async def test_publishes_call_completed_event(app_client, redis_stream_consumer, seed_call_log):
    await app_client.post("/api/v1/process", json={"session_id": seed_call_log.session_id})

    redis_stream_consumer.assert_event_published(
        "call-events",
        {"session_id": seed_call_log.session_id, "event_type": "call_completed"},
    )
```

### SQS Consumer (via localstack)
Set up a real SQS queue to capture messages the service publishes:

```python
@pytest.fixture
def sqs_consumer():
    import boto3
    import json

    sqs = boto3.client(
        "sqs",
        endpoint_url=os.environ.get("TEST_AWS_ENDPOINT", "http://localhost:4566"),
        region_name="ap-south-1",
    )
    queue_url = sqs.create_queue(QueueName=f"test-queue-{uuid4().hex[:8]}")["QueueUrl"]

    class Consumer:
        def __init__(self):
            self.queue_url = queue_url

        def consume(self, max_wait_seconds=5) -> list[dict]:
            response = sqs.receive_message(
                QueueUrl=queue_url,
                MaxNumberOfMessages=10,
                WaitTimeSeconds=max_wait_seconds,
            )
            return [
                json.loads(msg["Body"])
                for msg in response.get("Messages", [])
            ]

        def assert_message_published(self, expected_type: str, expected_fields: dict):
            messages = self.consume()
            matching = [m for m in messages if m.get("type") == expected_type]
            assert len(matching) > 0, f"No message of type {expected_type} found"
            for key, value in expected_fields.items():
                assert matching[0].get(key) == value

    yield Consumer()
    sqs.delete_queue(QueueUrl=queue_url)


# To wire the service to publish to the test queue, override the SNS/SQS dependency:
@pytest.fixture
def app_client_with_sqs(app_client, sqs_consumer):
    """Override the service's SNS publisher to send to the test SQS queue instead."""
    # This depends on how the service publishes — adapt to the actual dependency
    pass
```

### SNS -> SQS Subscription Pattern
For services that publish to SNS topics, create a test SQS queue subscribed to the topic:

```python
@pytest.fixture
def sns_sqs_consumer():
    import boto3

    endpoint = os.environ.get("TEST_AWS_ENDPOINT", "http://localhost:4566")
    sns = boto3.client("sns", endpoint_url=endpoint, region_name="ap-south-1")
    sqs = boto3.client("sqs", endpoint_url=endpoint, region_name="ap-south-1")

    # Create topic and queue
    topic_arn = sns.create_topic(Name="test-topic")["TopicArn"]
    queue_url = sqs.create_queue(QueueName=f"test-sub-{uuid4().hex[:8]}")["QueueUrl"]
    queue_arn = sqs.get_queue_attributes(
        QueueUrl=queue_url, AttributeNames=["QueueArn"]
    )["Attributes"]["QueueArn"]

    # Subscribe queue to topic
    sns.subscribe(TopicArn=topic_arn, Protocol="sqs", Endpoint=queue_arn)

    # ... consumer class similar to above

    yield consumer, topic_arn
    sqs.delete_queue(QueueUrl=queue_url)
    sns.delete_topic(TopicArn=topic_arn)
```

## Exotel Mocking

Exotel is the one upstream dependency that should always be mocked:

```python
@pytest.fixture
def mock_exotel(monkeypatch):
    """Mock Exotel telephony API responses."""
    from unittest.mock import AsyncMock

    mock_client = AsyncMock()
    mock_client.get_call_details.return_value = {
        "Call": {
            "Sid": "test-call-sid",
            "Status": "completed",
            "Duration": "120",
            "From": "+919876543210",
            "To": "+911234567890",
        }
    }

    # Override the Exotel dependency in the app
    from app.dependencies import get_exotel_client
    from app.main import app
    app.dependency_overrides[get_exotel_client] = lambda: mock_client

    yield mock_client

    if get_exotel_client in app.dependency_overrides:
        del app.dependency_overrides[get_exotel_client]
```

## Seed Data Patterns

### Fixture-Based Seeding
Create reusable fixtures that seed data and clean up via transaction rollback:

```python
@pytest.fixture
def seed_user(db_session):
    from factories.user_factory import create_user
    return create_user(db_session, name="Integration Test User")

@pytest.fixture
def seed_call_log(db_session, seed_user):
    from factories.call_log_factory import create_call_log
    return create_call_log(db_session, user_id=seed_user.id, status="pending")

@pytest.fixture
def seed_completed_call(db_session, seed_user):
    from factories.call_log_factory import create_call_log
    return create_call_log(db_session, user_id=seed_user.id, status="completed", duration=180)
```

### Complex Seed Scenarios
For tests that need a specific state of the world:

```python
@pytest.fixture
def seed_multi_call_scenario(db_session):
    """Set up a user with multiple calls in different states."""
    user = create_user(db_session)
    pending_call = create_call_log(db_session, user_id=user.id, status="pending")
    completed_call = create_call_log(db_session, user_id=user.id, status="completed")
    failed_call = create_call_log(db_session, user_id=user.id, status="failed")
    return {
        "user": user,
        "pending": pending_call,
        "completed": completed_call,
        "failed": failed_call,
    }
```

## Log Validation

When running via Claude Code, capture and assert on log output:

```python
@pytest.fixture
def capture_logs(caplog):
    """Capture structured logs for validation."""
    import logging
    with caplog.at_level(logging.INFO):
        yield caplog

async def test_logs_processing_start_and_completion(app_client, capture_logs, seed_call_log):
    await app_client.post("/api/v1/process", json={"session_id": seed_call_log.session_id})

    log_messages = [r.message for r in capture_logs.records]
    assert any("Processing session" in m for m in log_messages)
    assert any("completed" in m.lower() for m in log_messages)
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

## Test Markers

Always use these markers for integration tests:

```python
@pytest.mark.integration      # Identifies as integration test
@pytest.mark.anyio            # Async test support (NOT asyncio)
@pytest.mark.slow             # If test takes >5s
@pytest.mark.requires_aws     # If test needs AWS credentials (localstack)
@pytest.mark.requires_llm     # If test makes real LLM API calls
```

Configure in pyproject.toml:
```toml
[tool.pytest.ini_options]
markers = [
    "integration: marks tests as integration tests",
    "slow: marks tests as slow",
    "requires_aws: marks tests requiring AWS credentials or localstack",
    "requires_llm: marks tests making real LLM API calls",
]
```
