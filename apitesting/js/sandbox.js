'use strict';
/*
 * Runs Postman-style pre-request / test scripts for BaigNova API Testing.
 *
 * Python starts this file once per run and talks to it with one JSON object per line:
 *   in : {id, event, scripts: [{name, code}], request, response, vars: {environment, collection, project, globals, local}, info}
 *   out: {id, tests, console, errors, vars, request}
 * Node is started with --disallow-code-generation-from-strings and --permission, so scripts cannot use
 * eval / Function, the file system or child processes. Each script has a CPU time limit.
 */
const vm = require('vm');
const readline = require('readline');

const SCRIPT_TIMEOUT_MS = 5000;
const MAX_CONSOLE = 200;

function fmt(v) {
  if (typeof v === 'string') return v;
  if (v === undefined) return 'undefined';
  try { return JSON.stringify(v); } catch (e) { return String(v); }
}

function isObj(v) { return v !== null && typeof v === 'object'; }

function deepEqual(a, b) {
  if (a === b) return true;
  if (typeof a === 'number' && typeof b === 'number') return Number.isNaN(a) && Number.isNaN(b);
  if (!isObj(a) || !isObj(b) || Array.isArray(a) !== Array.isArray(b)) return false;
  const ka = Object.keys(a), kb = Object.keys(b);
  return ka.length === kb.length && ka.every(k => Object.prototype.hasOwnProperty.call(b, k) && deepEqual(a[k], b[k]));
}

function typeOf(v) {
  if (v === null) return 'null';
  if (Array.isArray(v)) return 'array';
  return typeof v;
}

function getPath(obj, path) {
  let cur = obj;
  for (const part of String(path).replace(/\[(\d+)\]/g, '.$1').split('.').filter(Boolean)) {
    if (!isObj(cur) || !(part in cur)) return {found: false};
    cur = cur[part];
  }
  return {found: true, value: cur};
}

// ---------- pm.expect: a compact chai-style assertion library ----------

class AssertionError extends Error {
  constructor(message) { super(message); this.name = 'AssertionError'; }
}

class Assertion {
  constructor(obj, message) {
    this._obj = obj; this._msg = message; this._neg = false; this._deep = false; this._any = false; this._incl = false;
  }
  _check(ok, positive, negative) {
    if (this._neg ? ok : !ok) throw new AssertionError((this._msg ? `${this._msg}: ` : '') + (this._neg ? negative : positive));
    return this;
  }
  get to() { return this; } get be() { return this; } get been() { return this; } get is() { return this; }
  get that() { return this; } get which() { return this; } get and() { return this; } get has() { return this; }
  get have() { return this; } get with() { return this; } get at() { return this; } get of() { return this; }
  get same() { return this; } get but() { return this; } get does() { return this; } get still() { return this; }
  get own() { return this; } get nested() { return this; } get ordered() { return this; }
  get not() { this._neg = !this._neg; return this; }
  get deep() { this._deep = true; return this; }
  get any() { this._any = true; return this; }
  get all() { this._any = false; return this; }
  get include() { this._incl = true; return makeCallable(this, 'includeFn'); }
  get includes() { return this.include; } get contain() { return this.include; } get contains() { return this.include; }

  get ok() { return this._check(!!this._obj, `expected ${fmt(this._obj)} to be truthy`, `expected ${fmt(this._obj)} to be falsy`); }
  get true() { return this._check(this._obj === true, `expected ${fmt(this._obj)} to be true`, `expected ${fmt(this._obj)} to not be true`); }
  get false() { return this._check(this._obj === false, `expected ${fmt(this._obj)} to be false`, `expected ${fmt(this._obj)} to not be false`); }
  get null() { return this._check(this._obj === null, `expected ${fmt(this._obj)} to be null`, 'expected value to not be null'); }
  get undefined() { return this._check(this._obj === undefined, `expected ${fmt(this._obj)} to be undefined`, 'expected value to not be undefined'); }
  get exist() { return this._check(this._obj !== null && this._obj !== undefined, `expected ${fmt(this._obj)} to exist`, `expected ${fmt(this._obj)} to not exist`); }
  get empty() {
    const o = this._obj;
    const n = typeof o === 'string' || Array.isArray(o) ? o.length : isObj(o) ? Object.keys(o).length : NaN;
    return this._check(n === 0, `expected ${fmt(o)} to be empty`, `expected ${fmt(o)} to not be empty`);
  }
  get NaN() { return this._check(Number.isNaN(this._obj), `expected ${fmt(this._obj)} to be NaN`, `expected ${fmt(this._obj)} to not be NaN`); }

  equal(v) {
    const ok = this._deep ? deepEqual(this._obj, v) : this._obj === v;
    return this._check(ok, `expected ${fmt(this._obj)} to equal ${fmt(v)}`, `expected ${fmt(this._obj)} to not equal ${fmt(v)}`);
  }
  equals(v) { return this.equal(v); } eq(v) { return this.equal(v); }
  eql(v) { return this._check(deepEqual(this._obj, v), `expected ${fmt(this._obj)} to deeply equal ${fmt(v)}`, `expected ${fmt(this._obj)} to not deeply equal ${fmt(v)}`); }
  eqls(v) { return this.eql(v); }
  above(n) { return this._check(this._obj > n, `expected ${fmt(this._obj)} to be above ${n}`, `expected ${fmt(this._obj)} to be at most ${n}`); }
  gt(n) { return this.above(n); } greaterThan(n) { return this.above(n); }
  least(n) { return this._check(this._obj >= n, `expected ${fmt(this._obj)} to be at least ${n}`, `expected ${fmt(this._obj)} to be below ${n}`); }
  gte(n) { return this.least(n); } greaterThanOrEqual(n) { return this.least(n); }
  below(n) { return this._check(this._obj < n, `expected ${fmt(this._obj)} to be below ${n}`, `expected ${fmt(this._obj)} to be at least ${n}`); }
  lt(n) { return this.below(n); } lessThan(n) { return this.below(n); }
  most(n) { return this._check(this._obj <= n, `expected ${fmt(this._obj)} to be at most ${n}`, `expected ${fmt(this._obj)} to be above ${n}`); }
  lte(n) { return this.most(n); } lessThanOrEqual(n) { return this.most(n); }
  within(a, b) { return this._check(this._obj >= a && this._obj <= b, `expected ${fmt(this._obj)} to be within ${a}..${b}`, `expected ${fmt(this._obj)} to not be within ${a}..${b}`); }
  a(type) {
    const t = typeOf(this._obj), want = String(type).toLowerCase();
    return this._check(t === want, `expected ${fmt(this._obj)} to be a ${want}`, `expected ${fmt(this._obj)} not to be a ${want}`);
  }
  an(type) { return this.a(type); }
  instanceOf(ctor) { return this._check(this._obj instanceof ctor, `expected ${fmt(this._obj)} to be an instance of ${ctor && ctor.name}`, `expected ${fmt(this._obj)} to not be an instance of ${ctor && ctor.name}`); }
  instanceof(ctor) { return this.instanceOf(ctor); }
  includeFn(v) {
    const o = this._obj;
    let ok;
    if (typeof o === 'string') ok = o.includes(v);
    else if (Array.isArray(o)) ok = o.some(x => (this._deep || isObj(v)) ? deepEqual(x, v) : x === v);
    else if (isObj(o) && isObj(v)) ok = Object.keys(v).every(k => deepEqual(o[k], v[k]));
    else ok = false;
    return this._check(ok, `expected ${fmt(o)} to include ${fmt(v)}`, `expected ${fmt(o)} to not include ${fmt(v)}`);
  }
  string(s) { return this._check(typeof this._obj === 'string' && this._obj.includes(s), `expected ${fmt(this._obj)} to contain ${fmt(s)}`, `expected ${fmt(this._obj)} to not contain ${fmt(s)}`); }
  match(re) { return this._check(re.test(String(this._obj)), `expected ${fmt(this._obj)} to match ${re}`, `expected ${fmt(this._obj)} not to match ${re}`); }
  matches(re) { return this.match(re); }
  oneOf(list) { return this._check(list.some(x => this._deep ? deepEqual(x, this._obj) : x === this._obj), `expected ${fmt(this._obj)} to be one of ${fmt(list)}`, `expected ${fmt(this._obj)} to not be one of ${fmt(list)}`); }
  property(name, ...value) {
    const direct = isObj(this._obj) && name in this._obj;
    const r = direct ? {found: true, value: this._obj[name]} : (isObj(this._obj) ? getPath(this._obj, name) : {found: false});
    if (!value.length) {
      this._check(r.found, `expected ${fmt(this._obj)} to have property '${name}'`, `expected ${fmt(this._obj)} to not have property '${name}'`);
      if (!this._neg) this._obj = r.value;
      return this;
    }
    const ok = r.found && (this._deep ? deepEqual(r.value, value[0]) : r.value === value[0]);
    return this._check(ok, `expected property '${name}' to be ${fmt(value[0])} but got ${fmt(r.value)}`, `expected property '${name}' to not be ${fmt(value[0])}`);
  }
  keys(...names) {
    if (names.length === 1 && Array.isArray(names[0])) names = names[0];
    const keys = isObj(this._obj) ? Object.keys(this._obj) : [];
    const ok = this._any ? names.some(n => keys.includes(n)) : names.every(n => keys.includes(n));
    return this._check(ok, `expected ${fmt(this._obj)} to have ${this._any ? 'any' : 'all'} of keys ${fmt(names)}`, `expected ${fmt(this._obj)} to not have keys ${fmt(names)}`);
  }
  key(...names) { return this.keys(...names); }
  lengthOf(n) {
    const len = this._obj == null ? undefined : this._obj.length;
    return this._check(len === n, `expected ${fmt(this._obj)} to have a length of ${n} but got ${len}`, `expected ${fmt(this._obj)} to not have a length of ${n}`);
  }
  length(n) { return this.lengthOf(n); }
  members(list) {
    const o = Array.isArray(this._obj) ? this._obj : [];
    const has = x => o.some(y => deepEqual(x, y));
    const ok = this._incl ? list.every(has) : (list.length === o.length && list.every(has));
    return this._check(ok, `expected ${fmt(o)} to have the members ${fmt(list)}`, `expected ${fmt(o)} to not have the members ${fmt(list)}`);
  }
  satisfy(fn) { return this._check(!!fn(this._obj), `expected ${fmt(this._obj)} to satisfy the given function`, `expected ${fmt(this._obj)} to not satisfy the given function`); }
}

// `include` works both as a word in the chain (`.to.include.keys(...)`) and as a call (`.to.include(x)`)
function makeCallable(assertion, method) {
  const fn = (...args) => assertion[method](...args);
  return new Proxy(fn, {
    get(target, prop) {
      const v = assertion[prop];
      return typeof v === 'function' ? v.bind(assertion) : v;
    },
  });
}

function expect(value, message) { return new Assertion(value, message); }

// ---------- helpers exposed to scripts ----------

function headerList(entries) {
  const items = (entries || []).map(h => Array.isArray(h) ? {key: h[0], value: h[1], disabled: false}
                                                          : {key: h.key, value: h.value, disabled: !!h.disabled});
  const same = (a, b) => String(a).toLowerCase() === String(b).toLowerCase();
  return {
    get(name) { const h = items.find(i => !i.disabled && same(i.key, name)); return h ? h.value : undefined; },
    has(name, value) { const v = this.get(name); return v !== undefined && (value === undefined || v === value); },
    add(h) { items.push({key: String(h.key), value: fmt(h.value), disabled: false}); },
    upsert(h) {
      const e = items.find(i => same(i.key, h.key));
      if (e) { e.value = fmt(h.value); e.disabled = false; } else this.add(h);
    },
    remove(name) { for (let i = items.length - 1; i >= 0; i--) if (same(items[i].key, name)) items.splice(i, 1); },
    toObject() { const o = {}; items.filter(i => !i.disabled).forEach(i => { o[i.key] = i.value; }); return o; },
    all() { return items.filter(i => !i.disabled).map(i => ({key: i.key, value: i.value})); },
    each(fn) { items.filter(i => !i.disabled).forEach(i => fn({key: i.key, value: i.value})); },
    count() { return items.filter(i => !i.disabled).length; },
    _items: items,
  };
}

function scope(values) {
  const v = Object.assign({}, values || {});
  return {
    get: k => v[k],
    set: (k, value) => { v[k] = value; },
    unset: k => { delete v[k]; },
    has: k => Object.prototype.hasOwnProperty.call(v, k),
    clear: () => { Object.keys(v).forEach(k => delete v[k]); },
    toObject: () => Object.assign({}, v),
    _values: v,
  };
}

function xml2Json(text) {
  // small XML -> object converter for XML APIs: <a x="1"><b>2</b><b>3</b></a> -> {a: {$: {x: '1'}, b: ['2', '3']}}
  const src = String(text || '').replace(/<\?[\s\S]*?\?>/g, '').replace(/<!--[\s\S]*?-->/g, '');
  const re = /<\/?([\w:.\-]+)((?:\s+[\w:.\-]+\s*=\s*(?:"[^"]*"|'[^']*'))*)\s*(\/?)>|([^<]+)/g;
  const root = {children: {}}, stack = [root];
  let m;
  const decode = s => s.replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&apos;/g, "'").replace(/&amp;/g, '&');
  const put = (node, name, value) => {
    if (name in node.children) {
      if (!Array.isArray(node.children[name])) node.children[name] = [node.children[name]];
      node.children[name].push(value);
    } else node.children[name] = value;
  };
  const finish = node => {
    const keys = Object.keys(node.children);
    if (!keys.length && !node.attrs) return node.text.trim();
    const out = Object.assign({}, node.children);
    if (node.attrs) out.$ = node.attrs;
    if (node.text.trim()) out._ = node.text.trim();
    return out;
  };
  while ((m = re.exec(src))) {
    if (m[4] !== undefined) { stack[stack.length - 1].text = (stack[stack.length - 1].text || '') + decode(m[4]); continue; }
    const isClose = m[0].startsWith('</');
    if (isClose) {
      const node = stack.pop();
      put(stack[stack.length - 1], node.name, finish(node));
      continue;
    }
    let attrs = null;
    (m[2] || '').replace(/([\w:.\-]+)\s*=\s*("([^"]*)"|'([^']*)')/g, (_, k, __, a, b) => { (attrs = attrs || {})[k] = decode(a !== undefined ? a : b); });
    const node = {name: m[1], children: {}, attrs, text: ''};
    if (m[3]) put(stack[stack.length - 1], node.name, finish(node));
    else stack.push(node);
  }
  return root.children;
}

// ---------- one script event ----------

function runEvent(msg) {
  const tests = [], consoleLines = [], errors = [];
  const vars = msg.vars || {};
  const environment = scope(vars.environment), collection = scope(vars.collection), globals = scope(vars.globals);
  const project = scope(vars.project), local = scope(vars.local), lov = scope(vars.lov);
  const resolve = k => [local, environment, collection, project, globals, lov].map(s => s._values).find(v => Object.prototype.hasOwnProperty.call(v, k));
  const variables = {
    get: k => { const s = resolve(k); return s ? s[k] : undefined; },
    set: (k, v) => local.set(k, v),
    unset: k => local.unset(k),
    has: k => !!resolve(k),
    toObject: () => Object.assign({}, globals._values, project._values, collection._values, environment._values, local._values),
    replaceIn: t => String(t).replace(/\{\{\s*([\w.\-$]+)\s*\}\}/g, (all, k) => { const s = resolve(k); return s ? fmt(s[k]) : all; }),
  };

  const req = msg.request || {};
  const requestHeaders = headerList(req.headers || []);
  const body = {
    mode: (req.body && req.body.mode) || 'none',
    raw: (req.body && req.body.raw) || '',
    update(v) { this.raw = typeof v === 'string' ? v : JSON.stringify(v, null, 2); },
    toString() { return this.raw; },
  };
  let url = req.url || '';
  const urlObject = () => ({toString: () => url, update: u => { url = String(u); }, getHost: () => { try { return new URL(url).host; } catch (e) { return ''; } }});
  const request = {
    method: req.method || 'GET',
    headers: requestHeaders,
    body,
    get url() { return urlObject(); },
    set url(u) { url = String(u); },
    addHeader: h => requestHeaders.add(h),
    removeHeader: n => requestHeaders.remove(n),
  };

  let response;
  const res = msg.response;
  if (res) {
    const headers = headerList(res.headers || []);
    const text = res.body || '';
    let parsed;
    const json = () => {
      if (parsed === undefined) {
        try { parsed = JSON.parse(text); } catch (e) { throw new Error(`Response body is not valid JSON: ${e.message}`); }
      }
      return parsed;
    };
    const statusCheck = (neg) => {
      const check = (ok, pos, negMsg) => { if (neg ? ok : !ok) throw new AssertionError(neg ? negMsg : pos); };
      const c = res.code;
      const be = {};
      const flag = (name, ok, what) => Object.defineProperty(be, name, {get() { check(ok, `expected response to be ${what} but got ${c}`, `expected response to not be ${what}`); return be; }});
      flag('ok', c >= 200 && c < 300, 'ok (2xx)'); flag('success', c >= 200 && c < 300, 'successful (2xx)');
      flag('info', c >= 100 && c < 200, 'informational (1xx)'); flag('redirection', c >= 300 && c < 400, 'a redirect (3xx)');
      flag('clientError', c >= 400 && c < 500, 'a client error (4xx)'); flag('serverError', c >= 500, 'a server error (5xx)');
      flag('error', c >= 400, 'an error (4xx/5xx)'); flag('notFound', c === 404, 'not found (404)');
      flag('unauthorized', c === 401, 'unauthorized (401)'); flag('forbidden', c === 403, 'forbidden (403)');
      flag('badRequest', c === 400, 'a bad request (400)');
      Object.defineProperty(be, 'json', {get() { let ok = true; try { json(); } catch (e) { ok = false; } check(ok, 'expected response body to be JSON', 'expected response body to not be JSON'); return be; }});
      const have = {
        status(s) { check(typeof s === 'number' ? c === s : res.status === s, `expected response to have status ${fmt(s)} but got ${c}`, `expected response to not have status ${fmt(s)}`); return have; },
        header(k, v) { check(headers.has(k, v), `expected response to have header ${k}${v !== undefined ? ` = ${v}` : ''}`, `expected response to not have header ${k}`); return have; },
        body(b) {
          if (b === undefined) check(text.length > 0, 'expected response to have a body', 'expected response to not have a body');
          else check(b instanceof RegExp ? b.test(text) : text === fmt(b), `expected response body to be ${fmt(b)}`, `expected response body to not be ${fmt(b)}`);
          return have;
        },
        jsonBody(path, value) {
          let data; try { data = json(); } catch (e) { check(false, 'expected response body to be JSON', ''); }
          if (path === undefined) return have;
          if (isObj(path)) { check(deepEqual(data, path), `expected response JSON to equal ${fmt(path)}`, 'expected response JSON to differ'); return have; }
          const r = getPath(data, path);
          check(r.found && (value === undefined || deepEqual(r.value, value)), `expected response JSON to have '${path}'${value !== undefined ? ` = ${fmt(value)}` : ''}${r.found ? ` but got ${fmt(r.value)}` : ''}`, `expected response JSON to not have '${path}'`);
          return have;
        },
        responseTime: undefined,
      };
      return {be, have, get not() { return statusCheck(!neg); }};
    };
    response = {
      code: res.code, status: res.status || '', responseTime: res.responseTime || 0, responseSize: res.responseSize || 0,
      headers, text: () => text, json, xml: () => xml2Json(text),
      get to() { return statusCheck(false); },
    };
  }

  const info = Object.assign({eventName: msg.event, iteration: 0, iterationCount: 1, requestName: '', requestId: ''}, msg.info || {});
  const test = (name, fn) => {
    try {
      if (typeof fn === 'function') fn();
      tests.push({name: String(name), passed: true, error: ''});
    } catch (e) {
      tests.push({name: String(name), passed: false, error: e && e.message ? e.message : String(e)});
    }
  };
  test.skip = name => tests.push({name: String(name), passed: true, skipped: true, error: ''});

  const pm = {
    environment, collectionVariables: collection, projectVariables: project, globals, variables, request, info,
    iterationData: {get: () => undefined, has: () => false, toObject: () => ({})},
    test, expect,
    sendRequest: () => { throw new Error('pm.sendRequest is not supported yet in BaigNova.'); },
  };
  if (response) pm.response = response;

  const log = level => (...args) => {
    if (consoleLines.length < MAX_CONSOLE) consoleLines.push({level, text: args.map(fmt).join(' ')});
  };
  const legacyTests = {};
  const sandbox = {
    pm, console: {log: log('log'), info: log('info'), warn: log('warn'), error: log('error'), debug: log('debug')},
    tests: legacyTests, xml2Json, atob, btoa, JSON, Math, Date,
    postman: {
      setEnvironmentVariable: (k, v) => environment.set(k, v), getEnvironmentVariable: k => environment.get(k),
      clearEnvironmentVariable: k => environment.unset(k), setGlobalVariable: (k, v) => globals.set(k, v),
      getGlobalVariable: k => globals.get(k), clearGlobalVariable: k => globals.unset(k),
    },
  };
  if (response) {
    Object.assign(sandbox, {responseBody: response.text(), responseCode: {code: res.code, name: res.status || ''},
                            responseTime: response.responseTime, responseHeaders: response.headers.toObject()});
  }
  const context = vm.createContext(sandbox, {codeGeneration: {strings: false, wasm: false}});
  for (const s of msg.scripts || []) {
    if (!s.code || !String(s.code).trim()) continue;
    try {
      new vm.Script(String(s.code), {filename: s.name || 'script'}).runInContext(context, {timeout: SCRIPT_TIMEOUT_MS});
    } catch (e) {
      const message = e && e.message ? `${e.name || 'Error'}: ${e.message}` : String(e);
      errors.push({script: s.name || 'script', message: /timed out/.test(message) ? `Script ran longer than ${SCRIPT_TIMEOUT_MS / 1000} s and was stopped.` : message});
    }
  }
  for (const [name, ok] of Object.entries(legacyTests)) tests.push({name, passed: !!ok, error: ok ? '' : 'tests["..."] was false'});

  return {
    tests, console: consoleLines, errors,
    vars: {environment: environment._values, collection: collection._values, project: project._values, globals: globals._values, local: local._values},
    request: {url, method: request.method, headers: requestHeaders._items, body: {mode: body.mode, raw: body.raw}},
  };
}

const rl = readline.createInterface({input: process.stdin, terminal: false});
rl.on('line', line => {
  let msg = {};
  let out;
  try {
    msg = JSON.parse(line);
    out = runEvent(msg);
  } catch (e) {
    out = {tests: [], console: [], errors: [{script: 'sandbox', message: String(e && e.message || e)}], vars: msg.vars || {}, request: msg.request || {}};
  }
  out.id = msg.id;
  let text;
  try { text = JSON.stringify(out); } catch (e) {
    text = JSON.stringify({id: msg.id, tests: out.tests, console: [], errors: [{script: 'sandbox', message: `Variables could not be saved: ${e.message}`}], vars: msg.vars || {}, request: msg.request || {}});
  }
  process.stdout.write(text + '\n');
});
