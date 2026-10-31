# Code style

Write code a new teammate can read top to bottom without a guide.

## Comments

- **Say why, not what.** The code already says what it does.
- **One line is the norm**, two or three at most. No essays, no history lessons, no "observed in the wild" stories.
- No banner comments (`# -------`), no restating the function name, no commented-out code.
- Put design rationale in `docs/guide/` or `docs/plans/`, not in the code.

```python
# Bad
# A stops-only source has no calendar and no trips, so every service-date path
# below would raise inside partridge. Branch out here, BEFORE the route filter
# and the service resolution, so the stops are assigned unconditionally ...
if source.is_stops_only:

# Good
# Stops-only sources have no calendar; handle them before service-date logic.
if source.is_stops_only:
```

## Docstrings

- Module: one sentence on what the module is for. Add a short paragraph only if the rules are not obvious from the code.
- Function: a one-line summary. Add `Args:` / `Returns:` / `Raises:` (Google style) only when a reader would otherwise have to guess.
- Keep plain text. Avoid Sphinx roles (`:func:`, `:data:`) unless needed; a broken role fails the `-W` docs build.
- Pydantic models: a one-line class docstring; short trailing `#` comments on fields only when the meaning is not obvious.

```python
def find_equivalences(stops, route_stops, routes, distance_m=30.0) -> pd.DataFrame:
    """Return accepted alias pairs (stop_uid_a < stop_uid_b), one row per pair."""
```

## Readable code

- Names say what a thing is: `served_stops`, not `s2`; `walk_minutes`, not `wm`.
- Functions do one thing and fit on a screen (aim for < 40 lines). Extract a helper when a block needs a comment to explain it.
- Early returns over deep nesting.
- Constants at module top in `UPPER_CASE`, with a one-line comment if the value needs a reason.
- No magic numbers inside logic; name them.
- Prefer pandas vector operations over row loops; prefer one clear merge over chained clever ones.
- Type hints on public functions.

## Clean-code rules for this project

- **Never raise for a bad feed.** Turn problems into `Finding`s; keep the per-feed `try/except` boundaries.
- **Keep functions pure where possible.** `analysis/*` builds frames; only `storage.py` / `db.py` touch the database; only `write_*` functions touch the disk.
- **Deterministic output:** sort before writing, use `kind="mergesort"` for stable sorts, never depend on dict or set order.
- **Idempotent DB writes:** delete-and-reload per feed, truncate-and-reload for cross-feed tables.
- **One source of truth for defaults:** define the constant once (e.g. `WITHIN_FEED_DISTANCE_M`) and reuse it in configs; the CLI literal must match and a test guards it.
- **Behaviour change = test + docs.** Add or update a test and the README / `docs/guide/` text in the same change.
- **Don't refactor and change behaviour in one step.** When cleaning code, check the AST is unchanged (see below) and the tests still pass.

## Safe cleanup check

To prove a comment/docstring cleanup did not change behaviour, compare the AST with docstrings removed:

```python
import ast

def code_only(source: str) -> str:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                and isinstance(getattr(body[0], "value", None), ast.Constant) \
                and isinstance(body[0].value.value, str):
            node.body = body[1:] or [ast.Pass()]
    return ast.dump(tree)

assert code_only(old_source) == code_only(new_source)
```
