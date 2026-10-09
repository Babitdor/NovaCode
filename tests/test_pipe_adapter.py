"""Run the example Node adapter against a fake pipe process, without providers."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def test_node_adapter_ready_prompt_stop_restart_and_crash(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to run the Remote App adapter example")
    adapter = (Path(__file__).parents[1] / "examples/remote-app/nova-pipe.mjs").as_uri()
    project = tmp_path / "项目 with spaces"
    project.mkdir()
    fake = """
import json, sys, os
print(json.dumps({"type":"ready","protocol":"nova.pipe","protocol_version":1,"session_id":"saved-session"}), flush=True)
for line in sys.stdin:
    frame = json.loads(line)
    if frame["type"] == "shutdown":
        print('{"type":"stopped","exit_code":0}', flush=True)
        break
    if frame["type"] == "prompt":
        if frame["content"] == "crash":
            os._exit(7)
        print(json.dumps({"type":"done","request_id":frame["id"],"ok":True}), flush=True)
"""
    script = f"""
import assert from 'node:assert/strict';
import {{ once }} from 'node:events';
import {{ NovaPipe }} from {json.dumps(adapter)};
const pipe = new NovaPipe({{binary:process.argv[1], args:['-u','-c',process.argv[3]], cwd:process.argv[2]}});
assert.throws(() => pipe.prompt('early', 'hi'), /ready/);
await pipe.start();
assert.equal(pipe.ready, true);
assert.equal(pipe.config.resume, 'saved-session');
const done = new Promise(resolve => pipe.on('event', event => {{ if(event.type === 'done') resolve(event); }}));
pipe.prompt('p1', 'hi');
assert.equal((await done).request_id, 'p1');
assert.equal(pipe.pending.size, 0);
await pipe.stop();
assert.equal(pipe.ready, false);
await pipe.start();
const exited = once(pipe, 'exit');
pipe.prompt('p2', 'crash');
const [exit] = await exited;
assert.equal(exit.code, 7);
assert.deepEqual(exit.interrupted, ['p2']);
assert.equal(pipe.ready, false);
await pipe.start();
assert.equal(pipe.pending.size, 0);
await pipe.stop();
"""
    run = subprocess.run(
        [node, "--input-type=module", "-e", script, sys.executable, str(project), fake],
        capture_output=True,
        timeout=20,
    )
    assert run.returncode == 0, run.stderr.decode("utf-8", errors="replace")


def test_node_adapter_rejects_missing_binary_and_directory(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to run the Remote App adapter example")
    adapter = (Path(__file__).parents[1] / "examples/remote-app/nova-pipe.mjs").as_uri()
    script = f"""
import assert from 'node:assert/strict';
import {{ NovaPipe }} from {json.dumps(adapter)};
assert.throws(() => new NovaPipe({{binary:'nova',cwd:process.argv[1]}}), /absolute/);
const pipe = new NovaPipe({{binary:process.argv[2],cwd:process.argv[1],startupTimeout:1000}});
await assert.rejects(pipe.start());
await pipe.stop();
"""
    run = subprocess.run(
        [node, "--input-type=module", "-e", script, str(tmp_path), str(tmp_path / "missing.exe")],
        capture_output=True,
        timeout=10,
    )
    assert run.returncode == 0, run.stderr.decode("utf-8", errors="replace")
