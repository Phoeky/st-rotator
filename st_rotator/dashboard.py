"""控制台前端页面（单文件 HTML，零 CDN 依赖）。

为什么要塞在 Python 字符串里
---------------------------
这个工具是零依赖、可离线部署的。如果前端拆成独立的 .html/.js/.css 文件，就得处理
"打包后文件在哪"的问题；而引 CDN 又会让内网环境直接白屏。所以整页内联，由网关自己
吐出来——打开一个 URL 就有完整界面。

为什么是 Web 而不是 tkinter
--------------------------
托管 Python 不带 tkinter（`ModuleNotFoundError: No module named 'tkinter'`），
而系统自带的 3.9 又太老。用本地 Web 页面 + Edge 的 ``--app`` 模式开窗，没有地址栏和
标签页，观感和原生桌面程序一致，还能顺带把实时表格、日志、代码复制都做得很舒服。
"""

from __future__ import annotations

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>多 Key 轮换控制台</title>
<style>
  :root {
    --bg: #f4f6fa;
    --card: #ffffff;
    --border: #e2e7f0;
    --border-strong: #cfd7e6;
    --text: #16202f;
    --muted: #6a7688;
    --accent: #2f6feb;
    --accent-soft: #eaf1ff;
    --green: #0f7a55;
    --green-bg: #e4f6ee;
    --blue: #1f6feb;
    --blue-bg: #e8f1fe;
    --amber: #8a5b00;
    --amber-bg: #fff3d4;
    --red: #b3261e;
    --red-bg: #fdeceb;
    --mono: ui-monospace, "SFMono-Regular", "Cascadia Mono", Consolas, "Liberation Mono", monospace;
    --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #12161d;
      --card: #1a1f28;
      --border: #2a3240;
      --border-strong: #3a4456;
      --text: #e6ebf2;
      --muted: #93a0b4;
      --accent: #5b93ff;
      --accent-soft: #1e2b45;
      --green: #4fd1a5;
      --green-bg: #14302a;
      --blue: #5b93ff;
      --blue-bg: #16233f;
      --amber: #e8b84b;
      --amber-bg: #33290f;
      --red: #ff8078;
      --red-bg: #3a1d1c;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font-family: var(--sans); font-size: 13px; line-height: 1.5;
    -webkit-font-smoothing: antialiased;
  }
  .wrap { max-width: 1360px; margin: 0 auto; padding: 18px 20px 40px; }

  header {
    display: flex; align-items: center; gap: 14px;
    padding: 14px 18px; margin-bottom: 16px;
    background: var(--card); border: 1px solid var(--border); border-radius: 12px;
  }
  header h1 { font-size: 15px; margin: 0; font-weight: 650; letter-spacing: .2px; }
  header .sub { color: var(--muted); font-size: 12px; margin-top: 2px; }
  .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 6px; }
  .dot.on { background: #16a34a; box-shadow: 0 0 0 3px rgba(22,163,74,.16); }
  .dot.paused { background: #d97706; box-shadow: 0 0 0 3px rgba(217,119,6,.16); }
  .spacer { flex: 1; }

  button {
    font-family: inherit; font-size: 12.5px; cursor: pointer;
    border: 1px solid var(--border-strong); background: var(--card); color: var(--text);
    padding: 6px 13px; border-radius: 7px; transition: .14s;
  }
  button:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
  button:disabled { opacity: .45; cursor: not-allowed; }
  button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  button.primary:hover:not(:disabled) { filter: brightness(1.08); color: #fff; }
  button.danger:hover:not(:disabled) { border-color: var(--red); color: var(--red); }
  button.tiny { padding: 3px 9px; font-size: 11.5px; border-radius: 6px; }

  .grid { display: grid; gap: 14px; }
  .kpis { grid-template-columns: repeat(auto-fit, minmax(148px, 1fr)); margin-bottom: 14px; }
  .two { grid-template-columns: minmax(0, 1.35fr) minmax(0, 1fr); }
  @media (max-width: 980px) { .two { grid-template-columns: 1fr; } }

  .card {
    background: var(--card); border: 1px solid var(--border); border-radius: 12px;
    padding: 15px 17px; min-width: 0;
  }
  .card h2 {
    font-size: 12px; font-weight: 650; text-transform: uppercase; letter-spacing: .7px;
    color: var(--muted); margin: 0 0 12px; display: flex; align-items: center; gap: 8px;
  }
  .card h2 .spacer { flex: 1; }

  .kpi .label { font-size: 11.5px; color: var(--muted); letter-spacing: .3px; }
  .kpi .value { font-size: 22px; font-weight: 640; margin-top: 3px; font-variant-numeric: tabular-nums; }
  .kpi .value small { font-size: 12px; color: var(--muted); font-weight: 400; margin-left: 3px; }
  .kpi .value.green { color: var(--green); }
  .kpi .value.blue { color: var(--blue); }
  .kpi .value.amber { color: var(--amber); }
  .kpi .value.red { color: var(--red); }

  table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
  th {
    text-align: left; font-weight: 600; color: var(--muted); font-size: 11px;
    text-transform: uppercase; letter-spacing: .5px;
    padding: 0 8px 7px; border-bottom: 1px solid var(--border); white-space: nowrap;
  }
  td { padding: 8px; border-bottom: 1px solid var(--border); vertical-align: middle; }
  tr:last-child td { border-bottom: none; }
  td.mono { font-family: var(--mono); font-size: 11.5px; }
  td.num { text-align: right; font-variant-numeric: tabular-nums; }
  th.num { text-align: right; }

  .pill {
    display: inline-block; padding: 2px 8px; border-radius: 20px;
    font-size: 11px; font-weight: 600; white-space: nowrap;
  }
  .pill.healthy { background: var(--green-bg); color: var(--green); }
  .pill.inflight { background: var(--blue-bg); color: var(--blue); }
  .pill.cooldown { background: var(--amber-bg); color: var(--amber); }
  .pill.invalid { background: var(--red-bg); color: var(--red); }
  .pill.neutral { background: var(--accent-soft); color: var(--accent); }

  input, select {
    font-family: inherit; font-size: 12.5px; color: var(--text);
    background: var(--card); border: 1px solid var(--border-strong);
    border-radius: 7px; padding: 6px 10px; width: 100%;
  }
  input:focus, select:focus { outline: none; border-color: var(--accent); }
  input.mono { font-family: var(--mono); font-size: 12px; }
  label.field { display: block; margin-bottom: 9px; }
  label.field > span { display: block; font-size: 11.5px; color: var(--muted); margin-bottom: 4px; }
  .row { display: flex; gap: 9px; align-items: flex-end; }
  .row > * { min-width: 0; }
  .row .grow { flex: 1; }

  .kv { display: flex; gap: 8px; align-items: center; margin-bottom: 7px; font-size: 12.5px; }
  .kv > .k { color: var(--muted); min-width: 74px; flex-shrink: 0; }
  .kv > .v { font-family: var(--mono); font-size: 11.5px; word-break: break-all; }
  .kv > .v.grow { flex: 1; }

  pre {
    background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
    padding: 11px 13px; margin: 0; overflow-x: auto;
    font-family: var(--mono); font-size: 11.5px; line-height: 1.6;
  }
  .tabs { display: flex; gap: 4px; margin-bottom: 9px; flex-wrap: wrap; }
  .tabs button { padding: 4px 11px; font-size: 11.5px; }
  .tabs button.active { background: var(--accent-soft); border-color: var(--accent); color: var(--accent); font-weight: 600; }

  #logbox {
    height: 208px; overflow-y: auto; background: var(--bg);
    border: 1px solid var(--border); border-radius: 8px; padding: 9px 11px;
    font-family: var(--mono); font-size: 11.5px; line-height: 1.65;
  }
  #logbox div { white-space: pre-wrap; word-break: break-all; }
  #logbox .warn { color: var(--amber); }
  #logbox .err { color: var(--red); }
  #logbox .ok { color: var(--green); }
  #logbox .dim { color: var(--muted); }
  #logbox .lg-time {
    color: var(--muted); margin-right: 8px; cursor: default;
    font-variant-numeric: tabular-nums;
  }

  .toast {
    position: fixed; right: 22px; bottom: 22px; z-index: 50;
    display: flex; flex-direction: column; gap: 8px; align-items: flex-end;
  }
  .toast div {
    background: var(--card); border: 1px solid var(--border-strong); border-left: 3px solid var(--accent);
    border-radius: 8px; padding: 9px 14px; font-size: 12.5px; max-width: 380px;
    box-shadow: 0 6px 22px rgba(20,30,50,.14); animation: pop .18s ease-out;
  }
  .toast div.err { border-left-color: var(--red); }
  .toast div.ok { border-left-color: var(--green); }
  @keyframes pop { from { opacity: 0; transform: translateY(6px); } }

  .banner {
    display: none; align-items: center; gap: 10px; margin-bottom: 14px;
    background: var(--amber-bg); border: 1px solid var(--amber); color: var(--amber);
    border-radius: 10px; padding: 10px 15px; font-size: 12.5px;
  }
  .banner.show { display: flex; }
  .banner input { max-width: 260px; }
  .muted { color: var(--muted); }
  .empty { padding: 22px; text-align: center; color: var(--muted); }
  .spin { display: inline-block; animation: rot .9s linear infinite; }
  @keyframes rot { to { transform: rotate(360deg); } }
  .badge {
    display: inline-block; padding: 1px 6px; border-radius: 4px; font-size: 10.5px;
    background: var(--accent-soft); color: var(--accent); margin-right: 3px;
  }
  .notice {
    margin-bottom: 10px; background: var(--amber-bg); border: 1px solid var(--amber);
    color: var(--amber); border-radius: 10px; padding: 10px 15px; font-size: 12.5px; line-height: 1.7;
  }
  svg.spark { display: block; width: 100%; height: 34px; margin-top: 6px; }

  /* 可用模型：下拉多选 */
  .avail-pick { position: relative; }
  .avail-trigger {
    display: flex; align-items: center; justify-content: space-between; gap: 8px;
    width: 100%; padding: 8px 10px; text-align: left; cursor: pointer;
    font-family: inherit; font-size: 12.5px; color: var(--text);
    background: var(--card); border: 1px solid var(--border-strong); border-radius: 7px;
  }
  .avail-trigger:hover { border-color: var(--accent); }
  .avail-trigger .caret { color: var(--muted); font-size: 10px; transition: transform .15s; }
  .avail-pick.open .avail-trigger .caret { transform: rotate(180deg); }
  .avail-trigger-label {
    display: flex; align-items: center; gap: 5px; flex-wrap: wrap; min-width: 0;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .avail-chip {
    display: inline-block; max-width: 230px; overflow: hidden; text-overflow: ellipsis;
    padding: 1px 9px; border-radius: 20px; font-family: var(--mono); font-size: 11px;
    background: var(--accent-soft); color: var(--accent); white-space: nowrap;
  }
  .avail-drop {
    position: absolute; z-index: 60; top: calc(100% + 4px); left: 0; right: 0;
    background: var(--card); border: 1px solid var(--border-strong); border-radius: 8px;
    box-shadow: 0 10px 28px rgba(0,0,0,.28); padding: 8px;
  }
  .avail-list { max-height: 210px; overflow-y: auto; margin-top: 6px; }
  .avail-item {
    display: flex; align-items: center; gap: 8px; padding: 5px 7px;
    border-radius: 5px; cursor: pointer; font-family: var(--mono); font-size: 12px;
  }
  .avail-item:hover { background: var(--accent-soft); }
  .avail-item input { width: auto; margin: 0; flex-shrink: 0; }
  .avail-item.sel { color: var(--accent); }
  .avail-foot {
    display: flex; align-items: center; gap: 8px; margin-top: 8px;
    border-top: 1px solid var(--border); padding-top: 8px;
  }
</style>
</head>
<body>
<div class="wrap">

  <header>
    <div>
      <h1>多 Key 轮换控制台</h1>
      <div class="sub" id="subline">正在连接…</div>
    </div>
    <div class="spacer"></div>
    <button id="btn-pause">暂停接入</button>
    <button id="btn-refresh">刷新</button>
  </header>

  <div class="banner" id="authbar">
    <span>该网关启用了本地鉴权，请输入访问 Token：</span>
    <input id="token-input" class="mono" type="password" placeholder="Bearer Token">
    <button class="primary tiny" id="btn-token">确认</button>
  </div>

  <div class="grid kpis" id="kpis"></div>

  <div class="grid two" style="margin-bottom:14px">
    <div class="card">
      <h2>网关接入信息 <span class="spacer"></span><button class="tiny" id="btn-copy-base">复制地址</button></h2>
      <div id="gateway"></div>
      <div class="tabs" id="snippet-tabs" style="margin-top:12px"></div>
      <pre id="snippet"></pre>
    </div>

    <div class="card">
      <h2>默认模型 <span class="spacer"></span><button class="tiny" id="btn-models">刷新清单</button></h2>
      <label class="field">
        <span>当前生效模型</span>
        <select id="model-select"></select>
      </label>
      <div id="model-meta" style="font-size:12px;margin-bottom:6px"></div>
      <div id="model-count" class="muted" style="font-size:11.5px;margin-bottom:11px"></div>
      <div class="row">
        <button class="primary grow" id="btn-apply-model">应用为默认模型</button>
      </div>
      <div style="border-top:1px solid var(--border);padding-top:12px;margin-top:12px">
        <label class="field">
          <span>可用模型（留空 = 全部可用；选择后只有这些模型能被调用，网关会真实拦截清单外的请求）</span>
        </label>
        <div class="avail-pick" id="avail-pick">
          <button type="button" class="avail-trigger" id="avail-trigger">
            <span class="avail-trigger-label" id="avail-trigger-label">全部（不限）</span>
            <span class="caret">▾</span>
          </button>
          <div class="avail-drop" id="avail-drop" hidden>
            <input id="avail-search" type="text" placeholder="筛选模型…" autocomplete="off">
            <div class="avail-list" id="avail-list"></div>
            <div class="avail-foot">
              <span class="muted" id="avail-count" style="flex:1;font-size:11.5px"></span>
              <button class="tiny" id="btn-avail-all" type="button">全选</button>
              <button class="tiny" id="btn-avail-none" type="button">清空</button>
            </div>
          </div>
        </div>
        <div class="row" style="margin-top:8px">
          <button class="primary grow" id="btn-apply-available">应用可用模型</button>
        </div>
        <div class="muted" style="font-size:11.5px;margin-top:8px">
          模型名以「刷新清单」拉到的最新上游清单为准；未选中的模型不会被列入清单、点名调用返回 400。
        </div>
      </div>
      <div class="muted" style="font-size:11.5px;margin-top:11px;line-height:1.6">
        网关对上层完全透明：客户端请求里带 <code>model</code> 就用它的，
        没带则回落到这里的默认值。
      </div>
    </div>
  </div>

  <div class="card" style="margin-bottom:14px">
    <h2>Key 池 <span class="spacer"></span><span class="muted" id="pool-note" style="text-transform:none;letter-spacing:0"></span></h2>
    <div id="pool"></div>

    <div style="margin-top:15px;padding-top:14px;border-top:1px solid var(--border)">
      <h2 style="margin-bottom:10px">添加 Key</h2>
      <div class="row">
        <label class="field grow" style="margin-bottom:0">
          <span>API Key（支持一次粘贴多把，逗号或换行分隔）</span>
          <input id="new-key" class="mono" placeholder="sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" autocomplete="off">
        </label>
        <label class="field" style="width:150px;margin-bottom:0">
          <span>归属账号</span>
          <input id="new-account" placeholder="留空自动命名">
        </label>
        <label class="field" style="width:104px;margin-bottom:0">
          <span>并发上限</span>
          <input id="new-concurrency" type="number" min="1" max="256" value="16">
        </label>
        <button class="primary" id="btn-add" style="height:33px">添加</button>
      </div>
      <div class="muted" style="font-size:11.5px;margin-top:9px">
        添加前会先逐把校验凭据（失败的直接拒收，不污染池子）；成功后立即写入配置文件并参与轮换。
      </div>
    </div>
  </div>

  <div class="card" style="margin-bottom:14px">
    <h2>用户 <span class="spacer"></span><span class="muted" id="sk-note" style="text-transform:none;letter-spacing:0"></span></h2>
    <div id="sk-state"></div>
    <div id="sk-create" style="display:none">
      <div class="row" style="margin-bottom:10px">
        <label class="field grow" style="margin-bottom:0">
          <span>名称（必填，1-64 字符）</span>
          <input id="sk-name" class="mono" placeholder="例如：WorkBuddy-前端" autocomplete="off">
        </label>
        <label class="field grow" style="margin-bottom:0">
          <span>备注（可选）</span>
          <input id="sk-note-input" placeholder="给谁用、什么时候发的">
        </label>
        <button class="primary" id="btn-sk-create" style="height:33px">签发用户 Key</button>
      </div>
    </div>
    <div class="muted" style="font-size:11.5px;margin-bottom:10px;line-height:1.7">
      用户 Key 是网关签发的对外凭据：<b>只能调用 API（/v1/*）与健康检查，永远无法访问本管理页面</b>，
      也无法查看或改动 Key 池与配置。每次调用按用户 Key 独立累计输入 / 输出 token、缓存命中与成功失败，
      可直接作为计费或审计依据；Key 池里会显示每个用户当前正在占用哪把 Key。已停用的用户 Key 立即失效。
      完整密钥只在签发时显示一次。
    </div>
    <div id="sk-table-sec"></div>
    <div style="margin-top:16px;padding-top:14px;border-top:1px solid var(--border)">
      <h2 style="margin-bottom:10px">用户用量明细 <span class="muted" style="font-size:12px">（分页显示，每页 10 条，最新在前）</span></h2>
      <div id="sk-history"></div>
      <div id="sk-hist-bar"></div>
    </div>
  </div>

  <div class="grid two">
    <div class="card">
      <h2>运行参数</h2>
      <div class="row" style="margin-bottom:12px">
        <label class="field grow" style="margin-bottom:0">
          <span>调度策略</span>
          <select id="opt-strategy">
            <option value="round_robin">round_robin · 轮转（配额均摊最均匀）</option>
            <option value="least_inflight">least_inflight · 最少在途</option>
            <option value="least_recent">least_recent · 最久未用</option>
            <option value="weighted">weighted · 加权随机</option>
          </select>
        </label>
        <label class="field" style="width:132px;margin-bottom:0">
          <span>限速模式</span>
          <select id="opt-rate-mode">
            <option value="adaptive">adaptive · AIMD</option>
            <option value="fixed">fixed · 固定</option>
            <option value="off">off · 不限速</option>
          </select>
        </label>
        <label class="field" style="width:112px;margin-bottom:0">
          <span>目标 QPS</span>
          <input id="opt-qps" type="number" step="0.05" min="0.01">
        </label>
      </div>
      <div class="row" style="margin-bottom:12px">
        <label class="field grow" style="margin-bottom:0">
          <span>单请求等待预算（秒，0 = 不限）</span>
          <input id="opt-wait" type="number" step="5" min="0">
        </label>
        <label class="field" style="width:132px;margin-bottom:0">
          <span>最大重试次数</span>
          <input id="opt-attempts" type="number" min="1" max="50">
        </label>
        <button class="primary" id="btn-apply-options" style="height:33px">应用</button>
      </div>
      <div class="row" style="margin-bottom:12px;align-items:flex-end;flex-wrap:wrap">
        <label class="field" style="width:auto;margin-bottom:0;flex-direction:row;align-items:center;gap:6px">
          <input id="opt-fx-enabled" type="checkbox" style="width:auto;margin:0">
          <span style="font-weight:600">智能一换一</span>
        </label>
        <label class="field" style="width:82px;margin-bottom:0">
          <span>并发</span>
          <input id="opt-fx-concurrency" type="number" min="1" max="64">
        </label>
        <label class="field" style="width:96px;margin-bottom:0">
          <span>单轮上限</span>
          <input id="opt-fx-req" type="number" min="1" max="2048">
        </label>
        <label class="field" style="width:110px;margin-bottom:0">
          <span>触发间隔(秒)</span>
          <input id="opt-fx-interval" type="number" min="60" step="60">
        </label>
        <label class="field" style="width:130px;margin-bottom:0">
          <span>长文 max_tokens</span>
          <input id="opt-fx-tokens" type="number" min="128" max="16384" step="128">
        </label>
        <label class="field" style="width:104px;margin-bottom:0">
          <span>大图边长px</span>
          <input id="opt-fx-imgsize" type="number" min="256" max="2048" step="256">
        </label>
        <label class="field" style="width:104px;margin-bottom:0">
          <span>每请求图片数</span>
          <input id="opt-fx-imgcount" type="number" min="1" max="9">
        </label>
        <label class="field" style="width:auto;margin-bottom:0;flex-direction:row;align-items:center;gap:6px">
          <input id="opt-fx-image" type="checkbox" style="width:auto;margin:0">
          <span>带大图</span>
        </label>
        <label class="field" style="width:auto;margin-bottom:0;flex-direction:row;align-items:center;gap:6px">
          <input id="opt-fx-yield" type="checkbox" style="width:auto;margin:0">
          <span title="有用户请求在途时烧点主动让路，优先保证首字延迟">服务优先</span>
        </label>
        <label class="field" style="width:118px;margin-bottom:0">
          <span>内存下限(MB)</span>
          <input id="opt-fx-minmem" type="number" min="0" max="8192" step="50">
        </label>
      </div>
      <div class="muted" style="font-size:11.5px;margin-bottom:8px">
        智能一换一：账号因「套餐额度耗尽」冷却期间，自动用该账号持续调用推广池模型
        （sensenova-6.8-flash-lite，长文/多图交替），按官方活动「1 推广池积分换 1 通用池积分」加速回补；
        每轮结束探测一次通用池，回补成功立即复活账号收工；撞推广池窗口限流会自动放慢，耗尽才停。
      </div>
      <div class="muted" style="font-size:11.5px">
        AIMD 模式下「目标 QPS」是**起始速率**；工具会自己往上下界之间收敛，撞 429 就降、
        长时间干净就升。改参数会重置收敛点，从起始速率重新探测。
      </div>
    </div>

    <div class="card">
      <h2>实时日志 <span class="spacer"></span>
        <button class="tiny" id="btn-autoscroll">自动滚动：开</button>
        <button class="tiny" id="btn-clearlog">清屏</button>
      </h2>
      <div id="logbox"></div>
      <div class="muted" style="font-size:11.5px;margin-top:8px" id="logfile"></div>
    </div>
  </div>
</div>
<div class="toast" id="toast"></div>

<script>
"use strict";

var S = {
  token: localStorage.getItem("st_rotator_token") || localStorage.getItem("sn_rotator_token") || "",
  cursor: 0,
  autoscroll: true,
  tab: "python",
  models: [],
  timerState: null,
  timerLog: null
};

/* 窗口是带 #token=xxx 打开的（fragment 不会发给服务端，也不进 Referer）。
   读出来存进 localStorage，然后立刻从地址栏抹掉，避免残留在可见 URL 里。 */
(function readTokenFromHash() {
  var match = /(?:^|[#&])token=([^&]+)/.exec(location.hash || "");
  if (!match) return;
  try {
    S.token = decodeURIComponent(match[1]);
    localStorage.setItem("st_rotator_token", S.token);
  } catch (err) { /* 非法编码，忽略 */ }
  history.replaceState(null, "", location.pathname + location.search);
})();

/* ------------------------------------------------------------------ 工具 */

function $(id) { return document.getElementById(id); }

function esc(text) {
  return String(text == null ? "" : text)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function toast(message, kind) {
  var box = $("toast");
  var item = document.createElement("div");
  item.className = kind || "";
  item.textContent = message;
  box.appendChild(item);
  setTimeout(function () { item.remove(); }, kind === "err" ? 6000 : 3200);
}

function fmtInt(n) { return Number(n || 0).toLocaleString("en-US"); }

function fmtDuration(seconds) {
  seconds = Math.max(0, Math.floor(seconds || 0));
  var d = Math.floor(seconds / 86400), h = Math.floor(seconds % 86400 / 3600);
  var m = Math.floor(seconds % 3600 / 60), s = seconds % 60;
  if (d) return d + "天" + h + "小时";
  if (h) return h + "小时" + m + "分";
  if (m) return m + "分" + s + "秒";
  return s + "秒";
}

function fmtCtx(n) {
  if (!n) return "";
  if (n >= 1048576) return Math.round(n / 1048576 * 10) / 10 + "M";
  if (n >= 1024) return Math.round(n / 1024) + "K";
  return String(n);
}

/* ------------------------------------------------------------------ 请求 */

async function api(path, options) {
  options = options || {};
  var headers = Object.assign({ "Content-Type": "application/json" }, options.headers || {});
  if (S.token) headers["Authorization"] = "Bearer " + S.token;
  var response;
  try {
    response = await fetch(path, Object.assign({}, options, { headers: headers }));
  } catch (err) {
    throw new Error("网关无响应：" + err.message);
  }
  if (response.status === 401) {
    $("authbar").classList.add("show");
    throw new Error("需要 Token 才能访问控制台接口");
  }
  var data = {};
  try { data = await response.json(); } catch (err) { /* 空响应体 */ }
  if (!response.ok) {
    var detail = (data && data.error && (data.error.message || data.error)) || ("HTTP " + response.status);
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data;
}

/* ------------------------------------------------------------------ 渲染 */

function renderKpis(state) {
  var s = state.summary || {};
  var rate = state.rate_control || {};
  var m = state.metrics || {};
  var rateText, rateClass = "", rateSub = "";
  if (rate.mode === "adaptive") {
    rateText = (rate.rate || 0).toFixed(2);
    rateSub = "区间 " + rate.min_rate + "~" + rate.max_rate;
    if (rate.penalties > rate.raises * 1.6 && rate.penalties > 3) rateClass = "amber";
    else if (rate.penalties === 0) rateClass = "green";
  } else if (rate.mode === "fixed") {
    rateText = (rate.rate || 0).toFixed(2); rateSub = "固定速率";
  } else {
    rateText = "关"; rateSub = "不限速";
  }
  var attempts = m.upstream_attempts || 0;
  var amp = m.client_requests ? (attempts / m.client_requests) : 0;
  var ampText = "上游尝试 " + fmtInt(attempts) + " 次";
  if (amp > 1.05) ampText += "（放大 " + amp.toFixed(2) + "×）";
  var cards = [
    ["可用 Key", fmtInt(s.healthy), "green", "共 " + fmtInt(s.total) + " 把"],
    ["在途中", fmtInt(s.status_inflight != null ? s.status_inflight : s.inflight), (s.status_inflight || s.inflight) ? "blue" : "", "已占用、请求未返回"],
    ["冷却中", fmtInt(s.cooldown), s.cooldown ? "amber" : "", "等待恢复"],
    ["已失效", fmtInt(s.invalid), s.invalid ? "red" : "", "401/403 已隔离"],
    ["当前限速", rateText, rateClass, rateSub + " req/s"],
    ["已服务请求", fmtInt(m.client_requests), "", ampText],
    ["运行时长", fmtDuration(m.uptime_seconds), "", m.stream_requests ? "其中流式 " + fmtInt(m.stream_requests) : "进程已启动"]
  ];
  $("kpis").innerHTML = cards.map(function (c) {
    return '<div class="card kpi"><div class="label">' + esc(c[0]) + '</div>' +
      '<div class="value ' + c[2] + '">' + esc(c[1]) + '</div>' +
      '<div class="label" style="margin-top:2px">' + esc(c[3]) + '</div></div>';
  }).join("");
  if (rate.mode === "adaptive") renderSpark(rate);
}

function renderSpark(rate) {
  var history = (rate.history || []).filter(function (p) { return p && p.length === 2; });
  // 曲线要挂在"当前限速"卡片下（第 4 张），而不是第一张——它是速率的历史轨迹
  var host = $("kpis").children[3];
  if (!host || history.length < 2) return;
  var rates = history.map(function (p) { return p[1]; });
  var lo = Math.min.apply(null, rates), hi = Math.max.apply(null, rates);
  if (hi - lo < 1e-6) { hi = lo + 0.01; }
  var w = 100, h = 30;
  var points = history.map(function (p, i) {
    var x = i / (history.length - 1) * w;
    var y = h - (p[1] - lo) / (hi - lo) * (h - 4) - 2;
    return x.toFixed(2) + "," + y.toFixed(2);
  }).join(" ");
  var svg = '<svg class="spark" viewBox="0 0 100 30" preserveAspectRatio="none" ' +
    'title="AIMD 速率轨迹（' + history.length + ' 次调整）">' +
    '<polyline fill="none" stroke="var(--accent)" stroke-width="1.2" ' +
    'vector-effect="non-scaling-stroke" points="' + points + '"></polyline></svg>';
  var node = host.querySelector(".kpi-spark");
  if (!node) {
    node = document.createElement("div");
    node.className = "kpi-spark";
    host.appendChild(node);
  }
  node.innerHTML = svg;
}

function renderGateway(state) {
  var g = state.gateway || {};
  var tokenText = g.token ? g.token : "未设置（本机任意进程都可调用）";
  var rows = [
    ["Base URL", g.base_url],
    ["对话端点", g.chat_endpoint],
    ["API Key", tokenText],
    ["上游", g.upstream],
    ["健康检查", g.health_endpoint],
    ["运行统计", g.stats_endpoint]
  ];
  $("gateway").innerHTML = rows.map(function (r) {
    return '<div class="kv"><span class="k">' + esc(r[0]) + '</span>' +
      '<span class="v grow">' + esc(r[1]) + '</span></div>';
  }).join("");
  renderSnippet(g);
}

function renderSnippetTabs() {
  var tabs = [["python", "Python (OpenAI SDK)"], ["curl", "cURL"], ["node", "Node.js"], ["generic", "通用 Agent 配置"]];
  $("snippet-tabs").innerHTML = tabs.map(function (t) {
    return '<button data-tab="' + t[0] + '" class="' + (S.tab === t[0] ? "active" : "") + '">' + esc(t[1]) + '</button>';
  }).join("");
  Array.prototype.forEach.call($("snippet-tabs").children, function (button) {
    button.onclick = function () { S.tab = button.dataset.tab; renderSnippetTabs(); renderSnippet(lastGateway); };
  });
}

var lastGateway = {};

function renderSnippet(g) {
  if (g) lastGateway = g;
  g = lastGateway || {};
  var base = g.base_url || "http://127.0.0.1:8080/v1";
  var token = g.token || "sk-local-any";
  var model = g.model || "deepseek-v4-flash";
  var text;
  if (S.tab === "curl") {
    text = 'curl ' + base + '/chat/completions \\\n' +
      '  -H "Content-Type: application/json" \\\n' +
      '  -H "Authorization: Bearer ' + token + '" \\\n' +
      '  -d \'{\n' +
      '    "model": "' + model + '",\n' +
      '    "messages": [{"role": "user", "content": "你好"}],\n' +
      '    "stream": false\n' +
      '  }\'';
  } else if (S.tab === "node") {
    text = 'import OpenAI from "openai";\n\n' +
      'const client = new OpenAI({\n' +
      '  baseURL: "' + base + '",\n' +
      '  apiKey: "' + token + '",   // 网关本地鉴权，非商汤 Key\n' +
      '});\n\n' +
      'const stream = await client.chat.completions.create({\n' +
      '  model: "' + model + '",\n' +
      '  messages: [{ role: "user", content: "你好" }],\n' +
      '  stream: true,\n' +
      '});\n' +
      'for await (const chunk of stream) {\n' +
      '  process.stdout.write(chunk.choices[0]?.delta?.content ?? "");\n' +
      '}';
  } else if (S.tab === "generic") {
    text = '# 通用 OpenAI 兼容配置（WorkBuddy / Dify / Cherry Studio / LobeChat …）\n' +
      'base_url : ' + base + '\n' +
      'api_key  : ' + token + '\n' +
      'model    : ' + model + '\n' +
      'stream   : 支持（含 tool_calls 透传）\n\n' +
      '# 说明\n' +
      '#   - 上层的 api_key 填上面这个本地 Token，不是商汤的 sk- Key\n' +
      '#   - 商汤的多把 Key 全部由本网关内部轮换，上层无感知\n' +
      '#   - 429 / 冷却 / 坏 Key 隔离都在网关内消化';
  } else {
    text = 'from openai import OpenAI\n\n' +
      'client = OpenAI(\n' +
      '    base_url="' + base + '",\n' +
      '    api_key="' + token + '",   # 网关本地鉴权，非商汤 Key\n' +
      ')\n\n' +
      'stream = client.chat.completions.create(\n' +
      '    model="' + model + '",\n' +
      '    messages=[{"role": "user", "content": "你好"}],\n' +
      '    stream=True,\n' +
      ')\n' +
      'for chunk in stream:\n' +
      '    delta = chunk.choices[0].delta.content\n' +
      '    if delta:\n' +
      '        print(delta, end="", flush=True)';
  }
  $("snippet").textContent = text;
}

function renderModels(state) {
  var catalog = state.models || {};
  S.models = catalog.models || [];
  var select = $("model-select");
  var current = state.default_model;
  var options = S.models.map(function (m) { return m.id; });
  if (current && options.indexOf(current) === -1) options.unshift(current);
  if (!options.length) options = [current].filter(Boolean);
  select.innerHTML = options.map(function (id) {
    return '<option value="' + esc(id) + '"' + (id === current ? " selected" : "") + '>' + esc(id) + '</option>';
  }).join("") || '<option value="">（无可用模型）</option>';
  select.dataset.current = current || "";

  // 可用模型：下拉多选。勾选状态保存在 availSelected（周期刷新不打断用户操作）；
  // 下拉可选项必须是「完整上游清单」，不能只用 S.models——后端返回的 models 已按
  // 白名单过滤过，只用它会导致保存白名单后，未选中的模型从下拉里消失、想再勾选
  // 也无从下手。catalog.blocked 就是本次被白名单拦下的模型名，并回来即完整清单。
  // 已保存白名单里不在最新清单的模型保留为历史项，方便取消勾选。
  availIds = S.models.map(function (m) { return m.id; });
  (catalog.blocked || []).forEach(function (id) {
    if (availIds.indexOf(id) === -1) availIds.push(id);
  });
  (state.available_models || []).forEach(function (id) {
    if (availIds.indexOf(id) === -1) availIds.push(id);
  });
  if (availSelected === null || !availDirty) {
    availSelected = (state.available_models || []).slice();
  }
  renderAvail();

  // 注意：详情写在 #model-meta，条数写在 #model-count。两者必须分开——
  // 早先共用一个元素时，"共 N 个模型"会把模型详情覆盖掉，详情永远看不见。
  var count = $("model-count");
  if (catalog.error) {
    count.innerHTML = '<span style="color:var(--red)">拉取模型清单失败：' + esc(catalog.error) + '</span>';
  } else if (!S.models.length) {
    count.textContent = "暂无清单，点「刷新清单」从上游拉取。";
  } else {
    var blocked = catalog.blocked || [];
    // 条数用「白名单内 + 被拦截」= 完整上游清单，否则白名单生效时这里只报放行数，
    // 与下方下拉的「共 N 个模型」对不上。
    var parts = ["共 " + (S.models.length + blocked.length) + " 个模型" + (catalog.cached ? "（缓存）" : "（刚拉取）")];
    if (catalog.restricted) {
      parts.push("已启用可用模型白名单，拦截 " + blocked.length + " 个不可用模型"
        + (blocked.length ? "：" + blocked.join("、") : ""));
    }
    count.textContent = parts.join("，");
  }
  updateModelMeta();
}

function updateModelMeta() {
  var id = $("model-select").value;
  var found = S.models.filter(function (m) { return m.id === id; })[0];
  var node = $("model-meta");
  if (!found) { node.innerHTML = ""; return; }
  var bits = [];
  if (found.context_length) bits.push("上下文 " + fmtCtx(found.context_length));
  if (found.max_output_length) bits.push("最大输出 " + fmtCtx(found.max_output_length));
  if ((found.input_modalities || []).length) {
    bits.push("输入 " + found.input_modalities.join("+") + " → 输出 " + (found.output_modalities || []).join("+"));
  }
  var badges = (found.features || []).map(function (f) { return '<span class="badge">' + esc(f) + '</span>'; }).join("");
  node.innerHTML = '<span class="muted">' + esc(bits.join(" · ")) + "</span>" +
    (badges ? "<div style='margin-top:5px'>" + badges + "</div>" : "");
}

function renderPool(state) {
  var keys = state.keys || [];
  if (!keys.length) {
    $("pool").innerHTML = '<div class="empty">池里还没有 Key。在下面添加至少一把才能对外提供服务。</div>';
  } else {
    var head = "<tr><th>账号</th><th>Key</th><th>状态</th><th>冷却</th><th>RPM</th>" +
      "<th class='num'>成功/失败</th><th class='num'>429</th><th class='num'>延迟</th><th></th></tr>";
    var body = keys.map(function (k) {
      var st = k.stats || {};
      var statusText = { healthy: "可用", inflight: "在途中", cooldown: "冷却中", invalid: "已失效" }[k.status] || k.status;
      // 状态后面跟"(用户名)"：在途中显示正在占用这把 Key 的调用方（并发时可能不止一个），
      // 冷却中显示把它打进冷却的那一位。没有归属信息（后台任务、管理端旧请求）就不显示。
      var users = (k.inflight_users || []).filter(Boolean);
      var who = "";
      if (users.length) {
        var uniq = [];
        users.forEach(function (u) { if (uniq.indexOf(u) < 0) uniq.push(u); });
        who = uniq.join("、") + (users.length > uniq.length ? " ×" + users.length : "");
      } else if (k.status === "cooldown" && k.last_user) {
        who = k.last_user;
      }
      var cooldown = k.cooldown_remaining > 0 ? k.cooldown_remaining + "s" : "—";
      return "<tr>" +
        "<td>" + esc(k.account) + "</td>" +
        '<td class="mono">' + esc(k.key) + "</td>" +
        '<td style="white-space:nowrap"><span class="pill ' + esc(k.status) + '">' + esc(statusText) + "</span>" +
          (who ? '<span class="muted" style="font-size:11px"> (' + esc(who) + ")</span>" : "") + "</td>" +
        '<td class="num">' + esc(cooldown) + "</td>" +
        '<td class="mono">' + esc(k.rpm_window) + "</td>" +
        '<td class="num">' + fmtInt(st.successes) + " / " + fmtInt(st.failures) + "</td>" +
        '<td class="num">' + fmtInt(st.rate_limited) + "</td>" +
        '<td class="num">' + (st.avg_latency_ms ? Math.round(st.avg_latency_ms) + "ms" : "—") + "</td>" +
        '<td style="text-align:right;white-space:nowrap">' +
          '<button class="tiny" data-act="verify" data-id="' + esc(k.id) + '" data-label="' + esc(k.account + " / " + k.key) + '">测试</button> ' +
          '<button class="tiny danger" data-act="remove" data-id="' + esc(k.id) + '" data-label="' + esc(k.account + " / " + k.key) + '">删除</button>' +
        "</td></tr>";
    }).join("");
    $("pool").innerHTML = "<table>" + head + body + "</table>";
    Array.prototype.forEach.call($("pool").querySelectorAll("button[data-act]"), function (button) {
      button.onclick = function () {
        onPoolAction(button.dataset.act, button.dataset.id, button.dataset.label, button);
      };
    });
  }
  var s = state.summary || {};
  // 右上角「正在尝试用户」：以 state.trying_users 为准 —— 它是"请求还在跑"的语义，
  // 包含轮换退避、等待可用 Key 这些**池里没有 inflight** 的空窗期，所以轮换时也能看到。
  // 回退到 keys[].inflight_users 汇总，兼容旧后端（只报"此刻正握着 Key"）。
  var activeNames = [];
  var trying = state.trying_users;
  if (trying && trying.length) {
    trying.forEach(function (u) { if (u && activeNames.indexOf(u) < 0) activeNames.push(u); });
  } else {
    keys.forEach(function (k) {
      (k.inflight_users || []).forEach(function (u) {
        if (u && activeNames.indexOf(u) < 0) activeNames.push(u);
      });
    });
  }
  var noteText = s.total ? ("在途 " + s.inflight + " / 共 " + s.total + " 把") : "";
  if (activeNames.length) {
    // 文案对齐用户预期：「用户1，用户2正在尝试」
    noteText += (noteText ? " · " : "") + activeNames.join("，") + "正在尝试";
  }
  $("pool-note").textContent = noteText;
}

function renderOptions(state) {
  var opt = state.options || {};
  var strategy = $("opt-strategy");
  if (document.activeElement !== strategy) strategy.value = opt.strategy || "round_robin";
  var mode = $("opt-rate-mode");
  if (document.activeElement !== mode) mode.value = opt.rate_mode || "off";
  var qps = $("opt-qps");
  if (document.activeElement !== qps) qps.value = opt.qps;
  var wait = $("opt-wait");
  if (document.activeElement !== wait) wait.value = opt.max_total_wait;
  var attempts = $("opt-attempts");
  if (document.activeElement !== attempts) attempts.value = opt.max_attempts;
  var fx = opt.flash_lite || {};
  var fe = $("opt-fx-enabled");
  if (document.activeElement !== fe) fe.checked = !!fx.enabled;
  var fi = $("opt-fx-image");
  if (document.activeElement !== fi) fi.checked = !!fx.image_enabled;
  var fy = $("opt-fx-yield");
  if (document.activeElement !== fy) fy.checked = fx.yield_to_serve !== false;
  var fm = $("opt-fx-minmem");
  if (document.activeElement !== fm && fx.min_available_mb != null) fm.value = fx.min_available_mb;
  [["opt-fx-concurrency", "concurrency"], ["opt-fx-req", "requests_per_trigger"],
   ["opt-fx-interval", "min_interval_s"], ["opt-fx-tokens", "long_text_max_tokens"],
   ["opt-fx-imgsize", "image_size"], ["opt-fx-imgcount", "multi_image_count"]].forEach(function (p) {
    var el = $(p[0]);
    if (document.activeElement !== el && fx[p[1]] != null) el.value = fx[p[1]];
  });
}

/* ------------------------------------------------------------------ 日志 */

function classifyLog(text) {
  if (/\[警告\]|WARNING|429/.test(text)) return "warn";
  if (/\[错误\]|ERROR|失败|Traceback/.test(text)) return "err";
  if (/成功|已启动|ok/i.test(text)) return "ok";
  return "";
}

function appendLogs(items) {
  var box = $("logbox");
  items.forEach(function (item) {
    var line = document.createElement("div");
    line.className = classifyLog(item.text);
    var time = document.createElement("span");
    time.className = "lg-time";
    time.textContent = item.t ? fmtTime(Number(item.t)) : "";
    time.title = item.t ? fmtTime(Number(item.t)) : "";
    line.appendChild(time);
    var text = document.createTextNode(item.text);
    line.appendChild(text);
    box.appendChild(line);
  });
  while (box.childElementCount > 800) box.removeChild(box.firstChild);
  if (S.autoscroll) box.scrollTop = box.scrollHeight;
}

async function pollLogs() {
  try {
    var data = await api("/api/logs?cursor=" + S.cursor);
    S.cursor = data.cursor;
    if (data.items && data.items.length) appendLogs(data.items);
  } catch (err) { /* 静默：日志轮询失败不该刷屏 */ }
}

/* ------------------------------------------------------------------ 交互 */

async function onPoolAction(action, keyId, label, button) {
  if (action === "remove") {
    if (!confirm("确定要从池中删除并写回配置文件吗？\n\n" + label)) return;
  }
  button.disabled = true;
  var original = button.textContent;
  button.innerHTML = '<span class="spin">◌</span>';
  try {
    var result = await api("/api/keys/" + action, {
      method: "POST",
      body: JSON.stringify({ id: keyId })
    });
    toast(result.message || "操作完成", result.ok === false ? "err" : "ok");
    await refreshState();
  } catch (err) {
    toast(err.message, "err");
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

async function onAddKeys() {
  var raw = $("new-key").value.trim();
  if (!raw) { toast("请先填入 API Key", "err"); return; }
  var button = $("btn-add");
  button.disabled = true;
  button.innerHTML = '<span class="spin">◌</span> 校验中…';
  try {
    var result = await api("/api/keys/add", {
      method: "POST",
      body: JSON.stringify({
        keys: raw,
        account: $("new-account").value.trim(),
        max_concurrency: parseInt($("new-concurrency").value, 10) || 16
      })
    });
    var msg = "已添加 " + result.added.length + " 把 Key";
    if (result.rejected && result.rejected.length) msg += "；" + result.rejected.length + " 把被拒收";
    toast(msg, result.added.length ? "ok" : "err");
    if (result.rejected && result.rejected.length) {
      result.rejected.forEach(function (r) { toast("拒收 " + r.key + "：" + r.reason, "err"); });
    }
    if (result.added.length) { $("new-key").value = ""; }
    await refreshState();
  } catch (err) {
    toast(err.message, "err");
  } finally {
    button.disabled = false;
    button.textContent = "添加";
  }
}

async function onApplyModel() {
  var model = $("model-select").value;
  if (!model) return;
  try {
    var result = await api("/api/model", { method: "POST", body: JSON.stringify({ model: model }) });
    toast(result.message, "ok");
    await refreshState();
  } catch (err) { toast(err.message, "err"); }
}

var availSelected = null; // 可用模型下拉多选：当前勾选（null = 还没同步过服务器白名单）
var availDirty = false;   // 用户改过勾选但还没点「应用」
var availIds = [];        // 下拉可选项：最新模型清单 + 已保存白名单里的历史项

async function onApplyAvailable() {
  var models = (availSelected || []).slice();
  try {
    var result = await api("/api/model/available", {
      method: "POST",
      body: JSON.stringify({ models: models })
    });
    toast(result.message, "ok");
    availDirty = false;
    await refreshState();
  } catch (err) { toast(err.message, "err"); }
}

/* ---------------------------------------------- 可用模型：下拉多选渲染 */
function renderAvail() { renderAvailTrigger(); renderAvailList(); }

function renderAvailTrigger() {
  var label = $("avail-trigger-label");
  var sel = availSelected || [];
  if (!sel.length) { label.textContent = "全部（不限）"; return; }
  var shown = sel.length;
  var rest = "";
  if (sel.length > 3) { shown = 3; rest = " +" + (sel.length - 3); }
  var chips = [];
  for (var i = 0; i < shown; i++) {
    chips.push('<span class="avail-chip" title="' + esc(sel[i]) + '">' + esc(sel[i]) + "</span>");
  }
  label.innerHTML = chips.join("") + (rest ? '<span class="muted" style="font-size:11px">' + rest + "</span>" : "");
}

function renderAvailList() {
  var list = $("avail-list");
  var q = ($("avail-search").value || "").trim().toLowerCase();
  var sel = availSelected || [];
  var frag = [];
  availIds.forEach(function (id) {
    if (q && id.toLowerCase().indexOf(q) === -1) return;
    var on = sel.indexOf(id) !== -1;
    frag.push('<label class="avail-item' + (on ? " sel" : "") + '" data-id="' + esc(id) + '">'
      + '<input type="checkbox"' + (on ? " checked" : "") + '>'
      + '<span style="overflow:hidden;text-overflow:ellipsis">' + esc(id) + "</span></label>");
  });
  list.innerHTML = frag.join("") || '<div class="muted" style="font-size:12px;padding:6px">没有匹配的模型</div>';
  $("avail-count").textContent = "已选 " + sel.length + " 项 / 共 " + availIds.length + " 个模型" + (q ? "（筛选）" : "");
  Array.prototype.forEach.call(list.querySelectorAll(".avail-item"), function (item) {
    item.addEventListener("change", syncAvailFromDom);
  });
}

function syncAvailFromDom() {
  availSelected = [];
  Array.prototype.forEach.call($("avail-list").querySelectorAll(".avail-item"), function (item) {
    var cb = item.querySelector("input");
    item.classList.toggle("sel", cb.checked);
    if (cb.checked) availSelected.push(item.getAttribute("data-id"));
  });
  availDirty = true;
  renderAvailTrigger();
  $("avail-count").textContent = "已选 " + availSelected.length + " 项 / 共 " + availIds.length + " 个模型";
}

async function onApplyOptions() {
  var body = {
    strategy: $("opt-strategy").value,
    rate_mode: $("opt-rate-mode").value,
    qps: parseFloat($("opt-qps").value),
    max_total_wait: parseFloat($("opt-wait").value),
    max_attempts: parseInt($("opt-attempts").value, 10),
    flash_lite: {
      enabled: $("opt-fx-enabled").checked,
      image_enabled: $("opt-fx-image").checked,
      yield_to_serve: $("opt-fx-yield").checked,
      min_available_mb: parseFloat($("opt-fx-minmem").value),
      concurrency: parseInt($("opt-fx-concurrency").value, 10),
      requests_per_trigger: parseInt($("opt-fx-req").value, 10),
      min_interval_s: parseFloat($("opt-fx-interval").value),
      long_text_max_tokens: parseInt($("opt-fx-tokens").value, 10),
      image_size: parseInt($("opt-fx-imgsize").value, 10),
      multi_image_count: parseInt($("opt-fx-imgcount").value, 10)
    }
  };
  try {
    var result = await api("/api/options", { method: "POST", body: JSON.stringify(body) });
    toast(result.message, "ok");
    await refreshState();
  } catch (err) { toast(err.message, "err"); }
}

async function onTogglePause() {
  var next = !(lastState.metrics && lastState.metrics.paused);
  try {
    var result = await api("/api/pause", { method: "POST", body: JSON.stringify({ paused: next }) });
    toast(result.message, "ok");
    await refreshState();
  } catch (err) { toast(err.message, "err"); }
}

function copyText(text, label) {
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(function () { toast("已复制" + label, "ok"); },
      function () { toast("复制失败", "err"); });
    return;
  }
  var area = document.createElement("textarea");
  area.value = text;
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  try { document.execCommand("copy"); toast("已复制" + label, "ok"); }
  catch (err) { toast("复制失败", "err"); }
  area.remove();
}

/* ------------------------------------------------------------------ 用户 */

var skRevealed = {};

function fmtTime(value) {
  if (!value) return "—";
  var d = new Date(Number(value) * 1000);
  if (isNaN(d.getTime())) return "—";
  function p(n) { return (n < 10 ? "0" : "") + n; }
  return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) +
    " " + p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
}

function renderSubkeys(state) {
  var sk = state.subkeys;
  var note = $("sk-note");
  var createBox = $("sk-create");
  var stateBox = $("sk-state");
  var tableBox = $("sk-table-sec");
  var histBar = $("sk-hist-bar");
  var historyBox = $("sk-history");
  if (!sk) {
    note.textContent = "";
    createBox.style.display = "none";
    stateBox.innerHTML = "";
    tableBox.innerHTML = '<div class="empty">用户功能未启用（当前进程未挂载 subkeys 管理器，可能是旧版本或直接调用代理）。</div>';
    histBar.innerHTML = "";
    historyBox.innerHTML = "";
    return;
  }
  createBox.style.display = "block";
  var warns = [];
  if (!sk.admin_token_set) {
    warns.push("未设置管理 Token（--token）：本管理页面与 /api/* 接口当前无需凭据即可访问。" +
      "启用用户功能后请务必设置管理 Token，否则任何知道地址的人都能绕过用户 Key 直接调 API 或改配置。");
  }
  stateBox.innerHTML = warns.map(function (w) {
    return '<div class="notice">' + esc(w) + "</div>";
  }).join("");
  var keys = sk.keys || [];
  note.textContent = keys.length + " 个 · " + keys.filter(function (k) { return k.enabled; }).length + " 启用";
  if (!keys.length) {
    tableBox.innerHTML = '<div class="empty">还没有用户。填好名称点「签发用户 Key」即可生成一把对外凭据。</div>';
  } else {
    var head = "<tr><th>名称</th><th>Key</th><th>状态</th><th>创建时间</th>" +
      "<th class='num'>请求</th><th class='num'>成功</th><th class='num'>失败</th>" +
      "<th class='num'>输入</th><th class='num'>输出</th><th class='num'>缓存命中率</th>" +
      "<th>最近使用</th><th></th></tr>";
    var body = keys.map(function (k) {
      var u = k.usage || {};
      var last = u.last_used_at ? fmtTime(u.last_used_at) : "—";
      var rate = u.prompt_tokens > 0 ? Math.round((u.cached_tokens || 0) / u.prompt_tokens * 100) + "%" : "—";
      var shown = skRevealed[k.id] ? k.key : k.masked;
      var keyCell = '<span class="mono">' + esc(shown) + "</span> " +
        '<button class="tiny" data-sk="reveal" data-id="' + esc(k.id) + '">' + (skRevealed[k.id] ? "隐藏" : "显示") + "</button> " +
        '<button class="tiny" data-sk="copy" data-id="' + esc(k.id) + '">复制</button>';
      return "<tr>" +
        '<td><b>' + esc(k.name) + "</b>" + (k.note ? '<div class="muted" style="font-size:11px">' + esc(k.note) + "</div>" : "") + "</td>" +
        "<td>" + keyCell + "</td>" +
        '<td style="white-space:nowrap"><span class="pill ' + (k.enabled ? "healthy" : "neutral") + '">' + (k.enabled ? "启用" : "停用") + "</span> " +
          '<button class="tiny" data-sk="toggle" data-id="' + esc(k.id) + '" data-enabled="' + (k.enabled ? 1 : 0) + '">' + (k.enabled ? "停用" : "启用") + "</button></td>" +
        "<td>" + esc(k.created_at || "—") + "</td>" +
        '<td class="num">' + fmtInt(u.requests) + "</td>" +
        '<td class="num">' + fmtInt(u.successes) + "</td>" +
        '<td class="num">' + fmtInt(u.errors) + "</td>" +
        '<td class="num">' + fmtCtx(u.prompt_tokens) + "</td>" +
        '<td class="num">' + fmtCtx(u.completion_tokens) + "</td>" +
        '<td class="num"' + (rate !== "—" ? ' title="缓存 ' + fmtInt(u.cached_tokens || 0) + " / 输入 " + fmtInt(u.prompt_tokens || 0) + '"' : "") + ">" + rate + "</td>" +
        "<td>" + esc(last) + "</td>" +
        '<td style="text-align:right;white-space:nowrap">' +
          '<button class="tiny" data-sk="rename" data-id="' + esc(k.id) + '" data-name="' + esc(k.name) + '">改名</button> ' +
          '<button class="tiny" data-sk="reset" data-id="' + esc(k.id) + '" data-name="' + esc(k.name) + '">清零</button> ' +
          '<button class="tiny danger" data-sk="revoke" data-id="' + esc(k.id) + '" data-name="' + esc(k.name) + '">删除</button>' +
        "</td></tr>";
    }).join("");
    tableBox.innerHTML = "<table>" + head + body + "</table>";
    Array.prototype.forEach.call(tableBox.querySelectorAll("button[data-sk]"), function (button) {
      button.onclick = function () { onSubkeyAction(button.dataset.sk, button); };
    });
  }
}

/* ------------------------------------------------- 用量明细（分页，默认每页 10 条） */

var skHist = { page: 1, pageSize: 10, total: 0, loading: false, pending: 0 };

async function loadSubkeyHistory(page) {
  if (skHist.loading) {
    if (page) skHist.pending = page;
    return;
  }
  skHist.loading = true;
  var bar = $("sk-hist-bar");
  var box = $("sk-history");
  try {
    var data = await api(
      "/api/subkeys/history?page=" + (page || skHist.page) + "&page_size=" + skHist.pageSize
    );
    skHist.page = data.page;
    skHist.total = data.total;
    renderSubkeyHistory(data, bar, box);
  } catch (err) {
    bar.innerHTML = "";
    box.innerHTML = '<div class="empty">' + esc(err.message) + "</div>";
  } finally {
    skHist.loading = false;
    if (skHist.pending) {
      var target = skHist.pending;
      skHist.pending = 0;
      loadSubkeyHistory(target);
    }
  }
}

function renderSubkeyHistory(data, bar, box) {
  var history = data.history || [];
  var pages = Math.max(1, Math.ceil(data.total / data.page_size));
  bar.innerHTML =
    '<div class="row" style="margin-top:8px;align-items:center">' +
      '<button class="tiny" id="sk-page-prev"' + (data.page <= 1 ? " disabled" : "") + ">上一页</button>" +
      '<span class="muted" style="margin:0 10px">第 ' + data.page + " / " + pages + " 页 · 共 " + fmtInt(data.total) + " 条</span>" +
      '<button class="tiny" id="sk-page-next"' + (data.page >= pages ? " disabled" : "") + ">下一页</button>" +
    "</div>";
  var prev = $("sk-page-prev");
  var next = $("sk-page-next");
  if (prev) prev.onclick = function () { loadSubkeyHistory(skHist.page - 1); };
  if (next) next.onclick = function () { loadSubkeyHistory(skHist.page + 1); };
  if (!history.length) {
    box.innerHTML = '<div class="empty">' +
      (data.total ? "本页暂无记录。" : "暂无调用记录（用户 Key 尚未被使用）。") + "</div>";
    return;
  }
  var hhead = "<tr><th>时间</th><th>用户</th><th>模型</th><th>模式</th><th>结果</th>" +
    "<th class='num'>输入</th><th class='num'>输出</th><th class='num'>缓存命中率</th><th class='num'>延迟</th></tr>";
  var hbody = history.map(function (h) {
    var mode = h.stream ? "流式" : "普通";
    var okBadge = h.ok
      ? '<span class="pill healthy">成功</span>'
      : '<span class="pill invalid">失败</span>';
    var estimate = h.estimated ? ' <span class="muted" style="font-size:10.5px">估</span>' : "";
    var rate = h.estimated || h.prompt <= 0 ? "—" : Math.round((h.cached || 0) / h.prompt * 100) + "%";
    var rateCell = '<td class="num"' + (rate !== "—" ? ' title="缓存 ' + fmtInt(h.cached || 0) + " / 输入 " + fmtInt(h.prompt) + '"' : "") + ">" + rate + "</td>";
    return "<tr>" +
      "<td>" + esc(fmtTime(h.t)) + "</td>" +
      "<td>" + esc(h.name || "—") + "</td>" +
      "<td>" + esc(h.model || "—") + "</td>" +
      "<td>" + mode + "</td>" +
      "<td>" + okBadge + "</td>" +
      '<td class="num">' + fmtCtx(h.prompt) + estimate + "</td>" +
      '<td class="num">' + fmtCtx(h.completion) + "</td>" +
      rateCell +
      '<td class="num">' + (h.latency_ms != null ? Math.round(h.latency_ms) + "ms" : "—") + "</td></tr>";
  }).join("");
  box.innerHTML = "<table>" + hhead + hbody + "</table>";
}

async function postSubkey(path, body, button) {
  button.disabled = true;
  var original = button.innerHTML || button.textContent;
  button.innerHTML = '<span class="spin">◌</span>';
  try {
    var result = await api(path, { method: "POST", body: JSON.stringify(body) });
    toast(result.message || "操作完成", result.ok === false ? "err" : "ok");
    await refreshState();
  } catch (err) { toast(err.message, "err"); }
  finally { button.disabled = false; button.innerHTML = original; }
}

async function onSubkeyCreate() {
  var name = $("sk-name").value.trim();
  if (!name) { toast("请先填用户名称", "err"); return; }
  var button = $("btn-sk-create");
  button.disabled = true;
  var original = button.textContent;
  button.innerHTML = '<span class="spin">◌</span> 签发中…';
  try {
    var result = await api("/api/subkeys/create", {
      method: "POST",
      body: JSON.stringify({ name: name, note: $("sk-note-input").value.trim() })
    });
    toast(result.message || "已创建", "ok");
    $("sk-name").value = "";
    $("sk-note-input").value = "";
    if (result.key) copyText(result.key, "用户 Key（完整密钥只显示这一次）");
    await refreshState();
  } catch (err) { toast(err.message, "err"); }
  finally { button.disabled = false; button.textContent = original; }
}

async function onSubkeyAction(action, button) {
  var id = button.dataset.id, name = button.dataset.name || "";
  if (action === "toggle") {
    var enable = !(button.dataset.enabled === "1");
    await postSubkey("/api/subkeys/toggle", { id: id, enabled: enable }, button);
    return;
  }
  if (action === "rename") {
    var next = prompt("新的名称（1-64 字符）：", name);
    if (next === null) return;
    next = next.trim();
    if (!next) { toast("名称不能为空", "err"); return; }
    await postSubkey("/api/subkeys/update", { id: id, name: next }, button);
    return;
  }
  if (action === "reset") {
    if (!confirm("确定清零用户「" + name + "」的用量统计吗？")) return;
    await postSubkey("/api/subkeys/reset", { id: id }, button);
    return;
  }
  if (action === "revoke") {
    if (!confirm("确定删除用户「" + name + "」吗？该 Key 立即失效，历史用量明细仍然保留。")) return;
    await postSubkey("/api/subkeys/revoke", { id: id }, button);
    return;
  }
  if (action === "reveal") {
    skRevealed[id] = !skRevealed[id];
    refreshState();
    return;
  }
  if (action === "copy") {
    var sk = lastState.subkeys || {};
    var found = (sk.keys || []).filter(function (k) { return k.id === id; })[0];
    copyText(found ? found.key : "", "用户 Key");
    return;
  }
}

/* ------------------------------------------------------------------ 主循环 */

var lastState = {};

async function refreshState() {
  try {
    var state = await api("/api/state");
    lastState = state;
    $("authbar").classList.remove("show");
    renderKpis(state);
    renderGateway(state);
    renderModels(state);
    renderPool(state);
    renderOptions(state);
    renderSubkeys(state);
    if (state.subkeys) loadSubkeyHistory(skHist.page);
    var g = state.gateway || {};
    $("subline").innerHTML = '<span class="dot ' + (state.metrics.paused ? "paused" : "on") + '"></span>' +
      (state.metrics.paused ? "已暂停接入（上层会收到 503）" : "运行中") +
      " · 监听 " + esc(g.listen) + " · " + fmtInt(state.summary.total) + " 把 Key · v" + esc(state.version);
    $("btn-pause").textContent = state.metrics.paused ? "恢复接入" : "暂停接入";
    if (state.log_file) $("logfile").textContent = "日志文件：" + state.log_file;
    else $("logfile").textContent = "未启用文件日志（仅内存缓冲）";
  } catch (err) {
    $("subline").innerHTML = '<span class="dot" style="background:var(--red)"></span>连接失败：' + esc(err.message);
  }
}

function bind() {
  $("btn-refresh").onclick = function () { refreshState(); toast("已刷新"); };
  $("btn-pause").onclick = onTogglePause;
  $("btn-add").onclick = onAddKeys;
  $("btn-sk-create").onclick = onSubkeyCreate;
  $("sk-name").addEventListener("keydown", function (event) { if (event.key === "Enter") onSubkeyCreate(); });
  $("btn-apply-model").onclick = onApplyModel;
  $("btn-apply-available").onclick = onApplyAvailable;
  $("avail-trigger").onclick = function (ev) {
    ev.stopPropagation();
    var open = $("avail-pick").classList.toggle("open");
    $("avail-drop").hidden = !open;
    if (open) $("avail-search").focus();
  };
  $("avail-search").addEventListener("input", function () { renderAvailList(); });
  $("avail-search").addEventListener("keydown", function (ev) {
    if (ev.key === "Escape") { $("avail-pick").classList.remove("open"); $("avail-drop").hidden = true; }
  });
  $("btn-avail-all").onclick = function (ev) {
    ev.stopPropagation();
    availSelected = availIds.slice();
    availDirty = true;
    renderAvail();
  };
  $("btn-avail-none").onclick = function (ev) {
    ev.stopPropagation();
    availSelected = [];
    availDirty = true;
    renderAvail();
  };
  document.addEventListener("click", function (ev) {
    var pick = $("avail-pick");
    if (!pick.classList.contains("open")) return;
    if (!pick.contains(ev.target)) {
      pick.classList.remove("open");
      $("avail-drop").hidden = true;
    }
  });
  $("btn-apply-options").onclick = onApplyOptions;
  $("btn-models").onclick = async function () {
    try { await api("/api/models/refresh", { method: "POST" }); await refreshState(); toast("模型清单已刷新", "ok"); }
    catch (err) { toast(err.message, "err"); }
  };
  $("btn-copy-base").onclick = function () { copyText((lastGateway.base_url || ""), "网关地址"); };
  $("model-select").onchange = updateModelMeta;
  $("new-key").addEventListener("keydown", function (event) { if (event.key === "Enter") onAddKeys(); });
  $("btn-clearlog").onclick = function () { $("logbox").innerHTML = ""; };
  $("btn-autoscroll").onclick = function () {
    S.autoscroll = !S.autoscroll;
    this.textContent = "自动滚动：" + (S.autoscroll ? "开" : "关");
  };
  $("btn-token").onclick = async function () {
    S.token = $("token-input").value.trim();
    localStorage.setItem("st_rotator_token", S.token);
    await refreshState();
  };
  $("token-input").addEventListener("keydown", function (event) {
    if (event.key === "Enter") $("btn-token").click();
  });
  renderSnippetTabs();
}

bind();
refreshState();
pollLogs();
S.timerState = setInterval(refreshState, 2000);
S.timerLog = setInterval(pollLogs, 1200);
</script>
</body>
</html>
"""
