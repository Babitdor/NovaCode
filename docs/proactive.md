# Proactive agents

Open `/proactive` and choose **Create agent**. Give the agent a name, standing
instructions, and a workspace. Save it before running it.

Each agent can have these read permissions:

- **Workspace** reads committed text files at the root of a local Git
  repository. Enable it only for a repo whose committed files can be shared
  with your selected model.
- **GitHub** reads recent Actions runs and job outcomes for one `owner/repo`.
  This uses the signed-in `gh` CLI and its existing repo access.
- **PyPI** reads publication status for one configured package.

**Run now** starts the saved agent outside your current chat. Use **Run context**
to attach the run to a particular release, commit, or other piece of work. Results
appear in **Run history**, and NovaCode shows a local notification when a run
finishes. Agents use the profile's configured model.

Set a five-field cron schedule in local time to run the agent periodically.
Enable GitHub tag watching to run it when the configured repository gains tags.
The first successful tag check records the baseline and subsequent polls check
the 30 tags GitHub returns about once a minute. New tags found in one poll go
into a single run.

**Enable triggers** activates the selected triggers after you save. Clear it
and save to pause them. Manual runs remain available. Closing NovaCode stops the
runner. Agent definitions and reports persist and triggers resume the next time
NovaCode opens. Missed cron ticks are coalesced into at most one run.

For example, create **Release Steward**, configure the repo and PyPI package,
and use standing instructions such as “Check CI for the release in the run
context. Report failing jobs or steps and confirm the version in PyPI when
available.” You can run it with a version in **Run context**, enable tag
watching, or schedule periodic checks.

Each agent can have one queued or active run. Runs time out after three minutes,
and the panel retains the most recent 30 reports per agent.
