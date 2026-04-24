---
title: "LLM Model Routing Hints (LMRH) 1.0"
abbrev: LMRH
docname: draft-blagbrough-lmrh-00
category: std
ipr: trust200902

stand_alone: yes
pi: [toc, sortrefs, symrefs]

author:
  -
    name: D. Blagbrough
    organization: VoIP Guru
    email: dblagbro@voipguru.org

normative:
  RFC2119:
  RFC8174:
  RFC8941:

informative:
  RFC7231:
---

# Abstract

This document specifies the "LLM-Hint" request header field and the
"LLM-Capability" response header field for advertising and negotiating
routing preferences between a client and an intermediary that multiplexes
traffic across multiple Large Language Model (LLM) providers and models.

The header fields use the Structured Fields for HTTP format defined in
{{RFC8941}}. Clients use `LLM-Hint` to express soft preferences and hard
constraints on task, safety, modality, region, latency, cost, context-length,
and several other dimensions. Intermediaries echo the selected provider and
model in `LLM-Capability` together with a machine-readable record of which
dimensions could not be satisfied and why a particular candidate was chosen.

# Introduction

Modern LLM deployments routinely multiplex requests across many providers
(e.g. Anthropic, OpenAI, Google, xAI, self-hosted) and many models per
provider. The selection between these candidates is a policy decision that
depends on both the caller's intent (e.g. "I need a reasoning-heavy answer
with strict refusal behavior") and the caller's operational constraints (e.g.
"this tenant must not pay more than $0.02/1k tokens", "this user is in the
EU and data must not leave EU regions").

Today, these decisions are encoded in ad-hoc fields scattered across query
parameters, custom headers, and JSON body extensions specific to each
intermediary. There is no standard for expressing this intent at the
request-header layer, and no standard for the intermediary to report back
which trade-offs it made.

LMRH defines two header fields:

-   `LLM-Hint`: a request header carrying a list of dimension=value pairs,
    some of which MAY be hard constraints (`;require`).
-   `LLM-Capability`: a response header echoing the selected provider,
    model, and the set of hints that could not be satisfied.

LMRH is deliberately transport-agnostic at the application layer: it is
orthogonal to the OpenAI Chat Completions and Anthropic Messages wire
formats and works equally well with either.

## Requirements Language

{::boilerplate bcp14-tagged}

# Terminology

Client
: The party that originates the LLM request. Often a developer SDK or
  browser-side application.

Intermediary
: An HTTP server that receives a request from a Client, selects one or
  more upstream LLM providers, forwards the request (possibly with
  format translation), and returns the response. Also known as a
  "proxy", "router", or "gateway" in common practice.

Provider
: A party that serves LLM completions. Examples include Anthropic,
  OpenAI, Google, xAI, and self-hosted stacks such as Ollama.

Model
: A specific model identifier served by a Provider (e.g. `claude-sonnet-4`,
  `gpt-4o`, `gemini-2.5-pro`).

Dimension
: A named policy axis carried by `LLM-Hint` (e.g. `task`, `cost`,
  `safety-min`, `region`).

Hard constraint
: A dimension marked with the `;require` parameter. Intermediaries
  MUST NOT route to a candidate that fails to satisfy a hard constraint.

Soft preference
: A dimension without `;require`. Intermediaries SHOULD prefer candidates
  that satisfy it but MAY route to candidates that do not.

# The LLM-Hint Request Header

The `LLM-Hint` request header field is a Structured Field List
{{RFC8941, Section 3.1}} of Items. Each Item is a String token whose value
is a dimension=value pair, optionally followed by the `require` parameter:

~~~ abnf
LLM-Hint       = sf-list
sf-list        = sf-item *( OWS "," OWS sf-item )
sf-item        = sf-token *parameter
parameter      = ";" parameter-name [ "=" parameter-value ]
~~~

The value Item encodes `dimension=value` as an `sf-token`. A token is used
rather than a String so that dimension pairs are readable in raw HTTP traces
and easy to construct without quoting.

## Example

~~~ http
LLM-Hint: task=reasoning, safety-min=4;require, region=us, cost=economy
~~~

This request expresses four hints:

1.  A soft preference for a reasoning-oriented task.
2.  A hard constraint that the selected model's `safety` rating must be
    >= 4.
3.  A soft preference for a US region.
4.  A soft preference for an economy cost tier.

## Registered Dimensions

This section lists the initial registered dimensions. Implementations MUST
ignore unknown dimensions. Future dimensions may be added in subsequent
revisions of this specification, or through the IANA registry defined in
{{iana}}.

### task

Soft preference for the high-level task class the model is expected to
perform. Registered values:

| Value       | Intent                                            |
|-------------|---------------------------------------------------|
| chat        | Conversational response                           |
| reasoning   | Multi-step inference; may include analysis/code  |
| analysis    | Information synthesis and summarization          |
| code        | Source-code generation or modification            |
| creative    | Storytelling, prose generation                    |
| audio       | Audio modality (not yet widely implemented)       |
| vision      | Vision modality (multimodal)                      |

### safety-min / safety-max

Integer in the closed interval [1, 5] representing the caller's acceptable
band for the model's safety tier. `safety-min` is the lower bound; `safety-max`
is the upper bound. A model whose declared safety integer falls outside the
stated bounds MUST NOT be chosen when either dimension carries `;require`.

Safety tier semantics (implementation-defined, but recommended):

| Integer | Behavior                                       |
|---------|------------------------------------------------|
| 1       | Highly permissive; rarely refuses             |
| 2       | Permissive                                    |
| 3       | Standard (default for most production models) |
| 4       | Conservative                                  |
| 5       | Strictest; refuses wide classes of content    |

### refusal-rate (Wave 4 alias)

Human-readable alias that maps to a `safety` integer band. Registered values
and their mappings:

| Value      | Permitted safety band |
|------------|-----------------------|
| permissive | [1, 2]                |
| standard   | [2, 3]                |
| strict     | [3, 4]                |
| maximum    | [4, 5]                |

When both `refusal-rate` and `safety-min`/`safety-max` are present on the
same request, the intermediary MUST apply the most restrictive intersection
of the two bands.

### modality

Comma-separated list of required modalities. Registered values: `text`,
`vision`, `audio`.

### region

Geographic region in ISO-3166-1 alpha-2 or a named region string
(e.g. `us`, `eu`, `asia`). Intermediaries MUST route to a candidate that
lists the requested region when `;require` is present.

### latency

Latency tier. Registered values: `low`, `medium`, `high`.

### cost

Cost tier. Registered values: `economy`, `standard`, `premium`. Cost tiers
are ordered economy < standard < premium.

### context-length

Positive integer. Minimum context length (in tokens) the model must
support.

### max-ttft

Positive integer in milliseconds. Maximum acceptable time-to-first-token.
When `;require` is present, a candidate whose declared or measured TTFT
p95 exceeds this value MUST NOT be chosen.

### max-cost-per-1k

Positive decimal in USD. Maximum acceptable unit cost per 1,000 tokens
(input + output). When `;require` is present, a candidate whose declared
per-1k cost exceeds this value MUST NOT be chosen.

### effort, cascade, hedge, tenant, freshness (pass-through)

These dimensions are defined for pass-through to the underlying Provider.
Intermediaries MUST NOT use them for routing, but SHOULD forward them to
the selected Provider as Provider-specific kwargs (e.g. OpenAI
`reasoning_effort`, Anthropic extended-thinking budget tokens).

## The ;require Parameter

A dimension marked with `;require` is a hard constraint. An intermediary
MUST NOT select a candidate that violates a hard constraint. If no
candidate satisfies all hard constraints, the intermediary MUST return
HTTP 503 (Service Unavailable) with a body that identifies which
constraints could not be satisfied (see {{iana}}).

Dimensions without `;require` are soft preferences. The intermediary
SHOULD rank candidates according to how well they satisfy soft
preferences, but MAY choose a lower-ranked candidate for reasons of
availability or cost.

# The LLM-Capability Response Header

The `LLM-Capability` response header is a Structured Field Dictionary
{{RFC8941, Section 3.2}} that echoes what the intermediary actually did:

~~~ abnf
LLM-Capability = sf-dictionary
~~~

## Required Keys

| Key               | Value type | Description                          |
|-------------------|------------|--------------------------------------|
| `v`               | Integer    | Protocol version; MUST be 1          |
| `provider`        | Token      | Provider type (e.g. anthropic)       |
| `model`           | Token      | Selected model identifier            |
| `chosen-because`  | Token      | Reason for selection; see below      |

### chosen-because

One of the following tokens:

-   `score`: This candidate had the highest soft-preference score.
-   `hard-constraint`: Only one candidate satisfied all hard constraints.
-   `fallback`: The top-ranked candidate was unavailable; this is a
    fallback.
-   `cheapest`: Cascade-routing cheap-first step chose this candidate.
-   `p2c`: Power-of-two-choices tiebreaker picked this among
    essentially-tied candidates.

## Optional Keys

### unmet

Inner-list of Tokens. Names the dimensions the selected candidate does
not satisfy. Absent when all soft preferences were satisfied.

Example: `unmet=(cost region)`

### region, cost, latency, safety, context-length

Optional introspection: the intermediary MAY echo the declared values for
the selected candidate.

# The LLM-Hint-Set Response Header

The `LLM-Hint-Set` response header is an optional diagnostic that echoes the
parsed input hints as the intermediary understood them. This is useful when
the client is debugging its own LMRH serialization.

~~~ http
LLM-Hint-Set: task=reasoning, safety-min=3, cost=economy
~~~

The `LLM-Hint-Set` header MUST NOT carry dimensions the intermediary did
not recognize.

# Examples

## Soft preferences, no hard constraints

Request:

~~~ http
POST /v1/messages HTTP/1.1
Host: proxy.example
LLM-Hint: task=reasoning, cost=economy, region=us
Content-Type: application/json
~~~

Response:

~~~ http
HTTP/1.1 200 OK
LLM-Capability: v=1, provider=anthropic, model=claude-haiku-4-5,
    chosen-because=score, cost=economy
LLM-Hint-Set: task=reasoning, cost=economy, region=us
~~~

## Hard constraint satisfied

Request:

~~~ http
LLM-Hint: task=reasoning, safety-min=4;require, max-cost-per-1k=0.02;require
~~~

Response:

~~~ http
LLM-Capability: v=1, provider=anthropic, model=claude-sonnet-4-5,
    chosen-because=hard-constraint, unmet=(cost)
~~~

Here the caller required a safety integer >= 4 AND a unit cost below
$0.02/1k tokens. The intermediary chose a Sonnet-class model that satisfies
both hard constraints, but notes in `unmet` that the soft `cost=economy`
preference was sacrificed.

## Hard constraint unsatisfiable

Request:

~~~ http
LLM-Hint: region=antarctica;require
~~~

Response:

~~~ http
HTTP/1.1 503 Service Unavailable
Content-Type: application/json

{
  "type": "no_provider_satisfies_hard_constraints",
  "unsatisfied": ["region"]
}
~~~

# IANA Considerations     {#iana}

This specification establishes the "LMRH Dimensions" registry. New
dimensions can be added via Specification Required {{RFC8126}}. Each
entry consists of:

-   Dimension name (Token, per Structured Fields).
-   Value type: Token, String, Integer, or Decimal.
-   Whether the `;require` parameter applies.
-   Reference.

The initial registry is populated with the dimensions defined in
{{registered-dimensions}}.

This specification also registers two HTTP Header fields:

-   `LLM-Hint` (request), this document.
-   `LLM-Capability` (response), this document.

# Security Considerations

The `LLM-Hint` header can carry policy signals — particularly `region`,
`tenant`, and `cost` — that affect how a request is routed. Intermediaries
MUST NOT trust these signals to make access-control decisions that are
privileged with respect to the calling identity: routing-policy enforcement
MUST be driven by the intermediary's own authenticated identity of the
caller, not by values in `LLM-Hint`.

The `pass-through` dimensions (`effort`, `cascade`, `hedge`, `tenant`,
`freshness`) are forwarded to the selected Provider as Provider-specific
kwargs. Intermediaries SHOULD apply rate-limiting and content-length
validation to these values before forwarding to prevent abuse of
compute-intensive reasoning budgets.

`LLM-Capability` response values can leak information about the
intermediary's internal provider topology (e.g. that a fallback occurred,
or that the selected provider is a specific vendor). Intermediaries
deployed in tenant-isolation contexts MAY omit the `chosen-because`,
`unmet`, and per-candidate introspection keys for untrusted callers.

# Privacy Considerations

`LLM-Hint` values are not themselves end-user data, but the presence or
absence of specific dimensions (e.g. `tenant=acme-corp`) MAY disclose
organizational information when traversing intermediate caches or CDNs.
Deployments that require confidentiality of routing policy MUST use TLS
and MUST NOT log `LLM-Hint` to untrusted stores.

# References

## Normative References

{::refs}
RFC2119
RFC8174
RFC8941
{:/refs}

## Informative References

{::refs}
RFC7231
{:/refs}

# Reference Implementation

An open-source reference implementation of this specification is available
under the Apache-2.0 license at
`https://github.com/dblagbro/llm-proxy-manager` (see `app/routing/lmrh.py`).
The implementation ships as a production-grade Python FastAPI service and
is deployed across a three-node cluster. It exercises every dimension in
this document and both header fields.

# Acknowledgments

The author thanks the operators of early adopter deployments for feedback
on the `refusal-rate` alias and the `chosen-because` taxonomy.
