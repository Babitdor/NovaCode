# Council settings

Open `/council settings` in the Nova terminal. Select a member on the left,
then edit their display name, system prompt and model. Scroll the editor to
reach the full prompt. **Save** applies your changes to future council runs;
**Cancel** or Escape discards the draft. **Reset all** restores defaults in the
draft and still requires Save.

Select **Session model (inherit)** to use the active model. To give a member
their own model, choose a provider and enter its exact model ID. Configure that
provider's credentials through `/auth` first. Editing and saving settings makes
no model calls and does not change your main agent's model. An unavailable
override produces an error instead of silently substituting another model.

Personality templates include Architect, Pragmatist, Skeptic, Innovator,
Minimalist, Craftsperson, Security Reviewer, Test Engineer, Performance Analyst,
Product Advocate and Judge. Applying a template replaces the selected name and
prompt while keeping its model. Give every seat a distinct name before saving.

The web debate uses the first five members. Planning councils also include the
Craftsperson seat and a judge. The judge's editable prompt adds a perspective to
the required scoring rubric and response format. Planning stages and explicit
plan approval remain mandatory.

Settings are stored under `council` in `~/.nova/Nova.config.json`. Each run takes
a snapshot when it starts, so saving changes does not alter an active round.
