# Model-Identity Edit API — OpenAPI spec for v3.6.0

**Status**: Draft for hub-team review
**Target ship**: llm-proxy2 v3.6.0 (Tue–Wed 2026-05-13/14)
**Counterpart**: coordinator-hub task #230 (Hub admin UI)

This document specifies `PUT /api/llm/models/{model_id}` — the
endpoint the Hub admin UI will call when an operator edits
`aliases` / `family` / `variant` for a model in the catalog scan
view.

## 1. Background

The v3.5.0 model-identity RFC introduced three editable fields on
each `ModelCapability` row: `aliases`, `model_family`,
`model_variant`. Today these are populated by:

1. The `scanner._fetch_model_list` auto-inference pass (v3.4.1+).
2. The existing per-(provider, model) admin endpoint
   `PUT /api/providers/{provider_id}/model-capabilities/{model_id:path}`
   (used by the proxy's own admin UI).

The Hub admin UI works at the **canonical-model** layer — it
shows the deduplicated catalog as exposed by `GET /v1/models`,
not per-(provider, model) rows. So Hub needs a corresponding
canonical-model-level PUT.

## 2. Implementation note (worth flagging)

`ModelCapability` rows are keyed `(provider_id, model_id)`. The
same canonical model_id can exist on multiple providers (e.g.
`claude-sonnet-4-6` lives on both `Devin-Anthropic-Max-Gmail` AND
`Devin-Anthropic-Max-VG`).

When the Hub PUTs `{aliases, family, variant}` for
`claude-sonnet-4-6`, the proxy applies the change to **every
ModelCapability row that matches** (canonical or alias). This is
the right semantic — aliases/family/variant describe the upstream
model itself, which is the same regardless of which provider
serves it.

If the Hub ever needs per-provider divergent aliases (rare), pass
the optional `?provider_id=<id>` query param to scope the write
to a single row. Default behavior is "apply to all matching".

## 3. OpenAPI 3.1 spec

```yaml
openapi: 3.1.0
info:
  title: llm-proxy2 — Model-Identity Edit API
  version: 3.6.0
  description: |
    Per-canonical-model edit of identity fields (aliases / family /
    variant). Companion to the existing GET /v1/models catalog endpoint.

paths:
  /api/llm/models/{model_id}:
    parameters:
      - name: model_id
        in: path
        required: true
        description: |
          Canonical model id OR any registered alias.
          Path-encoded — slashes ARE allowed (e.g.
          `x-ai/grok-3`, `openai/gpt-4o`).
        schema:
          type: string
          minLength: 1
          maxLength: 200
        example: "claude-sonnet-4-6"

    get:
      summary: Read model-identity fields for a canonical model
      description: |
        Returns the merged identity record for this model across
        ALL provider rows that match. If multiple rows have
        divergent identity (rare), the lowest-priority provider's
        row wins. Use `?provider_id=<id>` to read a specific row.

        Returns 404 if no ModelCapability row matches the path.

        Response carries an `ETag` header. Pass it back as
        `If-Match` on the matching PUT for optimistic concurrency.
      parameters:
        - name: provider_id
          in: query
          required: false
          schema: { type: string }
          description: Restrict the read to one provider's row.
      security:
        - adminSession: []
        - adminApiKey: []
      responses:
        "200":
          description: Identity record
          headers:
            ETag:
              schema: { type: string }
              description: |
                Quoted hash of the row state, e.g. `"abc123"`.
                Per-node — see §6 on cluster ETag drift.
          content:
            application/json:
              schema:
                $ref: "#/components/schemas/ModelIdentity"
        "404":
          $ref: "#/components/responses/NotFound"
        "401":
          $ref: "#/components/responses/Unauthorized"

    put:
      summary: Update model-identity fields for a canonical model
      description: |
        Updates `aliases`, `model_family`, `model_variant` on
        every ModelCapability row that matches `model_id` (canonical
        OR alias). Use `?provider_id=<id>` to scope the write to one
        row.

        Concurrency control via `If-Match` ETag.

        Returns the merged identity record (same shape as GET) and
        a fresh `ETag` reflecting the new state.
      parameters:
        - name: provider_id
          in: query
          required: false
          schema: { type: string }
          description: Restrict the write to one provider's row.
      security:
        - adminSession: []
        - adminApiKey: []
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: "#/components/schemas/ModelIdentityUpdate"
            examples:
              full:
                summary: Set all three fields
                value:
                  aliases: ["claude-3-7-sonnet", "claude-sonnet"]
                  family: "claude"
                  variant: "thinking-2026-q2"
              just-aliases:
                summary: Add aliases only (family/variant omitted = no change)
                value:
                  aliases: ["claude-3-7-sonnet"]
      parameters:
        - name: If-Match
          in: header
          required: true
          schema: { type: string }
          description: |
            ETag from the matching GET. Mismatch → 412.
        - name: provider_id
          in: query
          required: false
          schema: { type: string }
      responses:
        "200":
          description: Updated identity record
          headers:
            ETag:
              schema: { type: string }
              description: Fresh ETag after the write.
          content:
            application/json:
              schema:
                $ref: "#/components/schemas/ModelIdentity"
        "400":
          description: |
            Validation error (alias collision, alias > 64 chars,
            duplicate alias, alias contains whitespace, > 16
            aliases, family is empty string).
          content:
            application/json:
              schema:
                $ref: "#/components/schemas/Error"
        "404":
          $ref: "#/components/responses/NotFound"
        "412":
          description: |
            ETag mismatch (`If-Match` header doesn't match current
            row state). Refresh via GET and retry.
          headers:
            ETag:
              schema: { type: string }
              description: Current ETag, for the retry.
          content:
            application/json:
              schema:
                $ref: "#/components/schemas/Error"
        "401":
          $ref: "#/components/responses/Unauthorized"

components:
  securitySchemes:
    adminSession:
      type: apiKey
      in: cookie
      name: session
      description: |
        Admin session cookie issued by `POST /api/auth/login`.
    adminApiKey:
      type: http
      scheme: bearer
      bearerFormat: llmp-admin-*
      description: |
        Admin-scoped API key. v3.6.0 uses the standard admin
        scope (`key_type=admin`); v3.6.1 will add a narrower
        `key_type=admin-readonly-catalog` scope as a drop-in
        replacement (no contract change).

  schemas:
    ModelIdentity:
      type: object
      required: [model_id, aliases]
      properties:
        model_id:
          type: string
          description: Canonical model id (the name the operator edits).
          example: "claude-sonnet-4-6"
        aliases:
          type: array
          items: { type: string }
          maxItems: 16
          description: |
            Alternate spellings the proxy routes to the same upstream
            model. Each item must be 1-64 chars, no whitespace.
            Aliases cannot collide with another row's canonical id
            or alias.
          example: ["claude-3-7-sonnet", "claude-sonnet"]
        family:
          type: string
          nullable: true
          description: |
            Soft-validated against a known set (see §5). Empty
            string is rejected; missing/null is accepted (server
            derives family from canonical id at read time).
          example: "claude"
        variant:
          type: string
          nullable: true
          description: |
            Free-text. Fast-evolving field — no validation.
          example: "thinking-2026-q2"
        provider_count:
          type: integer
          description: |
            How many ModelCapability rows this identity record
            represents. >1 means the same model is served by
            multiple providers and the PUT will affect all of them
            unless `provider_id` is passed.

    ModelIdentityUpdate:
      type: object
      description: |
        All fields optional. Only fields present in the body are
        updated; missing fields preserve their current value
        (PATCH-like semantics on a PUT, intentionally — the Hub UI
        always loads the full record from GET first, so this is
        safe).
      properties:
        aliases:
          type: array
          items: { type: string }
          maxItems: 16
        family:
          type: string
          minLength: 1
        variant:
          type: string

    Error:
      type: object
      required: [detail]
      properties:
        detail:
          type: string
          example: "alias 'foo bar' contains whitespace"

  responses:
    Unauthorized:
      description: Auth missing or invalid
      content:
        application/json:
          schema:
            $ref: "#/components/schemas/Error"
    NotFound:
      description: No ModelCapability row matches the path
      content:
        application/json:
          schema:
            $ref: "#/components/schemas/Error"
```

## 4. Validation rules (hard reject → 400)

| Rule | Reason |
|---|---|
| `len(alias) < 1` or `> 64` | length bound |
| alias contains whitespace | clarity / paste-safety |
| `len(aliases) > 16` | upper bound to keep snapshot lookups O(small) |
| duplicate aliases in one row | obviously broken |
| alias collides with another row's canonical_id or alias | would cause routing ambiguity |
| `family == ""` (empty string) | use null/omit instead |

## 5. Family soft-validation (warn → 200 + `X-Warning` header)

```python
KNOWN_FAMILIES = {
    "claude", "gpt", "gemini", "grok",
    "llama", "mistral", "cohere", "deepseek",
}
```

If the operator sets `family` to something outside this set, the
PUT succeeds with status 200 but adds:

```
X-Warning: family "foo" is not in the known set. Saved anyway —
update KNOWN_FAMILIES if this should be canonical.
```

The Hub UI should render the X-Warning text as a yellow toast.
The constant is published here so the Hub can lift it into the
client and pre-validate without a round-trip.

## 6. Concurrency: ETag + If-Match

ETag is computed as a deterministic hash of the row state:

```python
import hashlib, json
def etag_for(cap: ModelCapability) -> str:
    state = {
        "model_id": cap.model_id,
        "aliases": sorted(cap.aliases or []),
        "family": cap.model_family,
        "variant": cap.model_variant,
        "updated_at": cap.updated_at.isoformat(),
    }
    h = hashlib.sha256(
        json.dumps(state, sort_keys=True).encode()
    ).hexdigest()[:16]
    return f'"{h}"'
```

When the GET reads multiple rows for one canonical model_id,
the ETag covers the merged state across all matching rows. PUT
recomputes after the write and returns the fresh ETag.

**Per-node ETag drift**: same caveat as `/lmrh/providers`
(documented in `docs/lmrh-2.0-bidirectional.md`). The ETag is
deterministic given the row state, but the Hub's load-balancer
may route GET → node A and PUT → node B, where node B is briefly
behind on cluster sync. Result: 412 on PUT.

**Handling**:
- Hub: catch 412, re-GET (lands on whichever node), re-PUT.
- One round-trip cost on the rare race. Worth the simplicity.
- If 412 rate >5% in production, add `?node=<id>` query param on
  GET so the Hub can manually pin (deferred to v3.6.x).

## 7. Concurrent multi-Hub writes (no 409)

Last-write-wins, mirroring existing cluster sync semantics:
- Each row carries `last_user_edit_at` (already exists).
- Two simultaneous PUTs from different Hubs both succeed locally.
- Cluster sync apply pass picks the row with the later
  `last_user_edit_at` and propagates it.
- The earlier write loses silently.

If stronger semantics ever needed (`?force=true` or 409 default),
that's a v3.6.x option — costs one more round-trip on rare collisions.

## 8. Examples

### 8.1 Add aliases for `claude-sonnet-4-6`

```http
GET /api/llm/models/claude-sonnet-4-6
Authorization: Bearer llmp-admin-...

200 OK
ETag: "a3b1c0f2d49e6781"
{
  "model_id": "claude-sonnet-4-6",
  "aliases": ["claude-3-7-sonnet"],
  "family": "claude",
  "variant": null,
  "provider_count": 2
}
```

```http
PUT /api/llm/models/claude-sonnet-4-6
Authorization: Bearer llmp-admin-...
If-Match: "a3b1c0f2d49e6781"
Content-Type: application/json

{
  "aliases": ["claude-3-7-sonnet", "claude-sonnet", "sonnet-4.6"],
  "variant": "thinking-2026-q2"
}

200 OK
ETag: "f8d217e0b3c45921"
{
  "model_id": "claude-sonnet-4-6",
  "aliases": ["claude-3-7-sonnet", "claude-sonnet", "sonnet-4.6"],
  "family": "claude",
  "variant": "thinking-2026-q2",
  "provider_count": 2
}
```

The PUT touched both `Devin-Anthropic-Max-Gmail` and
`Devin-Anthropic-Max-VG` rows.

### 8.2 Concurrency miss

```http
PUT /api/llm/models/x-ai/grok-3
If-Match: "stale1234567890ab"

412 Precondition Failed
ETag: "fresh67890fedcba0"
{ "detail": "ETag mismatch — refresh via GET and retry" }
```

### 8.3 Validation reject

```http
PUT /api/llm/models/openai/gpt-4o
If-Match: "..."

{ "aliases": ["gpt-4o", "  ", "gpt-4o-2024"] }

400 Bad Request
{ "detail": "aliases[1] is empty or whitespace-only" }
```

### 8.4 Family soft-warn

```http
PUT /api/llm/models/some-novel-model
If-Match: "..."

{ "family": "novel-architecture" }

200 OK
X-Warning: family "novel-architecture" is not in the known set. Saved anyway — update KNOWN_FAMILIES if this should be canonical.
ETag: "..."
{ "model_id": "some-novel-model", "family": "novel-architecture", ... }
```

## 9. Implementation notes (proxy side)

Touch points for v3.6.0:

| File | Change |
|---|---|
| `app/api/llm_models.py` (NEW) | Router for `GET/PUT /api/llm/models/{model_id}`. Mounted under `/api`. |
| `app/api/_etag.py` (NEW) | `etag_for_capability(cap)` + `compute_etag_for_canonical_model(rows)` helpers. |
| `app/main.py` | Register the new router. CORS expose `ETag` + `X-Warning` headers. |
| `app/auth/admin.py` | No change for v3.6.0 (existing admin scope is sufficient). v3.6.1 adds `key_type=admin-readonly-catalog`. |
| `tests/unit/test_v360_model_identity_put.py` (NEW) | Validation, concurrency, multi-row write, family soft-warn. |
| `docs/architecture.md` | Add §model-identity-edit-api section. |

`KNOWN_FAMILIES` lives in `app/routing/canonical.py` next to
`derive_family()`; the GET endpoint already imports from there,
the new validation lifts it.

## 10. Open questions for hub team

None at draft time — the contract was locked in the operator-forwarded
reply on 2026-05-09. The OpenAPI yaml above is the durable
representation of the same agreement; flag any drift and we'll
adjust before the v3.6.0 cut.

## 11. Path discrepancy — heads up

**Note for the hub team**: my reply on 2026-05-09 referenced
`PUT /api/llm/models/{model_id}`. The proxy doesn't have a route
prefix `/api/llm/` today (existing routes are `/v1/models` for
read and `/api/providers/.../model-capabilities/...` for the
per-row write). The new `/api/llm/models/{model_id}` route is
what v3.6.0 will introduce, and is the path the Hub UI should
target.

If you'd prefer a different prefix (e.g. `/api/catalog/models/...`
or `/api/v3.5/models/...`), say so before I cut v3.6.0 — easier to
change the path now than later.
