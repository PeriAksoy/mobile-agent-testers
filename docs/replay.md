# Replay the path of a tester (experimental)

Two runs never take the same path. So when a tester finds a bug, the path that led to it is worth keeping.

Each Android tester now leaves two more files in its results, next to `report.md`:

- `trace.json`: what the agent did, step by step. A tap is stored with the name of the element that was under the finger, not with its coordinates, so the path still replays when the layout moves. It also names the model that explored, when the run says it.
- `trace.params.json`: every text the agent typed (an e-mail, a password). The trace only holds their names, `TEXT_1`, `TEXT_2`. **Do not commit this file.**

Nothing changes for the agent: same tools, same brief. The taps are noted by a pass-through in front of `adb` ([scripts/android/shim/adb](../scripts/android/shim/adb)), and [scripts/ecr_trace.py](../scripts/ecr_trace.py) writes the trace when the agent is done.

To watch, comment and edit these paths before compiling them: [docs/dashboard.md](dashboard.md).

## From a trace to a test that reruns with no model

The trace is in the format of [Explore-Compile-Replay](https://github.com/pierridotite/explore-compile-replay), which compiles it to a [Maestro](https://maestro.dev) flow:

```bash
bash scripts/collect.sh <run id>                       # decrypts the results into runs/<run id>/
git clone https://github.com/pierridotite/explore-compile-replay && cd explore-compile-replay && pnpm install
pnpm ecr compile ../runs/<run id>/anna/trace.json --adapter packages/adapter-maestro/src/index.ts
maestro test -e TEXT_1="..." ../runs/<run id>/anna/anna-<date>.flow.yaml
```

## Replay it right after the run

With `-f replay_path=true`, each Android tester's path is replayed on the same phone as soon as the agent is done, with no model:

```bash
gh workflow run agent-testers.yml -f android_personas=anna -f ios_personas=none -f replay_path=true
```

The run page says how it went in one line, for example `replay: pass, 22 steps with no model in 41 s` or `replay: stopped at step 7 of 22 (target_not_found), after 38 s`. It never prints a text of your app. Three more files join the encrypted results: the compiled flow (`anna-<date>.flow.yaml`), `replay.json` (the status, the step where it stopped and why) and `replay.txt` (everything the tools printed), plus what Maestro kept in `replay-debug/`.

The replay runs with the model keys taken out of its environment ([scripts/ecr_replay.py](../scripts/ecr_replay.py)), and a replay that stops never fails the run. It adds a few minutes to each tester: installing Maestro, then the replay itself.

When a replay stops, the app may have shown something else the second time (an account that now exists, a list that is no longer empty), or the trace missed something the agent did. Tell us which line you got in an issue: it shows what the trace still misses.

## What it is today

- **Experimental.** The trace is written and compiles to a flow that Maestro accepts. Flows compiled from traces written by hand replay on an Android 11 emulator, on Tiny Todo, the demo app ([the device run of Explore-Compile-Replay](https://github.com/pierridotite/explore-compile-replay/actions/workflows/device.yml)). The path of a real agent has not been replayed yet: `replay_path` is there to find out.
- The agent's waits are not in the trace. Maestro waits for each element by itself, which may not be enough on a slow screen.
- **A path, not yet a test of the bug.** The flow has no checks: it fails when a step no longer finds its target. Turning a finding of the report into a check is the next step.
- **Android only.** iOS comes next.
- An element with no name and no id is stored as a point on the screen, in percent. It breaks on another screen size.
- When several elements share a name, the trace keeps the position of the one that was tapped among them.
- A tap on an element that shows a text the agent typed (a task it just added) is stored by its id, or as a point: typed texts stay out of the trace.
