/**
 * Live behavioral test for ACK-PACED (network) backpressure.
 *
 * test_backpressure_live.js proves the CLIENT-backlog (CPU) path: a client that
 * *reports* a high decode backlog gets a thinned (dropped) stream. That path has
 * a blind spot — when the NETWORK is the bottleneck the frames pile up in
 * buffers between server and client, the client's decode buffer stays at 0, and
 * the server (uvicorn does not back-pressure the ASGI send) keeps producing
 * until the pipe chokes and the keep-alive ping starves, resetting the socket
 * (raised by @YusufB5 testing PR #30 under Chrome "Slow 3G").
 *
 * The fix: the client also reports the highest frame index it has *received*
 * ("recv"), and on a network bottleneck the server PACES — it stops producing
 * until acks catch up, rather than dropping. ASCILINE plays finite videos only,
 * so a slow network must never cost scenes; dropping is reserved for the CPU
 * path (you can't wait out a CPU deficit). This test reproduces the slow link
 * with a throttling TCP proxy. Both clients report depth:0 (no CPU backlog) and
 * a truthful recv, so the only thing in play is the network (ack) path. We
 * assert:
 *
 *   - the slow client's stream is CONTIGUOUS (no skipped indices) -> no scenes
 *     dropped, and it isn't reset (collect() rejects on socket error)
 *   - the slow client gets FEWER frames than fast -> it was actually paced
 *   - a fast client (acks keep up) gets a contiguous stream -> no false pacing
 *
 * Requires: ffmpeg + a Python with the server deps. Override interpreter with
 * ASCIL_PY (e.g. ASCIL_PY=/data/ascil-venv/bin/python).
 *
 * Usage: node test/test_transport_backpressure.js
 */
const { spawn, execFileSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const net = require('net');
const path = require('path');

const PY = process.env.ASCIL_PY || 'python3';
const REPO = path.dirname(__dirname);
const WINDOW_MS = 14000;         // collection window per client
const SLOW_BYTES_PER_SEC = 60000;// server->client drip rate (~Slow 3G; ~10x below the ~575 KB/s offered)
const TICK_MS = 100;             // token-bucket refill interval

function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.listen(0, '127.0.0.1', () => {
      const port = srv.address().port;
      srv.close(() => resolve(port));
    });
    srv.on('error', reject);
  });
}

function waitForPort(port, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const tryOnce = () => {
      const sock = net.connect(port, '127.0.0.1');
      sock.on('connect', () => { sock.destroy(); resolve(); });
      sock.on('error', () => {
        sock.destroy();
        if (Date.now() > deadline) reject(new Error('server did not start'));
        else setTimeout(tryOnce, 150);
      });
    };
    tryOnce();
  });
}

/**
 * A TCP proxy that forwards client<->upstream but throttles the upstream->client
 * direction to exactly `bytesPerSec`. Incoming server data is queued and the
 * timer releases at most `bytesPerSec * TICK_MS/1000` bytes per tick, SLICING
 * the head chunk when needed (a naive "pause after the budget is spent" leaks at
 * one TCP chunk — ~64 KB — per tick). When the queue backs up past ~1s of data
 * we pause the upstream read, which backpropagates and saturates the server's
 * OS send buffer — exactly like a slow real link. Returns { port, close }.
 */
function startThrottleProxy(upstreamPort, bytesPerSec) {
  return new Promise((resolve) => {
    const conns = new Set();
    const perTick = Math.max(1, Math.floor(bytesPerSec * (TICK_MS / 1000)));
    const HIGH_WATER = bytesPerSec; // ~1s queued -> stop reading upstream
    const server = net.createServer((client) => {
      const upstream = net.connect(upstreamPort, '127.0.0.1');
      conns.add(client); conns.add(upstream);
      let queue = [];     // pending server->client buffers
      let qBytes = 0;

      client.pipe(upstream);            // client->server: unthrottled
      upstream.on('data', (chunk) => {  // server->client: queued, drained on tick
        queue.push(chunk); qBytes += chunk.length;
        if (qBytes > HIGH_WATER) upstream.pause();
      });
      const tick = setInterval(() => {
        let allow = perTick;
        while (allow > 0 && queue.length) {
          const head = queue[0];
          if (head.length <= allow) {
            client.write(head); allow -= head.length; qBytes -= head.length; queue.shift();
          } else {
            client.write(head.subarray(0, allow)); queue[0] = head.subarray(allow);
            qBytes -= allow; allow = 0;
          }
        }
        if (qBytes < HIGH_WATER) upstream.resume();
      }, TICK_MS);

      const shut = () => {
        clearInterval(tick);
        try { client.destroy(); } catch (_) {}
        try { upstream.destroy(); } catch (_) {}
      };
      client.on('close', shut); upstream.on('close', shut);
      client.on('error', shut); upstream.on('error', shut);
    });
    server.listen(0, '127.0.0.1', () => {
      resolve({
        port: server.address().port,
        close: () => { for (const c of conns) { try { c.destroy(); } catch (_) {} } server.close(); },
      });
    });
  });
}

// Collect frame indices for WINDOW_MS. Like the real client, it reports a buffer
// message with depth:0 (no CPU backlog) and recv = highest index received, so
// the only thing in play here is the ack-pacing (network) path.
function collect(port) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(`ws://127.0.0.1:${port}/ws?codec=adaptive`);
    ws.binaryType = 'arraybuffer';
    const indices = [];
    let lastRecv = -1, timer = null, acker = null;
    const stop = () => {
      if (timer) clearTimeout(timer);
      if (acker) clearInterval(acker);
      try { ws.close(); } catch (_) {}
      resolve(indices);
    };
    ws.onopen = () => {
      timer = setTimeout(stop, WINDOW_MS);
      acker = setInterval(() => {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: 'buffer', depth: 0, recv: lastRecv }));
        }
      }, 250);
    };
    ws.onmessage = (ev) => {
      if (typeof ev.data === 'string') return; // INIT / status
      const idx = new DataView(ev.data).getUint32(0, false);
      lastRecv = idx;
      indices.push(idx);
    };
    ws.onerror = (e) => { if (timer) clearTimeout(timer); reject(e.error || new Error('ws error')); };
  });
}

function maxGap(indices) {
  let m = 0;
  for (let i = 1; i < indices.length; i++) m = Math.max(m, indices[i] - indices[i - 1]);
  return m;
}

(async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'ascil-tbp-'));
  const clip = path.join(tmp, 'clip.mp4');
  let server = null, proxy = null;
  try {
    // 18s of full-frame NOISE at 24fps. Real ascii video has high per-frame
    // entropy; a compressible testsrc would shrink to ~1 KB/s and never
    // saturate a link. Noise keeps frames incompressible so the offered rate
    // (~575 KB/s) genuinely exceeds the slow link, just like the real footage
    // that surfaced this bug.
    execFileSync('ffmpeg', [
      '-y', '-f', 'lavfi', '-i', 'testsrc=s=320x240:r=24:d=18',
      '-vf', 'noise=alls=80:allf=t+u',
      '-pix_fmt', 'yuv420p', clip,
    ], { stdio: 'ignore' });

    const port = await freePort();
    server = spawn(PY, ['stream_server.py', clip, '--mode', '2', '--vol', '0',
      '--cols', '200', '--no-thumbnails', '--host', '127.0.0.1', '--port', String(port)],
      { cwd: REPO, stdio: ['pipe', 'ignore', 'ignore'] });
    server.on('error', (e) => { throw e; });
    await waitForPort(port, 15000);

    // Fast drain straight to the server (healthy link), then a slow drain
    // through the throttling proxy. Both report depth:0 and a truthful recv.
    const fast = await collect(port);
    proxy = await startThrottleProxy(port, SLOW_BYTES_PER_SEC);
    const slow = await collect(proxy.port);

    // Reaching this point at all proves the slow link did NOT reset the socket
    // (collect() rejects on ws error) — that reset was the original slow-link bug.
    const checks = [
      ['fast client received frames', fast.length > 5, `got ${fast.length}`],
      ['fast (healthy link) stream is contiguous', maxGap(fast) <= 1,
        `maxGap=${maxGap(fast)}`],
      ['slow client received some frames (not starved/reset)', slow.length > 0,
        `got ${slow.length}`],
      ['slow (saturated link) stream is CONTIGUOUS — paced, no scenes dropped',
        maxGap(slow) <= 1, `maxGap=${maxGap(slow)}`],
      ['slow received fewer frames than fast — actually paced to the link',
        slow.length < fast.length, `slow=${slow.length} fast=${fast.length}`],
    ];

    let failed = 0;
    for (const [name, ok, why] of checks) {
      console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${ok ? '' : '  -> ' + why}`);
      if (!ok) failed++;
    }
    console.log(`\nfast: ${fast.length} frames (maxGap ${maxGap(fast)})  |  ` +
      `slow: ${slow.length} frames (maxGap ${maxGap(slow)})`);
    console.log(`${checks.length - failed}/${checks.length} passed`);
    process.exitCode = failed === 0 ? 0 : 1;
  } finally {
    if (proxy) proxy.close();
    if (server) server.kill('SIGKILL');
    fs.rmSync(tmp, { recursive: true, force: true });
  }
})().catch((e) => { console.error('ERROR', e); process.exit(2); });
