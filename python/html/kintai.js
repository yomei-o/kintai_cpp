// 勤怠のページ（clock / month / requests）共通の小道具
"use strict";

const $ = (id) => document.getElementById(id);
const PARAMS = new URLSearchParams(location.search);

// index.html と同じ場所にあるログインのトークン
const TOKEN_KEY = "access_token";
function loginToken() {
  try { return localStorage.getItem(TOKEN_KEY) || ""; } catch (e) { return ""; }
}

// サーバが止まっていた・落ちた（502 など）ときも例外にせず fail の形で返す
async function api(path, body) {
  const h = { "Content-Type": "application/json" };
  const t = loginToken();
  if (t) h.Authorization = "Bearer " + t;
  try {
    const res = await fetch("/api/kintai/v0/" + path, { method: "POST", headers: h, body: JSON.stringify(body || {}) });
    if (!res.ok) return { status: "fail", message: "http " + res.status };
    return await res.json();
  } catch (e) {
    return { status: "fail", message: "network" };
  }
}

const MESSAGES = {
  "unknown tenant": "お店が見つかりません（URL を確かめてください）",
  "not logged in": "ログインしていません",
  "tenant member only": "このお店のメンバーではありません",
  "tenant admin only": "お店の管理者だけができます",
  "invalid kind": "出勤か退勤を選んでください",
  "invalid month": "月の指定が正しくありません",
  "invalid action": "修正の種類を選んでください",
  "invalid reason": "理由を書いてください（200 バイトまで）",
  "invalid time": "時刻が正しくありません（1 年以内で、先の時刻は入れられません）",
  "unknown punch": "その打刻が見つかりません（もう直されたかもしれません）",
  "already requested": "その打刻にはもう修正リクエストを出しています",
  "unknown request": "そのリクエストが見つかりません",
  "already decided": "そのリクエストはもう承認・却下・取り下げ済みです",
  "punch not found": "直す元の打刻がもうありません。却下してください",
  "network": "通信できません。電波の良いところでもう一度",
};
function msgOf(r) {
  const m = r && r.message;
  if (MESSAGES[m]) return MESSAGES[m];
  if (m && m.startsWith("http ")) return "サーバが起動中か混み合っています。少し待ってもう一度（" + m + "）";
  return m || "うまくいきませんでした";
}

function showMsg(id, text, kind) {
  const e = $(id);
  e.textContent = text || "";
  e.className = "msg" + (kind ? " " + kind : "");
}

function el(tag, attrs, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") e.className = v;
    else if (k === "onclick") e.onclick = v;
    else if (k === "text") e.textContent = v;
    else e.setAttribute(k, v);
  }
  for (const c of children) if (c != null) e.append(c);
  return e;
}

// 中身が変わったときだけ描き直す（ボタンを押そうとした瞬間に消えないように）
function render(box, key, build) {
  if (box.dataset.key === key) return;
  box.dataset.key = key;
  box.innerHTML = "";
  build();
}

// サーバの時刻は naive な UTC の "YYYY-MM-DD HH:MM:SS"
function parseUtc(s) {
  return s ? new Date(s.replace(" ", "T") + "Z") : null;
}
function hhmm(s) {
  const d = parseUtc(s);
  return d ? d.toLocaleTimeString("ja-JP", { hour: "2-digit", minute: "2-digit" }) : "";
}
function minutesAgo(s) {
  const d = parseUtc(s);
  return d ? Math.max(0, Math.floor((Date.now() - d.getTime()) / 60000)) : 0;
}

// f を ms ごとに呼ぶ。前の回が終わっていなければ飛ばす（遅いサーバで回が重なって詰まらないように）。
// hiddenMs を渡すとタブが裏にある間はその間隔、渡さなければ裏では呼ばない
function poll(f, ms, hiddenMs) {
  let busy = false, last = 0;
  const tick = async () => {
    const wait = document.hidden ? hiddenMs : ms;
    if (busy || !wait || Date.now() - last < wait - 50) return;
    busy = true;
    try { await f(); } catch (e) {} finally { busy = false; last = Date.now(); }
  };
  setInterval(tick, 1000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) { last = 0; tick(); } });
}

// 時刻の換算。サーバは UTC の "YYYY-MM-DD HH:MM:SS"、画面は端末の時刻（日本なら日本時間）
function pad2(n) { return String(n).padStart(2, "0"); }
function toUtcStr(d) {
  return d.getUTCFullYear() + "-" + pad2(d.getUTCMonth() + 1) + "-" + pad2(d.getUTCDate()) + " " +
    pad2(d.getUTCHours()) + ":" + pad2(d.getUTCMinutes()) + ":" + pad2(d.getUTCSeconds());
}
// <input type="datetime-local"> の値（端末の時刻）⇄ サーバの UTC 文字列
function localInputToUtc(v) { return v ? toUtcStr(new Date(v)) : ""; }
function utcToLocalInput(s) {
  const d = parseUtc(s);
  return d ? d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate()) + "T" +
    pad2(d.getHours()) + ":" + pad2(d.getMinutes()) : "";
}
function mdhm(s) {
  const d = parseUtc(s);
  return d ? (d.getMonth() + 1) + "/" + d.getDate() + " " + pad2(d.getHours()) + ":" + pad2(d.getMinutes()) : "";
}
function hoursText(min) { return Math.floor(min / 60) + " 時間 " + pad2(min % 60) + " 分"; }
function thisMonth() {
  const d = new Date();
  return d.getFullYear() + "-" + pad2(d.getMonth() + 1);
}
const KIND_LABEL = { in: "出勤", out: "退勤" };
const TENANT = PARAMS.get("tenant") || "";
function pageUrl(name, extra) {
  return name + "?tenant=" + encodeURIComponent(TENANT) + (extra || "");
}

// 修正リクエストのダイアログ。opts = { action: "change" | "add" | "delete", punch, kind, at, onDone }
//   change / delete は punch（対象の打刻 {id, kind, at}）が要る。add は kind / at を初期値にできる
function openRequestDialog(opts) {
  const dlg = el("dialog");
  const title = { change: "打刻を直すお願い", add: "打刻を足すお願い", delete: "打刻を消すお願い" }[opts.action];
  const kindSel = el("select", {}, el("option", { value: "in", text: "出勤" }), el("option", { value: "out", text: "退勤" }));
  kindSel.value = opts.kind || (opts.punch ? opts.punch.kind : "in");
  const at = el("input", { type: "datetime-local" });
  at.value = utcToLocalInput(opts.at || (opts.punch ? opts.punch.at : ""));
  const reason = el("textarea", { placeholder: "例: 退勤を押し忘れました" });
  const msg = el("div", { class: "msg" });
  const body = el("div");
  if (opts.punch) {
    body.append(el("p", { class: "note", text: "いまの打刻: " + KIND_LABEL[opts.punch.kind] + " " + mdhm(opts.punch.at) }));
  }
  if (opts.action !== "delete") {
    body.append(el("div", { class: "row" }, el("span", { text: "種類" }), kindSel),
      el("div", { class: "row" }, el("span", { text: "正しい時刻" }), at));
  }
  body.append(el("div", { style: "margin-top:10px" }, el("div", { text: "理由" }), reason));
  const send = el("button", { class: "primary", text: "お願いする" });
  const close = el("button", { text: "やめる", onclick: () => { dlg.close(); dlg.remove(); } });
  send.onclick = async () => {
    const b = { tenant: TENANT, action: opts.action, reason: reason.value.trim() };
    if (opts.punch) b.punch = opts.punch.id;
    if (opts.action !== "delete") {
      b.kind = kindSel.value;
      b.at = localInputToUtc(at.value);
    }
    send.disabled = true;
    const r = await api("request", b);
    send.disabled = false;
    if (r.status !== "success") {
      msg.textContent = msgOf(r);
      msg.className = "msg err";
      return;
    }
    dlg.close();
    dlg.remove();
    if (opts.onDone) opts.onDone(r.request);
  };
  dlg.append(el("h2", { text: title }), body, msg, el("div", { class: "row" }, send, close),
    el("p", { class: "note", text: "お店の管理者が承認すると打刻が直ります。" }));
  document.body.append(dlg);
  dlg.showModal();
}

// ログインしていない・メンバーでないときの案内
function showDenied(box, r) {
  box.classList.remove("hidden");
  box.innerHTML = "";
  box.append(el("b", { text: msgOf(r) }),
    el("p", { class: "note", text: "このお店のメンバーとしてパスキーでログインした人だけが使えます。" +
      "はじめての人はログイン画面で登録して、このお店に参加申請してください。" }),
    el("div", { class: "row" }, el("a", { href: pageUrl("index.html"), text: "ログイン・参加申請の画面へ" })));
}

function navLinks(current) {
  const links = [["clock.html", "打刻"], ["month.html", "勤務時間"], ["requests.html", "修正リクエスト"], ["index.html", "ログイン・メンバー"]];
  return el("div", { class: "nav" }, ...links.map(([href, text]) =>
    href === current ? el("b", { text }) : el("a", { href: pageUrl(href), text })));
}
