# NovaCode v1.0.87

## Features

- Keep image attachments available to multimodal chat models, and add an on-demand
  `VISION` side-call for text-only models. The independent vision model can be
  configured in `/model` → **Vision** without changing the conversation model.
- Bound VISION calls to 30 seconds and show initialization, timeout, and provider
  failures in the tool result so the conversation can continue with a clear reason.

## Verification

- Cover deferred image inspection, current-turn image selection, timeout reporting,
  and independent vision-model selection and persistence.
