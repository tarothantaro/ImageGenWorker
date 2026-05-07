---
name: test-writer
description: Write Python tests that land in the right layer with the right invariants asserted. Use BEFORE adding or modifying any test — and AFTER any code change. Encodes generic Python testing best practices (pytest, layering, fixtures, hygiene). Triggers on phrases like "add test", "write test", "test this", or after completing any implementation task in a Python project.
---

# test-writer

A checklist + decision tree for writing Python tests. Follow it for every test. If the project has a `TESTING.md` (or equivalent) at the repo root, load it first — project-specific rules override this skill.

## Step 0 — Discover the project's conventions

Before writing anything, scan the repo so the new test matches what's already there:

- **Test runner & config**: check `pyproject.toml` / `setup.cfg` / `pytest.ini` / `tox.ini` for `[tool.pytest.ini_options]`, markers, `addopts`, `testpaths`.
- **Layout**: `tests/` vs `src/<pkg>/tests/`; subfolders by layer (`unit/`, `integration/`, `e2e/`) vs flat.
- **Fixtures**: read every `conftest.py` from the repo root down to the target test dir — fixtures cascade.
- **Factories / builders**: look for `factories.py`, `factory_boy`, `polyfactory`, or hand-rolled `make_*` helpers.
- **Existing nearby test**: open the closest sibling test file and mirror its style (imports, fixture names, assertion patterns).

If none of this exists, you are bootstrapping — say so to the user and propose a minimal layout before writing tests.

## Step 1 — Pick the layer

Use the **lowest** layer that exercises the behavior. Wrong-layer tests are the most common review rejection: slow, flaky, and they hide real bugs behind incidental ones.

| You changed | Write tests at |
|---|---|
| Pure functions, dataclasses, validators, parsers, math | `tests/unit/` — no I/O, no network, no DB |
| A class that talks to a DB / cache / queue | `tests/integration/` — against a real local instance (testcontainers, docker-compose, or in-memory equivalent) |
| An HTTP endpoint (FastAPI / Flask / Django / Starlette) | `tests/api/` (or `tests/views/`) — in-process client (`httpx.AsyncClient`, `TestClient`, `Django Client`) |
| A CLI command | `tests/cli/` — invoke via `CliRunner` (Click/Typer) or `subprocess` with captured output |
| A background worker / consumer | `tests/workers/` — drive the handler function directly with a fake message |
| A webhook receiver | `tests/webhooks/` — assert signature verification, replay/dedup, timestamp skew |
| A user-visible flow spanning multiple components | one `tests/e2e/` test — black-box, public interface only |

**Rule of thumb:** if a unit test would catch the bug, an e2e test for the same bug is waste. The pyramid (many unit, fewer integration, few e2e) only works if you respect it.

## Step 2 — Honor universal invariants

These hold regardless of project. The reviewer should be able to check every PR against them.

1. **One concept per test.** "Computes total" + "writes to DB" → two tests, shared fixture. A failing test should point at one cause.
2. **Arrange / Act / Assert** with a blank line between sections. The structure is the documentation.
3. **Test name describes behavior, not mechanics.** `test_withdraw_with_insufficient_balance_raises` ✅, `test_withdraw_2` ❌.
4. **Test the public interface.** Don't reach into private attributes (`_foo`) to check intermediate state — assert on observable outputs. If you have to peek at internals, the seam is wrong.
5. **Deterministic.** No real `datetime.now()`, `random`, `uuid4()`, or sleeps tied to wall clock. Use `freezegun` / `time-machine`, fixed seeds, injected clocks.
6. **No real network.** Stub at the boundary — `respx` for httpx, `responses` for requests, `aioresponses` for aiohttp, `pytest-httpserver` for a real local server. Never hit production or third-party APIs from a test.
7. **No `time.sleep` over ~100ms.** For async events, poll with a timeout (`wait_for(condition, timeout=2.0)`); for time-dependent logic, freeze time.
8. **Tests are independent.** No ordering, no shared mutable state between tests. Reset fixtures (`yield` + cleanup) or use `tmp_path` / fresh DB per test.
9. **Assert on values, not call counts** when possible. `mock.assert_called_once_with(...)` is brittle — prefer asserting on the side effect the call produced.
10. **Status codes alone don't pin errors.** For HTTP tests, also assert on the response body's error code/shape (e.g. `body["error"]["code"]`), not just `resp.status_code`.

## Step 3 — Use existing fixtures and factories

Before writing a new fixture, search:

```bash
rg -n "^@pytest.fixture" --type py
```

Common fixtures worth reusing or building:

- `tmp_path` (built-in) for filesystem-touching tests.
- `monkeypatch` (built-in) for env vars and attribute patching — prefer over `unittest.mock.patch` decorators.
- `caplog` (built-in) for log assertions.
- `capsys` / `capfd` (built-in) for stdout/stderr.
- A session-scoped fixture for expensive resources (DB container, embedding model); a function-scoped fixture that resets state per test.
- An app/client fixture for HTTP tests (`httpx.AsyncClient(app=app, ...)`).
- An auth-headers fixture that returns a ready-to-use `Authorization` header dict.

**Factories vs fixtures:**
- A **factory** is a pure function that returns a valid object (`make_user(email="x@y")`). It does not persist. Pass it to a repository/ORM call to write.
- A **fixture** is the persisted, ready-to-use thing (`seeded_user`).
- If you find yourself repeating object construction in 3+ tests, extract a factory — not a fixture.

**Stubs at the boundary, not internals.** If you're mocking a function inside the module under test, the seam is in the wrong place.

## Step 4 — Per-layer recipes

### Unit
- One module under test per file.
- Cover happy path + each branch + each raise. Parametrize aggressively (`@pytest.mark.parametrize`) for table-driven cases.
- Property-based tests (`hypothesis`) for parsers, encoders, anything with an algebraic invariant.

```python
@pytest.mark.parametrize(
    "amount, balance, expected",
    [
        (10, 100, 90),
        (100, 100, 0),
    ],
)
def test_withdraw_returns_new_balance(amount, balance, expected):
    assert withdraw(amount, balance) == expected


def test_withdraw_with_insufficient_balance_raises():
    with pytest.raises(InsufficientFunds, match="balance"):
        withdraw(amount=200, balance=100)
```

### Integration
- Bring up the dependency once per session (testcontainers / docker-compose), reset per test (truncate / flush / new schema).
- Test the real query / real transaction. If your ORM lets you, run the test inside a transaction that rolls back at teardown — fastest reset.
- Cover: happy path, each conflict/constraint violation, transactional atomicity when multiple writes belong together.

### HTTP API
Minimum suite per endpoint:
1. Happy path — correct status, response body matches schema.
2. One test per declared error code.
3. 401 (no auth) and 403 (wrong-user resource) where applicable.
4. 422 / 400 from input validation (extra field if `extra='forbid'`, type violation).
5. If state-changing: assert the persisted side effect.

```python
async def test_create_widget_returns_201_and_persists(app_client, auth_headers):
    resp = await app_client.post(
        "/api/v1/widgets",
        headers=auth_headers,
        json={"name": "spinner", "color": "blue"},
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "spinner"

    stored = await widget_repository.get(body["id"])
    assert stored.color == "blue"


async def test_create_widget_rejects_extra_field(app_client, auth_headers):
    resp = await app_client.post(
        "/api/v1/widgets",
        headers=auth_headers,
        json={"name": "spinner", "color": "blue", "rogue": 1},
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
```

### CLI
```python
def test_init_creates_config_file(tmp_path):
    runner = CliRunner()
    result = runner.invoke(cli, ["init", "--path", str(tmp_path)])

    assert result.exit_code == 0
    assert (tmp_path / "config.toml").exists()
```

### Webhook
Always cover the four cases: valid signature accepted; invalid signature rejected; replay (same event id within dedup window) rejected; stale timestamp (e.g. >5min) rejected. Use the SDK's real signature helper (`stripe.Webhook.construct_event`, etc.) — never mock signature verification.

### E2E
- Black-box. Drive the public interface (HTTP, CLI, queue message in → side effect out).
- Don't reach into the DB to inspect state — call a `GET` endpoint or read the user-facing artifact.
- Keep the count low. One golden-path e2e per user-visible flow is usually enough.

## Step 5 — Hygiene checklist before commit

- [ ] Test name reads like a sentence about behavior.
- [ ] Arrange / Act / Assert visually separated.
- [ ] No `time.sleep(>0.1)`, no real network, no real wall-clock dependency.
- [ ] No mocking of internals — only at the system boundary.
- [ ] Asserts on values, not call counts, where possible.
- [ ] Parametrized rather than copy-pasted when 2+ near-identical cases exist.
- [ ] Cleanup is via fixture teardown (`yield` + cleanup), not test code.
- [ ] No commented-out asserts or `print()` left behind.

## Step 6 — Run before declaring done

```bash
# Fast: just the file you touched
pytest tests/<layer>/test_<file>.py -x -vv

# Then the layer
pytest tests/<layer>/ -x

# Then the whole gate (whatever the project uses — Makefile target, tox, nox)
make test    # or: tox / nox / pytest
```

If unrelated tests fail, that's a real bug — investigate, don't ignore. CI will run the same gate.

For coverage-gated projects: `pytest --cov=<pkg> --cov-report=term-missing` and check the new code is exercised.

## Anti-patterns (do not do)

- ❌ **Mocking the thing under test.** If `module.foo` is what you're testing, don't `mock.patch("module.foo")`. Patch its dependencies, not itself.
- ❌ **Asserting only on status codes.** `assert resp.status_code == 400` doesn't pin the error — also assert on the body's error code or message.
- ❌ **`assert mock.called`.** Useless — it doesn't check arguments. Use `assert_called_once_with(...)` or, better, assert on the observable side effect.
- ❌ **Hidden coupling via shared state.** Module-level lists/dicts mutated by tests cause order-dependent failures. Use fixtures.
- ❌ **`try/except` inside a test to "make it not fail".** If the code can raise, either `pytest.raises` it or let it bubble.
- ❌ **`if`/`for` logic in test bodies.** Branching tests test nothing reliably. Parametrize instead.
- ❌ **An e2e test for what a unit test would catch.** Slow, flaky, and the failure won't tell you where the bug is.
- ❌ **Sleeping to wait for async work.** Poll with timeout, or use the framework's `await` / event hook.
- ❌ **Re-encoding production secrets in test fixtures.** Use clearly fake values (`sk_test_...`, `password = "test-password"`).

## When to stop and load deeper docs

- Adding a new test layer or directory that doesn't exist yet.
- Introducing a new external dependency (DB, queue, third-party SDK) — decide stubbing strategy before writing.
- Writing the first test in a layer you haven't touched in this project.
- The project has a `TESTING.md` and you're about to violate one of its rules.

Otherwise this checklist + existing nearby test file + project conftest is enough.
