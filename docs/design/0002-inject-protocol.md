# Design 0002: the inject protocol

**Status:** draft · **Scope:** one wire contract (Contract C), the built-in
runtime that speaks it, and the conformance canary that ratifies an
implementation.

This document specifies **Contract C — the Wind Tunnel inject protocol**: a
fixed HTTP wire contract between a Wind Tunnel driver and an agent process.
An agent framework that implements two routes — one to inject a user turn,
one to reset state — gets benched by the built-in runtime with no driver
code at all. The pitch is deliberately MCP-shaped: *implement this one
shape*, not *configure our adapter*.

## Why a fixed contract, not a configurable mapper

The obvious alternative is a configurable `HttpRuntime`: let each adopter
map their own payload field names, response keys, and reset route in
config. It was rejected on direct evidence: two inject endpoints built by
the *same author*, under the *same conventions*, with the *first endpoint
as an explicit template*, still drifted — on the response key name, on
default ports, on payload field names. Strangers configuring a payload
mapper would drift unboundedly, and every mapping config is a place where
a bench silently tests the wrong thing.

A fixed contract converts that entire failure class into a binary:
*conforms or doesn't*. A wire contract is machinery; a configurable mapper
is hope with extra steps.

## The session model

The inject wire is **stateful on the endpoint side**. Each inject request
carries only the *newest* user turn (`text`), not the conversation history;
the agent process owns its own conversation memory, keyed by `session_id`.
This is the natural shape for agent frameworks (they already hold sessions)
and it is exactly why the reset route is load-bearing: state the bench
cannot see must still be state the bench can destroy.

Consequences, all normative:

- **The driver mints `session_id`** (an opaque string). The endpoint must
  treat an unknown `session_id` as a fresh session — no registration
  handshake.
- Endpoint-internal identity (user ids, chat ids, tenant ids) is the
  endpoint's business. It may derive them from `session_id` however it
  likes; they never appear on the wire.
- Requests within a session are **serialized**: the driver sends one
  inject at a time per session and waits for the response. Concurrent
  injects into one session are out of scope for version 1.

## Contract C — the wire

### Transport

HTTP/1.1, JSON bodies, UTF-8. Two routes:

| Route | Method | Purpose |
|---|---|---|
| `/wt/inject` | POST | Deliver one user turn, get the agent's reply |
| `/wt/reset`  | POST | Destroy all bench-visible state, synchronously |

The spec'd default port is **8647**. Runtimes must allow overriding the
base URL, but a conforming endpoint that binds its default to 8647 works
with a zero-config driver — spec'd defaults exist to kill exactly the kind
of port drift observed in practice.

Authentication is out of scope for version 1. The bench assumes a trusted
network (localhost or an isolated compose network); if you need transport
auth, put it in a reverse proxy, not in the contract.

### Versioning

Every request **and** every response carries the protocol version as an
integer field:

```json
{ "wt_inject": 1 }
```

Same discipline as `windtunnel_interchange` (Contract A) and
`windtunnel_universe` (Contract B). The response echo is deliberate: a
driver validating the echo catches a *stale server*, not just a stale
client. Within version 1, changes are additive only and unknown fields
must be ignored by both sides (the `Trace._from_dict` forward-tolerance
discipline). Anything breaking bumps the integer.

### Inject request

```json
{
  "wt_inject": 1,
  "session_id": "wt-3f9a1c2e",
  "text": "Which client ordered the misrouted pallet?",
  "timeout_s": 120
}
```

Field notes:

- `wt_inject` — protocol version, integer, required.
- `session_id` — opaque string, required, minted by the driver. Unknown
  value ⇒ fresh session.
- `text` — the user turn, string, required. Only the newest turn; history
  lives endpoint-side (§ The session model).
- `timeout_s` — number, required. The endpoint's own deadline for
  producing a response. The endpoint should enforce it and return an
  agent-level error envelope (below) when it expires, rather than letting
  the transport time out.

### Inject response

```json
{
  "wt_inject": 1,
  "reply": "Bluewing Logistics — order #4417, flagged at the Reno hub.",
  "tool_calls": [
    { "name": "client_lookup", "arguments": { "query": "misrouted pallet" } },
    { "name": "order_status",  "arguments": { "order_id": "4417" } }
  ]
}
```

Field notes:

- `wt_inject` — required, must equal the request's version (§ Versioning).
- `reply` — the agent's final user-facing text, string, required. May be
  empty only when `error` is set.
- `tool_calls` — **required even when empty.** An omitted key and `[]`
  mean different things: `[]` is a conforming "the agent called nothing";
  a missing key is a contract violation. Without this rule, "agent used no
  tools" and "endpoint forgot to surface them" are indistinguishable —
  which is precisely the silent-wrong-thing failure this contract exists
  to kill. Entries are **ordered** (invocation order) and **complete**
  (every intermediate call, not just the last): this is the transcript
  half of the trajectory evidence rule the runtime guide already treats as
  the single most important correctness property of a driver.
- `tool_calls[*].name` — the bare tool name as the agent invoked it,
  string, required.
- `tool_calls[*].arguments` — **a JSON object, never a stringified JSON
  blob.** The OpenAI wire convention of string-encoded arguments is a
  guaranteed drift point (half of everyone's reference code does it each
  way), so the wire pins the object form. Conversion to whatever the SPI
  `Response` needs — OpenAI-style stringification included — is the
  *driver's* job (§ Layering).
- `error` — optional string, non-empty when present (§ Errors and
  timeouts).

### Errors and timeouts

Failure paths are part of the contract; deterministic machinery includes
its failure cases or it is not deterministic.

- **Agent-level failure** (the agent ran and failed: model error, its own
  `timeout_s` expiry, tool crash): HTTP **200** with a full envelope —
  `wt_inject` and `tool_calls` still required (listing whatever calls were
  witnessed before the failure), `error` set to a non-empty string,
  `reply` may be empty. This is a *scoreable* outcome: the driver records
  the error verbatim into the trace and must never fabricate a reply.
- **Contract-level failure** (non-200 status, unparseable body, missing
  required key, version mismatch): the driver fails the run with the
  status and body as detail. **No silent retry** — a retry that happens to
  succeed hides a flaky endpoint from the bench, and hiding flakiness from
  a reliability bench is self-defeating.
- **Driver deadline**: the driver's transport timeout is `timeout_s` plus
  a small fixed grace (recommended: 5 s) so a conforming endpoint always
  gets the chance to convert its own expiry into an agent-level error
  envelope. If the grace also expires, the run fails — again, no retry.

### Reset

```json
POST /wt/reset
{ "wt_inject": 1 }
```

→ `200` `{ "wt_inject": 1 }`, returned **only after state is actually
gone.**

- **Synchronous.** The 200 is a completion signal, not an acknowledgement.
  An endpoint whose state spans several stores (transcript files,
  in-process caches, approval queues, search indexes) must await *all* of
  them before responding. An async reset produces the worst failure mode a
  bench can have: an intermittently green isolation canary.
- **Total.** Reset destroys all bench-visible state for all sessions.
  There is **no scope parameter** — the earlier illustrative skeleton's
  `{"scope": "all"}` is dead; scopes are configurability sneaking back in,
  and partial resets are how contamination survives. Wipe derived state
  too: the canonical incident is a search index answering scenario N+1's
  question from scenario N's transcript after the transcript itself was
  deleted.
- **Idempotent.** Reset on a fresh endpoint is a cheap 200, matching the
  SPI contract for `AgentHandle.reset_state()` (called before *every*
  run).

A failed reset (non-200) must be fatal to the bench run: a bench that
continues past a failed wipe is scoring contamination.

## Layering: the wire is not the SPI

The contract's knowledge boundary is deliberate: **an endpoint implementer
never needs to know what an OpenAI-shaped message looks like.** The inject
wire carries `{name, arguments-as-object}`; the driver
(`HttpInjectRuntime`) converts to whatever the SPI `Response` requires —
message-role conventions, OpenAI-style argument stringification, tool-call
id synthesis — on its own side of the wire. Normalization code belongs in
the driver, always. If an endpoint author finds themselves reading the
OpenAI wire format to implement Contract C, the contract has failed at its
one job.

The generic `HttpRuntime` skeleton in [writing a
runtime](../writing-a-runtime.md) remains the right pattern for agent
platforms with their own run/poll APIs. Contract C is the paved path for
everyone else: add two routes, get benched.

## The built-in runtime

Version 1 ships with `http_inject`, a built-in runtime speaking exactly
this contract — no payload configuration surface, base URL as its only
required setting (default `http://127.0.0.1:8647`, overridden with
`WT_INJECT_URL`). The request deadline defaults to 120 seconds, can be
overridden with `WT_INJECT_TIMEOUT_S`, and the driver adds the specified
five-second transport grace. It is implemented on the standard library
(`urllib.request`), keeping Wind Tunnel's runtime dependency count where it
is. Selection follows the existing resolution order in `wt run --runtime`
(built-ins → entry points → dotted path).

## The reset canary

The scariest silent failure in any driver is an incomplete reset: state
leaking between scenarios makes every score a lie. The conformance suite
therefore includes an isolation canary:

1. Open a session; inject a turn embedding a **random nonce** (a UUID —
   never a memorable phrase).
2. Call `/wt/reset`.
3. Open a *new* session and probe for the nonce ("what did I just tell
   you?", plus a direct recall ask).
4. If any post-reset reply contains the nonce, the endpoint **fails** —
   leakage is proven.

The claim is deliberately asymmetric, and the canary's promise is worded
to match: **it converts one class of leak into a red X.** A recalled nonce
proves contamination; a pass does *not* prove isolation — state may have
leaked somewhere the model didn't happen to query (the search-index
incident class). For stateful backends, pair the canary with a
`StateProbe` assertion that inspects the stores directly.

## Conformance and ratification

An implementation **conforms** when:

- every inject response validates against § Contract C (version echo,
  required keys, `tool_calls` present-even-empty, object arguments);
- error paths produce the in-contract shapes (§ Errors and timeouts);
- reset is synchronous, total, and idempotent, and the reset canary
  passes;
- the [runtime checklist](../writing-a-runtime.md) items hold (a
  `must_call` scenario passes; two back-to-back stateful scenarios stay
  isolated).

**Ratification bar:** this contract does not freeze at version 1 until
**two independent driver-side implementations** conform *and* pass the
canary. Real-world conformance before freeze beats any amount of spec
review.

## Non-goals for version 1

- **Configurable payload mapping** — rejected outright (§ Why a fixed
  contract).
- **Reset scopes** — rejected (§ Reset).
- **Streaming responses** — the bench scores completed turns; a streaming
  variant would be additive in a later version.
- **Tool results on the wire** — `tool_calls` carries names and arguments
  as transcript-side trajectory evidence; results are witnessed
  server-side by the MCP layer (Contract B universes, `LoggingFastMCP`),
  which remains the preferred evidence source.
- **Concurrent injects per session** — the driver serializes (§ The
  session model).
- **Authentication** — trusted network assumed; transport-layer concern.

## Appendix (non-normative): notes for endpoint authors

- With identity derivation on the endpoint side (§ The session model),
  *which* surface the bench drives — channel, workspace, tenant — becomes
  **endpoint configuration** (typically an environment variable), not
  driver knowledge. The driver knows only `session_id`. This is the
  intended division of labor: it deletes identity-derivation code from
  the driver entirely.
- Endpoints that key conversation state on a composite identity (e.g.
  platform + channel + user) should derive a distinct per-session
  identity from `session_id`, so that two bench sessions driving one
  configured surface stay isolated from each other. Session isolation is
  exactly what the reset canary probes; deriving identity per session is
  how a multiplexed endpoint passes it.
- The driver-side transport grace (§ Errors and timeouts) is a *maximum*
  courtesy, not a budget: a conforming endpoint should treat `timeout_s`
  itself as the deadline and convert its own expiry into an agent-level
  error envelope well inside the grace window.
