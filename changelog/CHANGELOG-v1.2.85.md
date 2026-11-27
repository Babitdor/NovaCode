# NovaCode 1.2.85

## Home-screen animations: the signal rain and scenes are ~2.8x cheaper

- The signal-style code rain's per-step repaint (`paint_chains`) is now a
  pure, module-level function: it builds each frame row once instead of
  clearing shared buffers and re-assigning every cell. Measured on a 194x30
  grid over 900 frames: 2.32s -> 0.85s of animation compute.
- The home scenes (terminal/studio/blueprint) are driven by a pure
  `scene_frame_layout` that returns plain text plus `(start, end, style)`
  spans; the Rich wrapper applies them. Behavior is unchanged, including the
  base art's own spans riding along.

## Headless/pipe (parallel session)

- Steering over the pipe protocol: `steer` frames are accepted mid-turn,
  validated and routed to the running session, with the capability advertised
  in `ready` and coverage in `test_pipe_mode`.
