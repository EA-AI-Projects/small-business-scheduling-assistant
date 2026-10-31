# Agent framework research for the SMS scheduling assistant

**Status: Research / proposed design options only.** Reviewed 2026-10-31. No architecture decision, migration commitment, dependency installation, or application implementation accompanies this note. Framework capabilities below come from linked first-party documentation; suitability judgments are our assessment. Documentation is a moving target: verify the selected package/version before any future experiment. This is a representative shortlist, not a popularity ranking.

## What we already have

The application already implements a small domain-specific agent harness: verified SMS input, bounded model/tool loops, separate client/owner tools, validated final text, deterministic fallbacks, and durable action results. The model selects actions; backend services authorize and commit them. Client loops allow five model calls, three tool calls and 30 seconds; owner loops use the same counts and 36 seconds. Identity comes from verified receipts, not tool arguments. Only one action takes effect per message. [Current conversation contract](CONVERSATION.md)

An agent framework could replace orchestration plumbing around those tools. It would not remove the difficult scheduling contracts: stale/version checks, a second message to accept an offer, pending owner approval, consent/STOP gates, atomic holds, receipt deduplication, or the transactional notification outbox. Sent-aware conversation history also has deliberate privacy and delivery semantics: at most 24 messages/12,000 characters from the last 24 hours, excluding failed sends and drafts. Framework memory is not automatically equivalent. [Conversation and delivery boundaries](CONVERSATION.md)

The existing `OwnerToolLoop` and `ClientToolLoop` protocols in [conversation.py](../backend/scheduling/domain/conversation.py) already provide a possible substitution seam: both receive a trusted tool dispatcher, and the client loop additionally receives a final-text validator. The current [OpenAI interpreter](../backend/scheduling/adapters/openai_messages.py) makes raw Responses API requests rather than using the OpenAI Python SDK. A future comparison could target `run_owner_loop`/`run_client_loop` without redesigning scheduling services; this is a proposed seam, not an implementation plan.

## Recommendation

Evaluate **OpenAI Agents SDK and Pydantic AI first** if the objective becomes reducing custom loop code while keeping the current architecture. Consider **Strands** alongside them if AWS/Bedrock portability becomes a priority. Choose **LangGraph** when explicit branching, durable resumable workflows, or operational inspection of a graph becomes a demonstrated need. LangChain's simpler `create_agent` also deserves consideration independently of adopting a custom graph.

Keep the current harness as the baseline. A framework adds value only if it improves maintenance, testing, diagnostics, or provider choice enough to justify integration and dependency costs. More autonomous agents are not intrinsically better for short transactional SMS turns.

| Candidate | Most relevant value | Main cost or limitation here | Assessment |
| --- | --- | --- | --- |
| OpenAI Agents SDK | Standard loop, tools, guardrails, traces | Preserve custom domain and message semantics | First candidate with current OpenAI use |
| Pydantic AI | Typed Python tools/output, dependencies, model portability | Validation/retry behavior must match existing limits | Strong alternative |
| LangChain `create_agent` | Configurable harness, middleware, model ecosystem | Another abstraction over current narrow loop | Worth comparing with the first two |
| LangGraph | Explicit stateful orchestration, checkpoints, interrupts | Persistence/runtime integration and duplicated state risk | Best if workflow complexity grows |
| Strands Agents SDK | In-process agent loop, AWS deployment guidance, model flexibility | Session semantics/version changes need review | Strong AWS-oriented option |
| Google ADK | Tools, workflows, evaluation and Google integrations | Less natural deployment/ecosystem fit; 2.0 changes | Secondary option |
| Microsoft Agent Framework | Agents, graph workflows, middleware, provider ecosystem | More value with Microsoft ecosystem/complex orchestration | Secondary option; prefer over new AutoGen work |
| CrewAI | Crews and event-driven Flows | Team/role abstractions add little to one SMS assistant | Lower priority |
| Claude Agent SDK | Full Claude Code harness, custom tools, hooks | Binary/workspace-oriented runtime exceeds present needs | Lower priority for this application |

The table's ordering is a project-fit judgment. Sources for individual capabilities and limitations follow.

## Candidate details

### OpenAI Agents SDK

The SDK runs inside our application and provides the model → tool → model loop. Application code still supplies tools, deployment, state and approval behavior. That makes it a plausible replacement for the current runner without changing Twilio ingress, SQS workers, DynamoDB scheduling services or the sender. [SDK overview](https://developers.openai.com/api/docs/guides/agents/sdk)

It supports application-supplied history, SDK sessions and provider-managed conversation approaches; we should choose one history authority. Existing sent-aware history is the natural starting point. Interruptions and serialized run state support later approvals, but an SDK approval is distinct from our expiring, version-bound scheduling request and verified owner's subsequent message. [Running agents](https://developers.openai.com/api/docs/guides/agents/running-agents), [run results](https://developers.openai.com/api/docs/guides/agents/results)

Guardrails and approval hooks are useful orchestration features. Built-in tracing is enabled by default and can record model/tool inputs and outputs, so configuration must preserve current privacy and retention boundaries. Keep exact commands, serial tool execution, one effective action per receipt, existing budgets and no write replay after failed final wording. [Guardrails](https://developers.openai.com/api/docs/guides/agents/guardrails-approvals), [observability](https://developers.openai.com/api/docs/guides/agents/integrations-observability)

### Pydantic AI

Pydantic AI offers a typed Python agent loop with model providers including OpenAI, Anthropic, Google and Bedrock. Its dependency/tool/output approach is a natural fit for supplying a verified actor context and exposing existing domain functions. Provider portability reduces orchestration coupling, although model behavior still needs separate evaluation. [Overview](https://pydantic.dev/docs/ai/overview/)

`UsageLimits` caps requests and successful tool invocations; tool-call limits are checked before execution. This is relevant to our existing bounds, but not necessarily identical to counting malformed attempts, elapsed time or effective mutations. Those rules still need application enforcement. [Agent usage limits](https://pydantic.dev/docs/ai/core-concepts/agent/#usage-limits)

Durable execution is optional and distinct from storing history or snapshots. Current integrations include Temporal, DBOS, Prefect, Restate and an AWS Lambda durability integration; capabilities vary by engine. Ordinary Lambda hosting does not make an agent run durable. We could use the basic agent without introducing another workflow runtime. [Durability overview](https://pydantic.dev/docs/ai/capabilities/durable_execution/overview/), [AWS Lambda durability](https://pydantic.dev/docs/ai/harness/aws-lambda/)

### LangChain and LangGraph

These are different adoption choices. LangChain's `create_agent` is a configurable harness built from model, tools, prompt and middleware. LangGraph supplies lower-level stateful orchestration; using it does not require adopting every LangChain abstraction. A straightforward agent can start at the higher layer rather than hand-authoring a graph. [LangChain overview](https://docs.langchain.com/oss/python/langchain/overview), [LangGraph overview](https://docs.langchain.com/oss/python/langgraph/overview)

LangGraph checkpointers persist thread state; stores hold cross-thread data. Interrupts can pause for external input and later resume. This becomes useful for long multi-stage work, but our pending requests already survive SMS turns as business records. A second graph-level approval state can create synchronization work. An in-memory saver is not restart-durable; production persistence needs an appropriate backend or managed server. [Persistence](https://docs.langchain.com/oss/python/langgraph/persistence), [interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)

Resuming an interrupted node reruns it. The official documentation explicitly requires idempotent effects and warns that even post-approval effects can repeat after a failure. Graph checkpointing must therefore preserve our receipt-keyed transactions and outbox rather than replace them. [Interrupt replay and effects](https://docs.langchain.com/oss/python/langgraph/interrupts#side-effects-called-before-interrupt-must-be-idempotent)

Deep Agents adds planning, subagents and filesystem capabilities. Those are useful for broader task execution but offer little obvious value for our deliberately narrow scheduling tools. [LangGraph repository guidance](https://github.com/langchain-ai/langgraph)

### Strands Agents

Strands supplies an in-process Python/TypeScript loop with tools, structured output, lifecycle controls, provider portability and tracing. Its SDK does not require a hosted control plane. The current repository distinguishes the SDK from its more assembled harness and has moved the old `sdk-python` location into a broader `harness-sdk` repository; package/version assumptions should be pinned during evaluation. [Official repository](https://github.com/strands-agents/harness-sdk)

Official AWS Lambda deployment guidance makes it relevant to our worker architecture. Session managers support snapshots through file, S3 and custom storage. Such session snapshots do not establish our business transaction or provider-accepted-message semantics. Keep existing sent-aware history authoritative unless an explicit adapter preserves those semantics. AWS hosting is a fit advantage, not a requirement to move onto Bedrock AgentCore. [Lambda deployment](https://strandsagents.com/docs/user-guide/sdk/deploy/deploy_to_aws_lambda/), [sessions](https://strandsagents.com/docs/user-guide/sdk/agents/session-management/)

### Google ADK

ADK is model- and deployment-agnostic while optimized for Gemini. Its documentation covers tools, evaluation, workflows and non-Gemini providers, including Claude, OpenAI and LiteLLM. This makes it technically viable for Python, although its Google deployment ecosystem gives us less immediate reuse than an AWS-oriented option. [Official repository](https://github.com/google/adk-python), [documentation](https://adk.dev/)

Current documentation describes ADK 2.0's graph Workflow Runtime and explicit incompatibilities from 1.x, including custom event/session storage changes. Do not evaluate it using older tutorials without selecting a release line first. [ADK 2.0 compatibility](https://adk.dev/2.0/)

### Microsoft Agent Framework and AutoGen

Agent Framework supports Python and .NET agents, functional/graph workflows, sessions, context providers, middleware and providers including OpenAI, Anthropic and Microsoft services. This is viable beyond Azure, but its relative advantage here would depend on new integration or workflow requirements. [Overview](https://learn.microsoft.com/en-us/agent-framework/overview/)

AutoGen's official repository says it is in maintenance mode and encourages migration to Agent Framework. Treat AutoGen as relevant historical context rather than a new-adoption recommendation. Do not infer uniform stability across Agent Framework languages or components: its overview explicitly labels Go preview, and release/component status should be checked for the chosen Python APIs. [AutoGen status](https://github.com/microsoft/autogen), [Agent Framework repository](https://github.com/microsoft/agent-framework)

### CrewAI and Claude Agent SDK

CrewAI includes role-based Crews and event-driven Flows; Flows can combine deterministic control with single LLM calls rather than requiring a team of agents. Its default flow persistence uses local SQLite, with a custom persistence interface available. Inference: Lambda would require a deliberate durable storage arrangement; local persisted state alone is insufficient for cross-invocation recovery. Crews are a more natural fit for multi-role research/back-office work than our short scheduling turns. [Repository](https://github.com/crewAIInc/crewAI), [Flows and persistence](https://docs.crewai.com/en/concepts/flows)

Claude Agent SDK embeds the Claude Code binary and its loop, sessions, hooks, permissions and tools in Python/TypeScript. Custom tools are supported, so scheduling is possible. Its workspace and command-execution capabilities add packaging/runtime concerns without an obvious product need here. It is distinct from Anthropic's client SDK and hosted Managed Agents. [SDK overview](https://code.claude.com/docs/en/agent-sdk/overview)

## What would justify a future experiment?

Use a small, offline comparison against the existing harness only if we decide to proceed later. Measure completed scheduling scenarios, model/tool count, latency, final-text rejection rate, fallback rate, debugging effort and orchestration code removed. Include ambiguous requests, stale offers, duplicate receipts, conflicting visits, STOP during processing, timeout after commit, failed final wording and concurrent owner/client messages.

Reject any candidate that cannot preserve verified actor scoping, one action per receipt, stored-offer matching, approval/version rules, serial tools, current budgets and committed-action recovery. Generic tool approval or schema validation does not prove these business properties. A framework does not itself make model interpretations or prose factually correct. These acceptance conditions derive from [the current contract](CONVERSATION.md), not vendor claims.

Framework choice also does not require MCP, multi-agent teams, a persistent autonomous process, unrestricted integrations or replacing Lambda/SQS. Existing Python functions can remain the tool boundary. Durable graph state is valuable only when it owns a clearly defined problem that current receipts, requests and outbox records do not already solve.

## Licensing and evidence limits

Verified repository labels: [Pydantic AI](https://github.com/pydantic/pydantic-ai), [LangGraph](https://github.com/langchain-ai/langgraph), [Microsoft Agent Framework](https://github.com/microsoft/agent-framework) and [CrewAI](https://github.com/crewAIInc/crewAI) use MIT; [Strands](https://github.com/strands-agents/harness-sdk) and [Google ADK](https://github.com/google/adk-python) use Apache-2.0. These licenses do not describe provider billing, hosted service terms or telemetry policies. This note makes no blanket licensing claim for all Claude runtime components.

No performance benchmark, dependency footprint, integration prototype or production-readiness audit was performed. Fit rankings are architectural judgments based on the repository contract and current first-party documentation, not measured superiority or market share.
