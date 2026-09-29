<!-- What this changes and why. Link the issue it closes, if there is one. -->

## Checklist

What each item means is in [CONTRIBUTING.md](https://github.com/ianderso/gramps-evidence-mcp/blob/main/CONTRIBUTING.md#what-a-change-carries).

- [ ] A test that fails without this change.
- [ ] `uv run ruff check .`, `uv run ruff format --check .` and `uv run pytest` pass.
- [ ] A tool that records a fact takes a citation, or records it as `UNSOURCED`.
- [ ] Every write goes through `service._mutate()`; nothing PUTs a partial object.
- [ ] Bulk output leaves out private records and probably-living people unless `expose_private` is set.
- [ ] Failures come back as an `error` result, not an exception.
- [ ] Tool descriptions are written for the model and fit under `DESCRIPTION_BUDGET`. Raising it takes a pull request of its own, saying why.
- [ ] A new tool declares annotations saying whether it reads, adds, or changes and removes.
- [ ] If a tool or parameter changed: `tests/fixtures/tool_schema.json` is regenerated and the README tables are updated.
- [ ] If this relies on how gramps-webapi answers: what was observed, on which version and when, is recorded in `docs/PITFALLS.md` or beside the code, and no test data comes from a real tree.
- [ ] A `CHANGELOG.md` entry, if users will notice the change.
