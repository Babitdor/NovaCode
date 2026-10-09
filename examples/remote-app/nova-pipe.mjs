/** Local process adapter. Keep relay authentication and pairing in your app. */
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { EventEmitter } from 'node:events';
import { isAbsolute } from 'node:path';
import { stat } from 'node:fs/promises';

export class NovaPipe extends EventEmitter {
  constructor({ binary, args = [], cwd, model, resume, startupTimeout = 130_000 }) {
    super();
    if (!isAbsolute(binary) || !isAbsolute(cwd)) {
      throw new Error('binary and cwd must be absolute paths');
    }
    this.config = { binary, args, cwd, model, resume, startupTimeout };
    this.child = null;
    this.ready = false;
    this.pending = new Map();
    this.starting = false;
  }

  async start() {
    if (this.child || this.starting) throw new Error('Nova is already running or starting');
    const { binary, args, cwd, model, resume, startupTimeout } = this.config;
    this.starting = true;
    try {
      if (!(await stat(cwd)).isDirectory()) throw new Error('cwd must be an existing directory');
    } catch (error) {
      this.starting = false;
      throw error;
    }
    // With Python use an absolute installed interpreter and args ['-I', '-m', 'novacode_cli'].
    // Never use shell:true or pass untrusted chat text as command-line flags.
    const flags = [...args, '--mode', 'pipe'];
    if (model) flags.push('--model', model);
    if (resume) flags.push('--continue', resume);
    const child = spawn(binary, flags, { cwd, shell: false, windowsHide: true,
      stdio: ['pipe', 'pipe', 'pipe'] });
    this.child = child;
    this.starting = false;
    this.ready = false;
    // Drain stderr separately so diagnostics cannot block the process.
    child.stderr.on('data', data => this.emit('diagnostic', data.toString('utf8')));
    child.stdin.on('error', error => this.emit('transport-error', error));
    const lines = createInterface({ input: child.stdout, crlfDelay: Infinity });
    return new Promise((resolve, reject) => {
      let settled = false;
      const fail = error => {
        if (!settled) { settled = true; clearTimeout(timer); reject(error); }
      };
      const timer = setTimeout(() => {
        fail(new Error('Nova readiness deadline exceeded'));
        child.kill();
      }, startupTimeout);
      lines.on('line', line => {
        let event;
        try { event = JSON.parse(line); }
        catch { fail(new Error('Invalid JSON from Nova')); child.kill(); return; }
        if (event.type === 'ready') {
          if (event.protocol !== 'nova.pipe' || event.protocol_version !== 1) {
            fail(new Error('Unsupported Nova pipe protocol')); child.kill(); return;
          }
          this.ready = true;
          this.config.resume = event.session_id;
          if (!settled) { settled = true; clearTimeout(timer); resolve(event); }
        }
        if (event.type === 'done') this.pending.delete(event.request_id);
        if (event.type === 'error' && ['invalid_request', 'queue_full', 'request_id_conflict'].includes(event.code)) {
          this.pending.delete(event.request_id);
        }
        this.emit('event', event);
      });
      child.once('error', error => { fail(error); this.emit('transport-error', error); });
      child.once('close', (code, signal) => {
        clearTimeout(timer);
        fail(new Error(`Nova exited before readiness (${code ?? signal})`));
        lines.close();
        this.child = null;
        this.ready = false;
        // Do not replay prompts after a crash: tools may have already executed.
        const interrupted = [...this.pending.keys()];
        this.pending.clear();
        this.emit('exit', { code, signal, interrupted });
      });
    });
  }

  send(frame) {
    if (!this.child || !this.ready || this.child.stdin.destroyed) {
      throw new Error('Wait for Nova ready before sending commands');
    }
    const line = JSON.stringify(frame) + '\n';
    if (Buffer.byteLength(line, 'utf8') > 1024 * 1024) throw new Error('Frame exceeds 1 MiB');
    if (this.child.stdin.writableLength > 1024 * 1024) throw new Error('Nova input is backpressured');
    this.child.stdin.write(line);
  }

  prompt(id, content) {
    if (this.pending.has(id) && this.pending.get(id) !== content) {
      throw new Error('Request ID already has different content');
    }
    this.send({ type: 'prompt', id, content });
    this.pending.set(id, content);
  }

  async stop(graceMs = 10_000) {
    const child = this.child;
    if (!child) return;
    await new Promise(resolve => {
      const timer = setTimeout(() => child.kill(), graceMs);
      child.once('close', () => { clearTimeout(timer); resolve(); });
      if (this.ready) this.send({ type: 'shutdown' });
      else child.kill();
    });
  }
}
