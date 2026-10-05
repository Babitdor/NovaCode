You are working on the NovaCode website/landing page.

Your job is to improve the landing page substantially, with the main focus on **content, messaging, information hierarchy, and conversion**, while preserving the strong existing visual/terminal aesthetic.

Do NOT blindly redesign the entire site. First inspect the existing codebase, understand the current page structure, components, styling, animations, responsive behavior, and content. Then make thoughtful changes.

## Product positioning

NovaCode is not just another AI coding assistant.

The core positioning should be:

**“A coding agent that verifies its own work.”**

Nova can understand a codebase, plan work, edit files, run tools/commands/tests, evaluate whether the requested task was actually completed, and retry when verification fails.

The landing page should make this distinction obvious within the first few seconds.

The visitor should understand this mental model:

**Give Nova a task → Nova understands → plans → builds → tests → verifies → fixes failures → returns verified work.**

Verification should be one of the strongest themes throughout the page.

---

## Primary objective

Rewrite and restructure the landing page so that it sells **outcomes and benefits first**, and uses technical capabilities as evidence.

The current site contains many impressive technical details, but it can feel like documentation or a feature inventory.

Avoid leading with numbers such as:

- 50+ tools
- 4 + 9 agents
- 50+ skills
- 12 MCP presets
- 7 providers

These can remain as supporting proof, but they should not be the main value proposition.

Follow this hierarchy whenever possible:

**Problem → Benefit → Capability → Evidence → Technical detail**

Not:

**Technical detail → technical detail → technical detail → benefit**

---

# 1. Improve the hero

The hero should immediately communicate what Nova does and why it is different.

Use messaging close to:

### A coding agent that verifies its own work.

Give Nova a task. It explores your codebase, makes the changes, runs the tools and tests, checks whether the result actually works, and retries when it doesn't.

**Open source. Runs locally. Bring your own model.**

Primary CTA:
**Install Nova**

Secondary CTA:
**See it work**

Preserve the existing NovaCode personality and terminal/developer aesthetic.

Avoid generic AI marketing phrases such as:

- “Revolutionize your development”
- “Supercharge your workflow”
- “The future of coding”
- “10x your productivity”
- “AI-powered development”

The copy should sound technical, confident, concise, and credible.

---

# 2. Add a concrete task demonstration near the top

Before explaining Nova's architecture, show what using it actually feels like.

Create a visually compelling terminal/agent demonstration.

Example task:

> Add rate limiting to the API. Use the existing Redis client, cover the public endpoints, and add tests.

Then visually show Nova progressing:

✓ Exploring repository  
✓ Found existing Redis integration  
✓ Planning implementation  
✓ Implemented rate limiting  
✓ Added tests  
✓ Running test suite  
✗ 2 tests failed  
✓ Diagnosed middleware ordering  
✓ Fixed implementation  
✓ Running tests again  
✓ 148 tests passing  
✓ Task verified

End with something like:

**Nova doesn't stop when code has been written. It checks the result and keeps working when verification fails.**

Make this section visually communicate the product rather than requiring a large paragraph.

If appropriate, animate the sequence while respecting reduced-motion preferences.

---

# 3. Make verification a centerpiece

Add or significantly improve a section around:

### Code written isn't the same as a task finished.

Explain that generating plausible code is only part of the job.

Nova evaluates the result against the requested task. When verification fails, the failure becomes feedback for another attempt.

Visually communicate:

TASK  
↓  
UNDERSTAND  
↓  
PLAN  
↓  
EDIT  
↓  
TEST  
↓  
VERIFY

If verification fails:

VERIFY ✗ → FEEDBACK → RETRY

If verification passes:

VERIFY ✓ → DONE

Use the actual behavior of NovaCode as the source of truth. Do not invent capabilities that aren't implemented.

This should be one of the most memorable sections of the landing page.

---

# 4. Reframe codebase intelligence around the benefit

Instead of leading with implementation terminology, lead with:

### Nova finds the code that matters.

Explain that developers can describe behavior rather than manually identifying every relevant file.

Nova can use capabilities such as semantic search, project understanding/graph information, LSP, and dependency/blast-radius information where actually supported.

Technical concepts should appear as supporting evidence:

`Semantic search · Project graph · LSP · Blast-radius analysis`

Keep the explanation concise.

---

# 5. Improve the multi-agent/delegation section

Do not make “4 agents”, “9 agents”, etc. the primary message.

Lead with the user outcome:

### Give Nova the jobs you'd normally have to break apart yourself.

Explain that Nova can handle multi-step tasks and delegate specialized work when appropriate.

Then explain `/council` if it is currently supported.

A possible framing:

**For difficult decisions, Nova can generate competing approaches, critique them, and let you approve the strongest plan before implementation begins.**

The architecture and exact number of agents can remain visible as secondary technical details.

---

# 6. Strengthen the safety section

Keep the spirit of the existing “Real edits, with the brakes on” messaging.

Consider framing it as:

### Autonomous doesn't mean uncontrolled.

Explain clearly that Nova may edit files and execute commands, but risky operations remain controlled.

Surface actual protections that exist in the project, such as:

- approval gates
- gitignore enforcement
- command-injection detection
- snapshots
- restore functionality
- sandboxing

Only mention capabilities verified in the codebase.

Make the user feel that Nova can act autonomously without being given unlimited uncontrolled access.

---

# 7. Improve the self-improvement section

Use a strong heading such as:

### Nova learns from the work it gets wrong.

Explain this concretely rather than with vague “self-learning AI” language.

If supported by the implementation, explain that completed runs and verification results can be analyzed so repeated failures become useful feedback, skills, or improvements.

Use the mental model:

**Trace → Grade → Retry → Learn**

This can be a visual process.

Avoid unsupported claims suggesting Nova permanently trains or modifies the underlying LLM.

Be precise about what actually improves.

---

# 8. Simplify context-management content

The existing context-management implementation is technically interesting, but the landing page should not read like internal documentation.

Use an outcome-oriented heading such as:

### Long sessions don't have to lose the plot.

Explain briefly that Nova actively manages context around the model's available context window, preserving useful information while making room for ongoing work.

Supporting concepts can include, if accurate:

`Adaptive compaction · Recoverable history · Context reserve`

Move low-level implementation details out of the primary narrative or make them expandable/secondary.

---

# 9. Present integrations as workflow flexibility

Create a concise section showing where Nova can operate.

For example:

### Use Nova where you already work.

Then surface only integrations/modes that genuinely exist:

- Terminal
- CI/headless usage
- Discord
- Telegram
- MCP
- supported model providers

Do not overwhelm the visitor with configuration details.

Link technical details to documentation where appropriate.

---

# 10. Explain model choice clearly

If accurate, highlight:

### Bring your own model.

Explain that Nova is the agent/runtime layer rather than being tied to one model provider.

Show supported providers/models based strictly on the repository's actual implementation.

This is a meaningful differentiator and should be easy to discover.

---

# 11. Preserve technical depth without overwhelming the homepage

The audience is developers, so do NOT dumb the product down.

Instead, use progressive disclosure.

Primary landing-page copy:
**What does this do for me?**

Secondary copy:
**How does Nova accomplish it?**

Documentation:
**Exactly how is it implemented/configured?**

Keep technical credibility, but don't require someone to understand Nova's internal architecture before understanding its value.

---

# 12. Improve the overall information architecture

Aim for approximately this narrative:

1. Navbar
2. Hero
3. Concrete Nova task/demo
4. Verification differentiator
5. Codebase understanding
6. Complex/multi-step work and delegation
7. Safe autonomy
8. Learning from failed work
9. Context management
10. Integrations/workflows
11. Bring your own model
12. Open-source/technical credibility
13. Installation / quick start
14. FAQ if appropriate
15. Final CTA
16. Footer

Do not mechanically create 16 huge sections.

Combine related ideas where doing so produces a cleaner page.

The page should feel progressively deeper as the visitor scrolls.

---

# 13. Reduce repetition

Audit ALL existing copy.

Remove sections or sentences that repeat the same concepts.

Every major section should answer a different visitor question:

**What is Nova?**

**Why is it different?**

**Can it actually handle real work?**

**How does it know whether the work is correct?**

**Can it understand a large codebase?**

**Can I trust it to modify my project?**

**Does it fit my workflow?**

**Which models can I use?**

**How do I install it?**

If two sections answer essentially the same question, combine or rewrite them.

---

# 14. Writing style

Use concise developer-oriented language.

Prefer:

> Nova runs the tests and checks the result.

over:

> Harness the power of advanced AI-driven verification technology to ensure unparalleled code quality.

Avoid hype.

Avoid excessive adjectives.

Avoid vague claims.

Avoid unnecessary buzzwords.

Avoid pretending Nova can do something unless the repository demonstrates that capability.

Use short paragraphs, strong headings, terminal output, diagrams, code snippets, and actual examples to communicate technical ideas.

---

# 15. Improve CTA strategy

There should be one obvious primary action throughout the site:

**Install Nova**

Possible secondary actions:

**See it work**
**View on GitHub**
**Read the docs**

Don't create many competing CTAs.

Near the bottom, create a strong final conversion section.

Possible direction:

### Give Nova a real task.

Install Nova, point it at a project, and let the verification loop do the rest.

[Install Nova] [View on GitHub]

Adapt this based on the actual installation process.

---

# 16. Preserve what already works

Do NOT turn this into a generic SaaS landing page.

Keep the distinctive:

- developer-first identity
- terminal aesthetic
- `.book` / technical visual language where it still works
- monospace/code elements
- animations/interactions that add meaning
- existing brand identity
- dark/light behavior if present
- responsive behavior

Improve rather than replace the site's personality.

The finished site should still unmistakably feel like NovaCode.

---

# 17. Quality requirements

Before finishing:

- Check desktop and mobile layouts.
- Check tablet widths.
- Ensure no text overflows.
- Check navigation anchors.
- Check CTA links.
- Check installation commands.
- Check GitHub/documentation links.
- Check accessibility.
- Maintain semantic HTML.
- Maintain keyboard navigation.
- Respect `prefers-reduced-motion`.
- Check contrast.
- Avoid unnecessary JavaScript.
- Avoid hurting page performance.
- Remove dead/unused components created by the refactor.
- Run the project's existing lint/build/test commands.
- Fix issues introduced by your changes.

---

# 18. Accuracy requirement

This is critical:

**Inspect the NovaCode repository before making product claims.**

Do not invent statistics, benchmark results, testimonials, customer numbers, security certifications, performance improvements, supported integrations, model providers, or capabilities.

If existing landing-page copy makes a claim that cannot be substantiated by the current project, rewrite or remove it.

Technical credibility is more important than aggressive marketing.

---

## Final goal

When a developer visits the finished landing page, within roughly 10 seconds they should understand:

**Nova is an open-source coding agent that can work on my codebase and verify whether its own work actually succeeded.**

After scrolling further, they should understand:

**how it works, why verification matters, how Nova handles complex work, what control I retain, how it fits my stack, and how to install it.**

The website should make me want to give Nova a real coding task—not merely read about its architecture.

Start by auditing the existing implementation and content. Then implement the improvements directly in the codebase. Preserve good existing work rather than rebuilding unnecessarily.

When finished, run the appropriate validation/build commands and give me a concise summary of:
1. What you changed
2. Why you changed it
3. Which existing capabilities you verified before making claims
4. Anything you deliberately left unchanged
5. Build/test/lint results