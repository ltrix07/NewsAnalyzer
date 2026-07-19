# Codex brief — Stop the DB fixture from hiding configuration errors

## The problem

`tests/conftest.py:89`:

```python
except (OperationalError, RuntimeError, ValidationError) as exc:
    pytest.skip(f"Database is not reachable: {exc}")
```

`ValidationError` here is a Pydantic settings failure — the `.env` does not satisfy `Settings`. That is
not "the database is unreachable"; it is "this environment cannot construct its configuration". Every
DB-backed test then skips, and the run reports success.

This is not hypothetical. When the merge-window fix added the settings invariant
`min(cluster_window_hours, consolidate_window_hours) >= selection_window_hours`, a local `.env` still
carrying `CLUSTER_WINDOW_HOURS=36` made `Settings` raise. The suite reported a clean pass with 144
tests silently skipped, and that run was reported as "make test passed: 180 tests" — 180 being the
*collected* count. The actual executed count was 27 passed / 9 failed. The green result was an artifact
of this except clause.

The severity is not the one line. It is that this converts *any* future configuration mistake into a
false green, and it does so precisely at the moment when a change to `Settings` is what is being
tested — the exact case where the suite most needs to fail.

## The change

Remove `ValidationError` from the tuple. A configuration that cannot be constructed must raise and fail
the run.

Keep the other two, but stop giving all of them the same wrong message:

- `OperationalError` — the database genuinely is not reachable. Legitimate skip for a contributor with
  no Postgres running. Message should say so.
- `RuntimeError` — raised deliberately at `conftest.py:48` when `DATABASE_URL` is unset. Legitimate
  skip, but a different situation and it should say `DATABASE_URL is not configured`, not "unreachable".

The shared message is what made the misdiagnosis possible: the skip reason named a cause that was not
the cause, so reading the output confirmed a wrong hypothesis instead of contradicting it. Any skip
message here should be specific enough to be actionable on its own.

Also drop the now-unused `ValidationError` import at `conftest.py:11`.

## Optional hardening — implement only if it stays small

Add an environment flag (e.g. `REQUIRE_DB_TESTS=1`) that turns these skips into hard failures. The
value is in CI and in any run whose result is going to be reported as evidence: it makes "the suite
passed" and "the suite actually exercised the database" the same statement. If it cannot be done
cleanly in a few lines, leave it out and say so — the mandatory part above is what matters.

## Tests

This is test infrastructure, so the verification is behavioural rather than a new test case:

- With a deliberately invalid setting in the environment, the suite **fails** instead of skipping.
  Demonstrate this in the PR description with the actual command and output.
- With a valid environment and a reachable database, the suite still runs with **0 skips**.
- With `DATABASE_URL` unset, the suite skips with a message naming that specific cause.

## Definition of done

- `make lint` and `make test` pass, reported with the **executed** count and the skip count. Both
  numbers, explicitly — this brief exists because those two were conflated once already.
- The invalid-configuration case is shown failing.
