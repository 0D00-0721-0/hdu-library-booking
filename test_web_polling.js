// Execute the actual page functions with mocked transport and virtual timers.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "web_app.py"), "utf8");
const page = source.slice(source.indexOf("<script>") + 8, source.indexOf("</script>"));
new vm.Script(page);
function functionSource(name, next) {
  const start = page.indexOf(`    async function ${name}(`);
  const end = page.indexOf(`    async function ${next}(`, start);
  assert.ok(start >= 0 && end > start);
  return page.slice(start, end);
}

let checks = 0;
async function check(name, action) {
  await action();
  checks++;
  console.log(`PASS ${name}`);
}

function polling(replies) {
  const timers = new Map();
  const requests = [];
  let timerId = 0;
  const ctx = vm.createContext({
    currentJobId: "job-1", pollFailures: 0, pollTimer: null,
    busy: true, status: "执行中", notice: "", refreshes: 0,
    fields: {logBox: {textContent: "", scrollHeight: 0}},
    setBusy(value) { ctx.busy = value; },
    setStatus(value) { ctx.status = value; },
    setNotice(value) { ctx.notice = value; },
    setTimeout(fn, ms) { const id = ++timerId; timers.set(id, {fn, ms}); return id; },
    clearTimeout(id) { timers.delete(id); },
    async requestJson(url, options, timeout) {
      requests.push({url, options, timeout});
      assert.ok(url.startsWith("/api/job?id="));
      assert.equal(options.method, undefined); // State reads only.
      const reply = replies.shift();
      if (reply instanceof Error) throw reply;
      return await reply;
    },
    async loadBookings() { ctx.refreshes++; },
  });
  vm.runInContext(functionSource("pollJob", "resumeActiveJob"), ctx);
  return {
    ctx, timers, requests,
    poll: () => ctx.pollJob("job-1"),
    async tick() {
      assert.equal(timers.size, 1);
      const [id, timer] = timers.entries().next().value;
      timers.delete(id);
      await timer.fn();
    },
  };
}

async function main() {
  await check("temporary failure retains task and reconnects", async () => {
    const p = polling([new Error("offline"), {status: "running", logs: ["waiting"], poll_after_ms: 2000}]);
    await p.poll();
    assert.equal(p.ctx.currentJobId, "job-1");
    assert.equal(p.ctx.busy, true);
    assert.equal(p.ctx.status, "连接恢复中");
    await p.tick();
    assert.equal(p.ctx.status, "执行中");
    assert.equal(p.ctx.pollFailures, 0);
    assert.equal(p.ctx.fields.logBox.textContent, "waiting\n");
    assert.equal([...p.timers.values()][0].ms, 2000);
    assert.equal(p.requests[0].timeout, 5000);
  });

  await check("repeated read failures have capped backoff", async () => {
    const p = polling(Array.from({length: 8}, () => new Error("offline")));
    const delays = [];
    await p.poll();
    for (let i = 0; i < 8; i++) {
      delays.push([...p.timers.values()][0].ms);
      if (i < 7) await p.tick();
    }
    assert.deepEqual(delays, [1000, 2000, 4000, 8000, 10000, 10000, 10000, 10000]);
    assert.equal(p.ctx.currentJobId, "job-1");
    assert.equal(p.ctx.busy, true);
  });

  for (const [state, label] of [["done", "完成"], ["uncertain", "待确认"], ["error", "失败"], ["cancelled", "已取消"]]) {
    await check(`recover terminal state ${state}`, async () => {
      const p = polling([new Error("offline"), {status: state, logs: [], message: "finished", error: "details"}]);
      await p.poll();
      await p.tick();
      assert.equal(p.ctx.status, label);
      assert.equal(p.ctx.busy, false);
      assert.equal(p.ctx.currentJobId, "");
      assert.equal(p.timers.size, 0);
      assert.equal(p.ctx.refreshes, 1);
    });
  }

  await check("missing job is uncertain rather than failed", async () => {
    const error = Object.assign(new Error("missing"), {code: "job_not_found", status: 404});
    const p = polling([error]);
    await p.poll();
    assert.equal(p.ctx.status, "待确认");
    assert.equal(p.ctx.currentJobId, "");
    assert.equal(p.timers.size, 0);
    assert.equal(p.ctx.refreshes, 1);
  });

  for (const fails of [false, true]) {
    await check(`stale ${fails ? "failure" : "response"} cannot overwrite new task`, async () => {
      let resolve, reject;
      const pending = new Promise((a, b) => { resolve = a; reject = b; });
      const p = polling([pending]);
      const running = p.poll();
      p.ctx.currentJobId = "job-2";
      if (fails) reject(new Error("old failure"));
      else resolve({status: "done", logs: []});
      await running;
      assert.equal(p.ctx.currentJobId, "job-2");
      assert.equal(p.ctx.busy, true);
      assert.equal(p.timers.size, 0);
    });
  }

  for (const interval of [2000, 2501, undefined]) {
    await check(`preserve server polling interval ${interval}`, async () => {
      const p = polling([{status: "running", logs: [], poll_after_ms: interval}]);
      await p.poll();
      assert.equal([...p.timers.values()][0].ms, interval || 1000);
    });
  }

  function transport(fetch) {
    const timers = new Map();
    let id = 0;
    const ctx = vm.createContext({
      fetch, AbortController,
      setTimeout(fn, ms) { const key = ++id; timers.set(key, {fn, ms}); return key; },
      clearTimeout(key) { timers.delete(key); },
    });
    vm.runInContext(functionSource("requestJson", "loadConfig"), ctx);
    return {ctx, timers};
  }
  await check("request timeout aborts status read and clears timer", async () => {
    const t = transport(async (_url, options) => new Promise((_resolve, reject) => {
      options.signal.addEventListener("abort", () => reject(new Error("aborted")));
    }));
    const pending = t.ctx.requestJson("/api/job", {}, 5000);
    const timer = [...t.timers.values()][0];
    assert.equal(timer.ms, 5000);
    timer.fn();
    await assert.rejects(pending, /aborted/);
    assert.equal(t.timers.size, 0);
  });
  await check("structured API error survives transport", async () => {
    const t = transport(async () => ({ok: false, status: 404, json: async () => ({error: "missing", code: "job_not_found"})}));
    await assert.rejects(t.ctx.requestJson("/api/job", {}, 5000), error => error.code === "job_not_found" && error.status === 404);
    assert.equal(t.timers.size, 0);
  });
  await check("successful state read clears timeout", async () => {
    const t = transport(async () => ({ok: true, json: async () => ({status: "running"})}));
    const response = await t.ctx.requestJson("/api/job", {}, 5000);
    assert.equal(response.status, "running");
    assert.equal(t.timers.size, 0);
  });
  await check("mutation is sent once without automatic timeout or retry", async () => {
    let sent = 0;
    const t = transport(async (_url, options) => {
      sent++;
      assert.equal(options.signal, undefined);
      assert.equal(options.method, "POST");
      throw new Error("connection lost");
    });
    await assert.rejects(t.ctx.requestJson("/api/run", {method: "POST"}), /connection lost/);
    assert.equal(sent, 1);
    assert.equal(t.timers.size, 0);
  });
  await check("clock button shows fresh recommendation during a waiting task", async () => {
    assert.ok(source.includes('id="measureClockBtn"'));
    const ctx = vm.createContext({
      busyState: true, clockMeasuring: false,
      fields: {
        configPath: {value: "/tmp/config.yaml"},
        executeAt: {value: "20:00:00.300"},
        measureClockBtn: {disabled: false},
        clockResult: {hidden: true, className: "", textContent: ""},
      },
      async requestJson(url) {
        assert.ok(url.startsWith("/api/clock-offset?path="));
        return {
          measured_at: "19:30:00",
          message: "服务端与本机的时差最多约 0.509 秒；本次无法判断谁快",
          recommendation: {execute_at: "20:00:00.159", basis: "按服务器 20:00:00 开放计算"},
        };
      },
    });
    vm.runInContext(functionSource("measureClock", "savePlan"), ctx);
    await ctx.measureClock();
    assert.match(ctx.fields.clockResult.textContent, /建议执行时间：20:00:00\.159/);
    assert.match(ctx.fields.clockResult.textContent, /无法判断谁快/);
    assert.match(ctx.fields.clockResult.textContent, /当前填写：20:00:00\.300/);
    assert.match(ctx.fields.clockResult.textContent, /已启动的任务仍按原执行时间运行/);
    assert.equal(ctx.fields.clockResult.hidden, false);
    assert.equal(ctx.fields.measureClockBtn.disabled, false);
  });
  console.log(`JavaScript syntax and ${checks} offline behavior checks passed.`);
}

main().catch(error => { console.error(error); process.exitCode = 1; });
