# Provider setup and offline verification

Open `/auth` and choose **LLM Providers**, **Tools**, **Decisions**, or **Voice**.
Search by provider name, provider ID, or credential variable. Select a row to
add or replace its key. Keys remain in Nova's existing OS credential store.
Opening this screen does not call a model or validate account entitlement.

After adding an LLM key, open `/model` to select the provider and model. Custom
model IDs remain supported. A saved selection survives restart and session
restoration; adding a key does not automatically select a new gateway or region.
Regional and subscription variants have separate credential slots. The usual
provider environment variable names remain accepted as aliases.

The reviewed catalog is bundled with Nova, so the new provider list works
offline. It includes a small selection of current, tool-capable text models
per provider and documented model-specific API routes. Its source and digest
are recorded in `novacode_cli/config/provider_catalog.json`. Updating that
snapshot is a reviewable code change, not a background provider request.

## Providers with additional setup

`/auth` reports missing endpoint/project settings as **setup required**.
These settings belong to the current process environment; keys can be stored
through `/auth`. Provider endpoint overrides use the catalog's `base_url_env`.

| Provider | Additional settings |
| --- | --- |
| Cloudflare Workers AI | `CLOUDFLARE_ACCOUNT_ID` |
| Cloudflare AI Gateway | `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_GATEWAY_ID` |
| Databricks | `DATABRICKS_HOST` (hostname or HTTPS workspace URL) |
| Snowflake Cortex | `SNOWFLAKE_ACCOUNT` |
| Infomaniak | `INFOMANIAK_PRODUCT_ID` |
| Neon | `NEON_AI_GATEWAY_BASE_URL` |
| Vertex | `GOOGLE_VERTEX_PROJECT`, optional `GOOGLE_VERTEX_LOCATION` (default `global`), and Google Cloud Application Default Credentials |
| watsonx.ai | `WATSONX_URL`, `WATSONX_AI_PROJECT_ID`; the API key is exchanged for an IBM IAM token lazily |
| SAP AI Core | Store the service-key JSON as `AICORE_SERVICE_KEY`; set `SAP_AI_DEPLOYMENT_ID` and optionally `AICORE_RESOURCE_GROUP` |
| GitLab Duo | A token with Duo access; optional `GITLAB_INSTANCE_URL` for a different GitLab instance |
| QVAC | `QVAC_BASE_URL` for an already-running local server; an API key is optional |
| NHR@FAU | `NHR_FAU_BASE_URL` and a current model ID from your administrator |

Vertex currently offers Gemini models. SAP uses an existing OpenAI-compatible
GPT deployment. GitLab uses Duo chat routes, excluding the separate server-side
workflow API. None of these integrations creates cloud resources.

Perplexity Sonar does not support function tools and cannot run Nova's coding
agent. `/auth` explains this and points to **Perplexity Agent**. Lilac and
Standard Compute have no current tool-capable default in the reviewed catalog;
provide a current model ID rather than relying on a retired model.

## Tool services

Exa, Firecrawl, Parallel, Tavily, and TinyFish appear under **Tools**. Saving a
key does not install or enable a tool. Use the matching preset in `/mcp`.
Exa preserves anonymous access and uses its stored key when present. Parallel
and TinyFish read current keys from the credential store through secret
references; MCP configuration files contain no copied header values. These
references are pinned to each service's official endpoint and header, so a
configuration file cannot redirect the stored key to a different server.

## What offline checks establish

The tests account for all 231 supplied names and use real SDKs with intercepted
HTTP transports to check provider registration, credential routing, endpoints,
tool schemas, response parsing, async/streaming protocols, managed credential
refresh, and rejection of credential forwarding to another origin. Additional
tests cover session/config restoration, subprocess secret filtering, MCP key
rotation, and the auth sections on short terminals.

These checks do not establish live service availability, API-key validity,
account entitlement, model quality, quotas, or every vendor's server-side schema
restrictions. No billable provider requests are made by the tests. The existing
theme-modal test fixture also fails on committed main because its host lacks
`_set_diff_preview_style`; it is unrelated to provider or auth changes.

No runtime dependency or version change is required.
