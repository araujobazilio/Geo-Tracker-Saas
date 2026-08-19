# Stable Versioned Prompt System

## Overview

GEO Tracker generates AI-search prompts from project configuration (brand,
keywords, competitors, target market) and runs scans against them. For scan
results to be **reproducible** and **auditable**, the exact prompts used for a
given scan must remain stable and recoverable forever.

The Stable Versioned Prompt System guarantees this by treating a complete
generation of prompts as a first-class, **immutable, versioned entity** — the
`PromptSet`. Rather than overwriting prompts when project configuration
changes, the system creates a *new* `PromptSet` version and supersedes the old
one. Old prompt sets are never modified or deleted, so any historical scan can
always be tied back to the precise prompts that produced its results.

This design delivers two properties that are essential for Share-of-Voice
measurement:

- **Reproducibility** — the same project inputs + `generator_key` always
  produce byte-identical prompt text, so a scan can be re-run or reasoned about
  long after it happened.
- **Auditability** — every scan references a specific `PromptSet` version, and
  that version (and all of its prompts) is permanently retrievable.

Prompt generation is **deterministic and template-based** — it does *not* call
external AI APIs. The generator key `deterministic-template-v2` identifies the
current template algorithm; recording it on each `PromptSet` makes it possible
to know exactly how a set was produced.

---

## PromptSet entity

A `PromptSet` is a first-class entity representing one complete generation of
prompts for a project. Each `PromptSet` is immutable once created.

Model: `app/models/prompt_set.py` (`prompt_sets` table)

| Field                | Type             | Description                                                                 |
| -------------------- | ---------------- | --------------------------------------------------------------------------- |
| `id`                 | UUID (PK)        | Unique identifier.                                                          |
| `project_id`         | UUID (FK)        | Owning project. `ON DELETE RESTRICT`.                                       |
| `version`            | int              | Monotonically increasing per project, starting at `1`. `version > 0`.      |
| `input_revision`     | int              | The `Project.prompt_input_revision` captured at generation time. `> 0`.    |
| `status`             | `PromptSetStatus`| `ACTIVE` or `SUPERSEDED`.                                                   |
| `generator_key`      | str              | Algorithm that produced the prompts (currently `deterministic-template-v2`). |
| `created_by_user_id` | UUID (FK, nullable) | User who triggered generation. `ON DELETE SET NULL`.                    |
| `created_at`         | datetime (tz)    | Row creation timestamp.                                                     |
| `activated_at`       | datetime (tz, nullable) | When the set became `ACTIVE`.                                       |

### Constraints

- `UNIQUE(project_id, version)` — `uq_prompt_sets_project_version`. Versions
  are unique within a project.
- `CHECK(version > 0)` and `CHECK(input_revision > 0)`.
- **Partial unique index** `uq_prompt_sets_one_active_per_project` on
  `project_id` with `WHERE status = 'ACTIVE'` — enforces *at most one* `ACTIVE`
  prompt set per project.

```python
__table_args__ = (
    UniqueConstraint("project_id", "version", name="uq_prompt_sets_project_version"),
    CheckConstraint("version > 0", name="ck_prompt_sets_version_positive"),
    CheckConstraint("input_revision > 0", name="ck_prompt_sets_input_revision_positive"),
    Index(
        "uq_prompt_sets_one_active_per_project",
        "project_id",
        unique=True,
        postgresql_where=text("status = 'ACTIVE'"),
    ),
)
```

---

## PromptSet statuses

Defined in `app/core/enums.py` (`PromptSetStatus`):

| Status       | Meaning                                              |
| ------------ | --------------------------------------------------- |
| `ACTIVE`     | The current prompt set used by scans. At most **one** per project (enforced by the partial unique index). |
| `SUPERSEDED` | Replaced by a newer `ACTIVE` set. Retained permanently for historical access. |

Lifecycle:

```
ACTIVE ──(regeneration)──► SUPERSEDED
                              ▲
                              │  (new set becomes ACTIVE)
```

A `SUPERSEDED` set is never deleted and never returns to `ACTIVE`.

---

## Generator key

Each `PromptSet` records the `generator_key` that produced it. The current key is:

```
deterministic-template-v2
```

This key identifies the deterministic template algorithm in
`app/services/prompt_generation_service.py`. The service does **not** call
external AI APIs — it produces stable prompt text purely from project
configuration. Recording the key on every set ensures reproducibility: the same
inputs plus this key always yield the same prompt text.

### Generator version history

| Key                       | Description                                                                 |
| ------------------------- | --------------------------------------------------------------------------- |
| `deterministic-template-v1` | Initial generator. NON_BRANDED variants 1–3 used the same template, producing duplicate text. |
| `deterministic-template-v2` | Current generator. Each of the 5 variants uses a distinct template with a different search intent. |

### Generator-version regeneration semantics

Regeneration checks both `input_revision` **and** `generator_key`. If the
current `ACTIVE` set's `generator_key` does not match the current
`GENERATOR_KEY`, the set is considered stale — even if `input_revision` matches
— and regeneration will create a new version.

This means that when the generator is upgraded (e.g. v1 → v2), existing
projects with v1 prompt sets become **eligible for regeneration** even if
`prompt_input_revision` has not changed. Calling `regenerate_prompt_set` on
such a project will:

1. Create a new `PromptSet` with `generator_key = deterministic-template-v2`.
2. Supersede the old v1 set (`SUPERSEDED`).
3. **Not** change `Project.prompt_input_revision` (generator changes are
   separate from input changes).

**Historical v1 preservation**: existing `PromptSet` records with
`generator_key = deterministic-template-v1` are never modified or deleted.
Their `Prompt` rows remain unchanged forever, ensuring that historical scans
can always be tied back to the exact prompts used at the time.

Regeneration also checks the generator key: if the current `ACTIVE` set is fresh
*and* its `generator_key` matches the current key, regeneration is a no-op (see
[Regeneration flow](#regeneration-flow)).

---

## Prompt generation

For each **active** keyword, the generator produces exactly **5 prompt
variants** (`PROMPTS_PER_KEYWORD = 5`), each with a **distinct prompt text**:

| Variant (`variant_index`) | `prompt_type` | Intent                          | Content                                                |
| ------------------------- | ------------- | ------------------------------- | ----------------------------------------------------- |
| 1                         | `NON_BRANDED` | Recommendations                 | "What are the best options for {keyword} {market}?"                    |
| 2                         | `NON_BRANDED` | Comparison / shortlist          | "Which {keyword} solutions should I compare {market}?"                |
| 3                         | `NON_BRANDED` | Decision criteria               | "What should I look for when choosing {keyword} {market}?"            |
| 4                         | `BRANDED`     | Brand evaluation                | Includes the project's primary brand name.            |
| 5                         | `COMPETITOR`  | Competitive comparison          | Includes brand + up to 3 competitors. If the project has **no active competitors**, variant 5 falls back to `NON_BRANDED` with a buyer-oriented intent. |

The competitor subset is chosen deterministically: the first 3 active
competitors ordered by `(name, id)`.

### Distinctness guarantee

All 5 prompts for a single keyword **must** have distinct text after
normalized comparison. This is validated immediately after generation:

```python
normalized_texts = [normalize_text_for_comparison(s.text) for s in specs]
unique_texts = set(normalized_texts)
if len(unique_texts) != PROMPTS_PER_KEYWORD:
    raise ValidationError("Generated prompts are not distinct...")
```

If deterministic generation somehow produces duplicate prompts, a
`ValidationError` is raised and the prompt set is **not** persisted. This
ensures that future scans never spend multiple AI checks asking the same
question.

### Commercial intent

`commercial_intent` is **derived from the keyword's `funnel_stage`** — it is
not set manually:

| `funnel_stage` | `commercial_intent` |
| -------------- | ------------------- |
| `PURCHASE`     | `true`              |
| `CONSIDERATION`| `true`              |
| `AWARENESS`    | `false`             |
| `None` (unknown) | `false` (conservative default) |

```python
def _commercial_intent_from_funnel(funnel_stage: FunnelStage | None) -> bool:
    return funnel_stage in (FunnelStage.PURCHASE, FunnelStage.CONSIDERATION)
```

### Example generated prompts (English)

```
NON_BRANDED 1 (recommendations):
  What are the best options for email marketing software in the US?
  Please list and compare the top alternatives.

NON_BRANDED 2 (comparison / shortlist):
  Which email marketing software solutions should I compare in the US,
  and what are the main differences between them?

NON_BRANDED 3 (decision criteria):
  What should I look for when choosing email marketing software in the US,
  and which options stand out?

BRANDED:
  Is Acme a good option for email marketing software in the US?
  What strengths and alternatives should I consider?

COMPETITOR:
  Compare Acme, Mailchimp and Brevo for email marketing software in the US.
  What are the main differences and which would you recommend?
```

When there are **no active competitors**, variant 5 becomes a buyer-oriented
NON_BRANDED prompt:

```
NON_BRANDED 5 (buyer-oriented, no competitor):
  If I were choosing email marketing software in the US today,
  which options would you shortlist and why?
```

### Determinism

Generation is fully deterministic: the same project configuration, keywords,
competitors, and `generator_key` always produce byte-identical prompt text.
There is no randomness and no external model call. Each prompt is also validated
against a `MAX_PROMPT_LENGTH` of 1000 characters.

---

## Language support

The generator supports two language families, derived from
`Project.target_language` via `get_language_family`:

| Family | Accepted `target_language` values |
| ------ | --------------------------------- |
| `en`   | `en`, `en-US`, `en-GB`            |
| `pt`   | `pt`, `pt-BR`, `pt-PT`            |

Templates select phrasing and market-context prepositions based on the family
(e.g. `in {country}` for English, `no {country}` for Portuguese). Any
unsupported language raises a `ValidationError`.

---

## NON_BRANDED safety

`NON_BRANDED` prompts are the basis for unbiased Share-of-Voice measurement, so
they **MUST NOT** contain:

- the project's **brand name**,
- any of the project's **brand aliases**,
- any **competitor name**, or
- any **competitor domain** (full domain or primary label, e.g. `mailchimp`
  from `mailchimp.com`).

Every `NON_BRANDED` prompt is validated by `_validate_non_branded_safety`
immediately after generation. The check is accent-insensitive and
case-insensitive (via `normalize_text_for_comparison`). If a forbidden token is
found, a `ValidationError` is raised and the prompt set is not created.

```python
_validate_non_branded_safety(text, brand_name, brand_aliases, competitors)
```

This safety check also applies to variant 5 when it falls back to `NON_BRANDED`
(no active competitors).

---

## Staleness

A `PromptSet` is **stale** when its `input_revision` no longer matches the
project's current `prompt_input_revision`:

```python
def is_stale(self, prompt_set: PromptSet, project: Project) -> bool:
    return prompt_set.input_revision != project.prompt_input_revision
```

The project's `prompt_input_revision` is bumped whenever prompt-affecting
configuration changes (e.g. brand, keywords, competitors, target market). A
stale set is still `ACTIVE` and still usable, but it signals that regeneration
would produce a different set.

---

## Regeneration flow

`PromptSetService.regenerate_prompt_set` (in
`app/services/prompt_set_service.py`) manages the lifecycle. To prevent two
concurrent regenerations from creating conflicting versions or two `ACTIVE`
sets, it locks the `Project` row with `SELECT ... FOR UPDATE`.

Flow:

1. Lock the project row (`get_in_workspace_for_update`). If not found →
   `ConflictError`.
2. Load the current `ACTIVE` set.
3. **Fresh-and-current check**: if the current set's `input_revision` matches
   the project's `prompt_input_revision` **and** its `generator_key` equals the
   current `GENERATOR_KEY` → return the existing set. **No new version is
   created.**
4. Load active keywords and competitors.
5. If there are **no active keywords** → `ConflictError`
   (`"Cannot regenerate prompts: project has no active keywords."`).
6. Compute `next_version = max_version_by_project + 1`.
7. Mark the current `ACTIVE` set as `SUPERSEDED`.
8. Create the new `PromptSet` (version `next_version`, status `ACTIVE`,
   `input_revision` = current project revision, `generator_key` =
   `deterministic-template-v2`) and generate its prompts.
9. Commit.

```python
current = self._prompt_set_repo.get_active_by_project(project.id)
if current is not None:
    is_fresh = current.input_revision == project.prompt_input_revision
    is_current_generator = current.generator_key == GENERATOR_KEY
    if is_fresh and is_current_generator:
        self._session.commit()
        return current  # no new version

# ...
next_version = self._prompt_set_repo.max_version_by_project(project.id) + 1
if current is not None:
    current.status = PromptSetStatus.SUPERSEDED
new_set = self._create_prompt_set(project, keywords, competitors, next_version, ...)
self._session.commit()
return new_set
```

Key guarantees:

- **Regeneration while fresh returns the current set** — no new version, no
  supersession.
- **Regeneration with no active keywords raises `ConflictError`.**
- **Old prompts are unchanged** — supersession only flips the status of the old
  set; its `Prompt` rows are never edited.

The initial `PromptSet` (version 1) is created during project onboarding via
`generate_initial_prompt_set`.

---

## Historical access

Old prompt sets are **never deleted**. Superseded sets remain available forever
so that historical scans can always be tied back to the exact prompts used at
the time. This is reinforced at the database level:

- `prompts.prompt_set_id` uses `ON DELETE RESTRICT`.
- `prompts.project_keyword_id` uses `ON DELETE RESTRICT` (keywords are
  deactivated rather than hard-deleted in normal flows).
- `prompt_sets.project_id` uses `ON DELETE RESTRICT`.

Any historical version can be retrieved by its version number (see
[API endpoints](#api-endpoints)).

---

## Prompt model

Each individual prompt is a row in the `prompts` table
(`app/models/tracking.py`). Prompts are immutable (no `updated_at`).

| Field                  | Type                | Notes                                                    |
| ---------------------- | ------------------- | -------------------------------------------------------- |
| `id`                   | UUID (PK)           |                                                          |
| `prompt_set_id`        | UUID (FK)           | `ON DELETE RESTRICT`.                                    |
| `project_keyword_id`   | UUID (FK)           | `ON DELETE RESTRICT`.                                    |
| `variant_index`        | int                 | 1–5. `> 0`.                                              |
| `text`                 | str (1000)          | The prompt text.                                         |
| `prompt_type`          | `PromptType`        | `NON_BRANDED`, `BRANDED`, or `COMPETITOR`.               |
| `intent`               | str (nullable)      | Keyword intent.                                          |
| `funnel_stage`         | `FunnelStage` (nullable) | `AWARENESS`, `CONSIDERATION`, `PURCHASE`.            |
| `persona`              | str (nullable)      | Target audience.                                         |
| `target_country`       | str (nullable)      |                                                          |
| `target_language`      | str (nullable)      |                                                          |
| `commercial_intent`    | bool                | Derived from `funnel_stage`.                             |
| `active`               | bool                |                                                          |
| `created_at`           | datetime (tz)       |                                                          |

Constraints:

- `UNIQUE(prompt_set_id, project_keyword_id, variant_index)` —
  `uq_prompts_set_keyword_variant`.
- `CHECK(variant_index > 0)`.

---

## API endpoints

All endpoints are scoped under the projects router
(`app/routers/api/projects.py`). The base path is
`/api/v1/workspaces/{workspace_id}/projects/{project_id}`.

| Method | Path                          | Description                                      | Auth                  |
| ------ | ----------------------------- | ------------------------------------------------ | --------------------- |
| `GET`  | `/prompt-sets`                | List all prompt sets for the project (newest first). | Membership        |
| `GET`  | `/prompt-sets/current`        | Get the current `ACTIVE` set with its prompts.   | Membership            |
| `GET`  | `/prompt-sets/{version}`      | Get a specific version with its prompts.         | Membership            |
| `POST` | `/prompt-sets/regenerate`     | Regenerate the prompt set.                       | `ADMIN` or `OWNER`    |

### Responses

`PromptSetSummaryResponse` (list / regenerate):

```json
{
  "id": "uuid",
  "project_id": "uuid",
  "version": 2,
  "input_revision": 4,
  "status": "ACTIVE",
  "generator_key": "deterministic-template-v2",
  "created_at": "2025-01-01T00:00:00Z",
  "activated_at": "2025-01-01T00:00:00Z",
  "prompt_count": 25,
  "is_stale": false
}
```

`PromptSetDetailResponse` (current / by version) extends the summary with a
`prompts` array of `PromptResponse` objects, each containing the prompt `text`,
`prompt_type`, `variant_index`, `funnel_stage`, `commercial_intent`, etc.

### Errors

- `404 NotFoundError` — no active prompt set, or requested version does not exist.
- `409 ConflictError` — project not found, or regeneration attempted with no
  active keywords.
- `422 ValidationError` — unsupported language, NON_BRANDED safety violation, or
  prompt exceeds max length.

### Example: regenerate

```http
POST /api/v1/workspaces/{workspace_id}/projects/{project_id}/prompt-sets/regenerate
Authorization: Bearer <token>
```

If the current set is already fresh, the response is the existing `ACTIVE` set
(no version bump). If stale, a new version is created and the old set is marked
`SUPERSEDED`. The action is recorded in the audit log as `PROMPT_SET_CREATED`.
