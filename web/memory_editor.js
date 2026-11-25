// The Memory panel: a sidebar tab to view, edit and save back the values the
// Save Memory nodes store (a conversation history, a text), without going
// through a workflow. The HTTP routes it talks to are in routes.py.
//
// The memory is split in sessions, one per browser tab: the panel shows the
// one of its tab (the client id of the page, what its workflows are queued
// with) and the memory shared by all of them.

import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const TAB_ID = "openai-api-memory";
const PATH = "/openai_api/memory";
const ROLES = ["system", "developer", "user", "assistant"];
// Where a key lives, in the order of the list
const SCOPES = [
    { id: "session", title: "This session", help: "Private to this browser tab, for the Save Memory nodes where Shared is off." },
    { id: "shared", title: "Shared by all sessions", help: "For the Save Memory nodes where Shared is on, and the workflows queued without client id (scripts calling the API)." },
];
// The events telling that a run may have stored something (a Save Memory
// node). The first ones only reach the page that queued the prompt: the queue
// status is broadcast, so it also covers the runs queued by another tab or
// client.
const RUN_EVENTS = ["execution_success", "execution_error", "execution_interrupted", "status"];

// ---- state, kept while the sidebar tab is closed ----------------------------

const state = {
    entries: null,   // the keys holding a value, null before the first load
    listError: null,
    // The key being edited, null in the list of keys:
    //   { scope, key, kind, version, original (JSON text), draft, status, newerVersion, saving }
    edit: null,
};
let mounted = null;      // { root, body, toolbar } of the open tab, null when closed
const invalid = new Set(); // the JSON fields currently holding invalid JSON
let refreshTimer = null;

// ---- small helpers ---------------------------------------------------------

function h(tag, props = {}, ...children) {
    const el = document.createElement(tag);
    for (const [name, value] of Object.entries(props)) {
        if (value == null) continue;
        if (name === "class") el.className = value;
        else if (name.startsWith("on")) el.addEventListener(name.slice(2), value);
        else if (name in el) el[name] = value;
        else if (value !== false) el.setAttribute(name, value);
    }
    el.append(...children.flat().filter((c) => c != null && c !== false));
    return el;
}

const button = (label, title, onclick, extra = {}) =>
    h("button", { type: "button", class: "oaim-btn", title, "aria-label": title, onclick, ...extra }, label);

function plural(n, noun) {
    return n === 1 ? `${n} ${noun}` : `${n} ${noun}s`;
}

function kib(chars) {
    return chars >= 1024 * 1024 ? `${(chars / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.round(chars / 1024))} kB`;
}

const isDirty = (e) => JSON.stringify(e.draft) !== e.original;

async function request(path, options) {
    const response = await api.fetchApi(path, options);
    let data = {};
    try {
        data = await response.json();
    } catch {
        // not JSON (a proxy error page...): the status is reported
    }
    return { ok: response.ok, status: response.status, data };
}

// The client id of this page: its session. Unknown until its socket connects.
const clientId = () => api.clientId ?? "";

function errorText(result) {
    return result.data?.error || `The server answered HTTP ${result.status}`;
}

// ---- styles ----------------------------------------------------------------

const STYLE = `
.oaim { box-sizing: border-box; display: flex; flex-direction: column; gap: 8px; padding: 8px; min-height: 100%;
  color: var(--fg-color, #ddd); font-size: 13px; line-height: 1.4; }
.oaim * { box-sizing: border-box; }
.oaim [hidden] { display: none !important; }
.oaim-bar { position: sticky; top: 0; z-index: 2; display: flex; flex-wrap: wrap; align-items: center; gap: 6px;
  padding: 6px 0; background: var(--comfy-menu-bg, #202020); }
.oaim-title { flex: 1 1 8em; min-width: 0; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.oaim-title code { font-family: ui-monospace, monospace; }
.oaim-status { flex: 1 1 100%; color: var(--descrip-text, #999); font-size: 12px; }
.oaim-status.oaim-bad { color: var(--error-text, #f66); }
.oaim-help { margin: 0; color: var(--descrip-text, #999); font-size: 12px; }
.oaim-btn { cursor: pointer; padding: 3px 9px; border-radius: 4px; border: 1px solid var(--border-color, #555);
  background: var(--comfy-input-bg, #333); color: var(--input-text, var(--fg-color, #ddd)); font: inherit; }
.oaim-btn:hover:not(:disabled) { filter: brightness(1.25); }
.oaim-btn:disabled { opacity: .4; cursor: default; }
.oaim-primary { background: var(--p-primary-color, #3b82f6); border-color: transparent; color: #fff; }
.oaim-icon { padding: 1px 6px; }
.oaim-banner { padding: 6px 8px; border-radius: 4px; border: 1px solid #d9a21b; background: rgba(217,162,27,.15);
  display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.oaim-banner span { flex: 1 1 14em; }
.oaim-group { margin-top: 4px; font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: .04em;
  color: var(--descrip-text, #999); }
.oaim-list { display: flex; flex-direction: column; gap: 6px; }
.oaim-row { display: flex; flex-direction: column; gap: 2px; align-items: flex-start; text-align: left; width: 100%;
  padding: 6px 8px; border-radius: 4px; border: 1px solid var(--border-color, #555);
  background: var(--comfy-input-bg, #333); color: inherit; font: inherit; cursor: pointer; }
.oaim-row:hover:not(:disabled) { filter: brightness(1.2); }
.oaim-row:disabled { opacity: .55; cursor: default; }
.oaim-key { font-family: ui-monospace, monospace; font-weight: 600; overflow-wrap: anywhere; }
.oaim-kind { margin-left: 6px; padding: 0 5px; border-radius: 8px; font-size: 11px; font-weight: 400;
  border: 1px solid var(--border-color, #555); color: var(--descrip-text, #999); }
.oaim-sum { color: var(--descrip-text, #999); font-size: 12px; }
.oaim-msgs { display: flex; flex-direction: column; }
.oaim-gap { align-self: center; cursor: pointer; width: 100%; height: 14px; margin: 0; padding: 0; line-height: 10px;
  border: 0; background: transparent; color: var(--descrip-text, #999); font-size: 12px; opacity: .35; }
.oaim-gap:hover { opacity: 1; background: rgba(128,128,128,.15); }
.oaim-msg { display: flex; flex-direction: column; gap: 6px; padding: 6px 8px; border-radius: 4px;
  border: 1px solid var(--border-color, #555); border-left: 4px solid #888; background: var(--comfy-input-bg, #333); }
.oaim-role-system, .oaim-role-developer { border-left-color: #a78bfa; }
.oaim-role-user { border-left-color: #3b82f6; }
.oaim-role-assistant { border-left-color: #22c55e; }
.oaim-head { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.oaim-head .oaim-grow { flex: 1; }
.oaim-num { color: var(--descrip-text, #999); font-size: 12px; }
.oaim-tag { padding: 0 6px; border-radius: 8px; font-size: 11px; border: 1px solid var(--border-color, #555);
  color: var(--descrip-text, #999); }
.oaim-tag.oaim-bad { color: var(--error-text, #f66); border-color: var(--error-text, #f66); }
.oaim-select { padding: 2px 4px; border-radius: 4px; border: 1px solid var(--border-color, #555);
  background: var(--comfy-menu-bg, #202020); color: inherit; font: inherit; }
.oaim-text { display: block; width: 100%; min-height: 3.6em; max-height: 50vh; padding: 4px 6px; resize: vertical;
  border-radius: 4px; border: 1px solid var(--border-color, #555); background: var(--comfy-menu-bg, #202020);
  color: var(--input-text, var(--fg-color, #ddd)); font: inherit; field-sizing: content; }
.oaim-text.oaim-json { font-family: ui-monospace, monospace; font-size: 12px; }
.oaim-text.oaim-bad { border-color: var(--error-text, #f66); }
.oaim-whole { min-height: 40vh; max-height: 75vh; }
.oaim-part { display: flex; flex-direction: column; gap: 3px; }
.oaim-partbar { display: flex; align-items: center; gap: 6px; color: var(--descrip-text, #999); font-size: 12px; }
.oaim-partbar .oaim-grow { flex: 1; }
.oaim-img { align-self: flex-start; max-width: 100%; max-height: 110px; border-radius: 4px; cursor: zoom-in;
  border: 1px solid var(--border-color, #555); background: #000; }
.oaim-img.oaim-big { max-height: none; cursor: zoom-out; }
.oaim-extras summary { cursor: pointer; color: var(--descrip-text, #999); font-size: 12px; }
.oaim-extras .oaim-part { margin-top: 6px; }
.oaim-add { align-self: flex-start; }
`;

function ensureStyle() {
    if (document.getElementById("oaim-style")) return;
    document.head.append(h("style", { id: "oaim-style", textContent: STYLE }));
}

// ---- inputs ----------------------------------------------------------------

const AUTOSIZE_NEEDED = !(globalThis.CSS?.supports?.("field-sizing", "content"));

// Browsers without field-sizing do not grow a textarea with its text
function autosize(textarea) {
    if (!AUTOSIZE_NEEDED || !textarea.isConnected || !textarea.scrollHeight) return;
    textarea.style.height = "auto";
    textarea.style.height = `${textarea.scrollHeight + 2}px`;
}

function textArea(value, onChange, { label, classes = "" } = {}) {
    const textarea = h("textarea", {
        class: `oaim-text ${classes}`, value, spellcheck: false, "aria-label": label,
        oninput: () => {
            autosize(textarea);
            onChange(textarea.value);
        },
    });
    textarea.dataset.autosize = "1";
    return textarea;
}

// A field edited as JSON text, for what has no better editor: only valid
// JSON reaches the draft, and Save is disabled while one field is invalid
function jsonArea(value, onChange, label) {
    const token = {};
    const textarea = h("textarea", {
        class: "oaim-text oaim-json", value: JSON.stringify(value, null, 2), spellcheck: false, "aria-label": label,
        oninput: () => {
            autosize(textarea);
            try {
                onChange(JSON.parse(textarea.value));
                invalid.delete(token);
                textarea.classList.remove("oaim-bad");
            } catch {
                invalid.add(token);
                textarea.classList.add("oaim-bad");
            }
            touch();
        },
    });
    textarea.dataset.autosize = "1";
    return textarea;
}

// ---- the history editor ----------------------------------------------------

const roleClass = (role) => `oaim-role-${ROLES.includes(role) ? role : "other"}`;

function isEmptyMessage(message) {
    const { content } = message;
    if (typeof content === "string") return content.trim() === "";
    if (!Array.isArray(content)) return false;
    return content.every((part) => part?.type === "text" && typeof part.text === "string" && part.text.trim() === "");
}

function imageCount(message) {
    return Array.isArray(message.content) ? message.content.filter((p) => p?.type === "image_url").length : 0;
}

// A turn starts at a user message: what follows one is its answer, so the
// natural next message of a user one is an assistant one, and conversely
function nextRole(previous) {
    return previous?.role === "user" ? "assistant" : "user";
}

function partBar(label, onRemove, note) {
    return h("div", { class: "oaim-partbar" }, label, note ? h("span", {}, note) : null, h("span", { class: "oaim-grow" }),
        button("✕", `Remove this ${label}`, onRemove, { class: "oaim-btn oaim-icon" }));
}

function imagePart(message, index) {
    const part = message.content[index];
    const url = part.image_url?.url;
    const shown = typeof url === "string" && /^(data:image\/|https?:)/i.test(url);
    const img = shown ? h("img", { class: "oaim-img", src: url, alt: "image of the message", title: "Click to enlarge" }) : null;
    img?.addEventListener("click", () => img.classList.toggle("oaim-big"));
    return h("div", { class: "oaim-part" },
        partBar("image", () => removePart(message, index), typeof url === "string" ? `(${kib(url.length)})` : null),
        img ?? h("span", { class: "oaim-sum" }, "This image can not be shown"));
}

function jsonPart(message, index) {
    const part = message.content[index];
    return h("div", { class: "oaim-part" },
        partBar(part?.type ?? "part", () => removePart(message, index)),
        jsonArea(part, (parsed) => { message.content[index] = parsed; }, `The ${part?.type ?? ""} part, as JSON`));
}

// The reasoning of the reasoning models is long and rarely what is edited: it
// is kept closed. It is in the history as extra fields of the assistant
// messages (reasoning_content, reasoning, reasoning_details) or, for Mistral
// models, as "thinking" parts of the content.
const REASONING_FIELDS = ["reasoning_content", "reasoning", "reasoning_details"];
const THINKING_PART = "thinking";

// The sections the user opened, per message: they stay open when the list is
// redrawn (a delete, a move...) instead of closing again
const opened = new WeakMap();

function collapsible(message, id, summary, children) {
    const details = h("details", { class: "oaim-extras", open: opened.get(message)?.has(id) ?? false },
        h("summary", {}, summary), children);
    details.addEventListener("toggle", () => {
        if (!opened.has(message)) opened.set(message, new Set());
        opened.get(message)[details.open ? "add" : "delete"](id);
    });
    return details;
}

function thinkingPart(message, index) {
    const part = message.content[index];
    return collapsible(message, `part:${index}`, "Thinking", h("div", { class: "oaim-part" },
        partBar("thinking", () => removePart(message, index)),
        jsonArea(part, (parsed) => { message.content[index] = parsed; }, "The thinking part, as JSON")));
}

function textPart(message, index) {
    const part = message.content[index];
    return h("div", { class: "oaim-part" },
        partBar("text", () => removePart(message, index)),
        textArea(part.text, (value) => { part.text = value; touch(); }, { label: "Text of the message" }));
}

function removePart(message, index) {
    message.content.splice(index, 1);
    redrawBody();
}

function contentEditor(message) {
    const { content } = message;
    if (typeof content === "string") {
        return textArea(content, (value) => { message.content = value; touch(); }, { label: "Text of the message" });
    }
    if (Array.isArray(content)) {
        return h("div", { class: "oaim-parts" }, content.map((part, index) => {
            if (part?.type === "text" && typeof part.text === "string") return textPart(message, index);
            if (part?.type === "image_url") return imagePart(message, index);
            if (part?.type === THINKING_PART) return thinkingPart(message, index);
            return jsonPart(message, index);
        }));
    }
    return jsonArea(content, (parsed) => { message.content = parsed; }, "The content, as JSON");
}

function fieldEditor(message, key) {
    const value = message[key];
    const editor = typeof value === "string"
        ? textArea(value, (text) => { message[key] = text; touch(); }, { label: key })
        : jsonArea(value, (parsed) => { message[key] = parsed; }, `${key}, as JSON`);
    return h("div", { class: "oaim-part" },
        partBar(key, () => { delete message[key]; redrawBody(); }), editor);
}

// What a message holds beyond its role and content, in two closed sections:
// its reasoning, and anything else
function extraFields(message) {
    const keys = Object.keys(message).filter((key) => key !== "role" && key !== "content");
    const reasoning = keys.filter((key) => REASONING_FIELDS.includes(key));
    const others = keys.filter((key) => !REASONING_FIELDS.includes(key));
    return [
        reasoning.length
            ? collapsible(message, "reasoning", `Reasoning: ${reasoning.join(", ")}`, reasoning.map((key) => fieldEditor(message, key)))
            : null,
        others.length
            ? collapsible(message, "other", `Other fields: ${others.join(", ")}`, others.map((key) => fieldEditor(message, key)))
            : null,
    ];
}

function moveMessage(e, from, to) {
    const [message] = e.draft.splice(from, 1);
    e.draft.splice(to, 0, message);
    redrawBody();
}

function messageCard(e, index) {
    const message = e.draft[index];
    const roles = ROLES.includes(message.role) ? ROLES : [...ROLES, message.role];
    const select = h("select", {
        class: "oaim-select", title: "Role of the message", "aria-label": "Role of the message",
        onchange: () => {
            message.role = select.value;
            card.className = `oaim-msg ${roleClass(message.role)}`;
            touch();
        },
    }, roles.map((role) => h("option", { value: role, textContent: role })));
    select.value = message.role;
    const images = imageCount(message);
    const emptyTag = h("span", { class: "oaim-tag oaim-bad", title: "A server may reject an empty message", hidden: !isEmptyMessage(message) }, "empty");
    const card = h("div", { class: `oaim-msg ${roleClass(message.role)}`, "data-index": String(index) },
        h("div", { class: "oaim-head" },
            h("span", { class: "oaim-num" }, `#${index + 1}`), select,
            images ? h("span", { class: "oaim-tag" }, plural(images, "image")) : null,
            emptyTag,
            h("span", { class: "oaim-grow" }),
            button("↑", "Move up", () => moveMessage(e, index, index - 1), { class: "oaim-btn oaim-icon", disabled: index === 0 }),
            button("↓", "Move down", () => moveMessage(e, index, index + 1), { class: "oaim-btn oaim-icon", disabled: index === e.draft.length - 1 }),
            button("✕", "Delete this message", () => { e.draft.splice(index, 1); redrawBody(); }, { class: "oaim-btn oaim-icon" })),
        contentEditor(message),
        extraFields(message));
    // The edits of the texts bubble up here, after they reached the message
    card.addEventListener("input", () => { emptyTag.hidden = !isEmptyMessage(message); });
    return card;
}

function insertGap(e, at) {
    return h("button", {
        type: "button", class: "oaim-gap", title: "Insert a message here", "aria-label": "Insert a message here",
        onclick: () => {
            e.draft.splice(at, 0, { role: nextRole(e.draft[at - 1]), content: "" });
            redrawBody(at);
        },
    }, "＋");
}

function historyEditor(e) {
    const list = h("div", { class: "oaim-msgs" });
    list.append(insertGap(e, 0));
    e.draft.forEach((_, index) => list.append(messageCard(e, index), insertGap(e, index + 1)));
    if (!e.draft.length) {
        list.prepend(h("p", { class: "oaim-help" }, "This history has no message. Add one with the + below."));
    }
    return list;
}

// ---- the editor view -------------------------------------------------------

function touch() {
    // An edit makes any earlier status ("Saved...") obsolete
    if (state.edit) state.edit.status = null;
    clearTimeout(touch.timer);
    // Comparing the draft with the saved text costs a pass over everything
    // it holds, images included: not on every keystroke
    touch.timer = setTimeout(updateBar, 150);
}

function updateBar() {
    const e = state.edit;
    if (!e || !mounted?.bar) return;
    const { save, revert, status, banner } = mounted.bar;
    const dirty = isDirty(e);
    save.disabled = !dirty || invalid.size > 0 || e.saving;
    revert.disabled = !dirty || e.saving;
    let text = e.status?.text ?? "";
    let bad = Boolean(e.status?.bad);
    if (invalid.size) {
        text = "A field holds invalid JSON: fix it to save";
        bad = true;
    } else if (!e.status && dirty) {
        text = "Unsaved changes";
    }
    // "Saved" next to a banner saying the key changed since would contradict it
    if (e.newerVersion != null && !bad) text = "";
    status.textContent = text;
    status.classList.toggle("oaim-bad", bad);
    banner.replaceChildren();
    if (e.newerVersion != null) {
        banner.append(
            h("span", {}, "This key was saved again since you opened it (a workflow run, or another tab). Reload to see the new value (your edits are dropped), or save yours over it."),
            button("Reload", "Load the current value, dropping your edits", () => openKey(e.scope, e.key, true)),
            button("Save anyway", "Overwrite the current value with yours", () => save_(true), { disabled: !dirty || invalid.size > 0 }));
    }
    banner.hidden = e.newerVersion == null;
}

async function save_(overwrite = false) {
    const e = state.edit;
    if (!e || e.saving) return;
    if (overwrite && e.newerVersion != null) e.version = e.newerVersion;
    e.saving = true;
    e.status = { text: "Saving..." };
    updateBar();
    const sent = JSON.stringify(e.draft);
    let result;
    try {
        result = await request(`${PATH}/value`, {
            method: "PUT", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ scope: e.scope, client_id: clientId(), key: e.key, version: e.version, value: e.draft }),
        });
    } catch (error) {
        result = { ok: false, status: 0, data: { error: `The server can not be reached (${error.message})` } };
    }
    e.saving = false;
    if (result.ok) {
        e.version = result.data.version;
        e.original = sent;
        e.newerVersion = null;
        e.status = { text: "Saved: the next run loading this key gets this value" };
        refreshList();
    } else if (result.status === 409) {
        e.newerVersion = result.data.version;
        e.status = { text: "Not saved: the key changed since you opened it", bad: true };
    } else {
        e.status = { text: `Not saved: ${errorText(result)}`, bad: true };
    }
    updateBar();
}

function revert() {
    const e = state.edit;
    e.draft = JSON.parse(e.original);
    e.status = null;
    redrawBody();
}

function leaveEditor() {
    const e = state.edit;
    if (isDirty(e) && !confirm("Discard your unsaved edits?")) return;
    state.edit = null;
    invalid.clear();
    redraw();
    refreshList();
}

function redrawBody(focusMessage) {
    const e = state.edit;
    if (!e || !mounted?.content) return;
    invalid.clear();
    e.status = null;
    mounted.content.replaceChildren(e.kind === "text" ? wholeText(e) : historyEditor(e));
    updateBar();
    requestAnimationFrame(() => {
        mounted?.content?.querySelectorAll("textarea[data-autosize]").forEach(autosize);
        if (focusMessage != null) {
            mounted?.content?.querySelector(`[data-index="${focusMessage}"] textarea`)?.focus();
        }
    });
}

function wholeText(e) {
    return textArea(e.draft, (value) => { e.draft = value; touch(); }, { label: `The text under the key ${e.key}`, classes: "oaim-whole" });
}

function drawEditor(root) {
    const e = state.edit;
    const bar = {
        save: button("Save", "Store this value back in memory", () => save_(false), { class: "oaim-btn oaim-primary" }),
        revert: button("Revert", "Drop your edits", revert),
        status: h("div", { class: "oaim-status", role: "status" }),
        banner: h("div", { class: "oaim-banner", hidden: true }),
    };
    const content = h("div", {});
    root.append(
        h("div", { class: "oaim-bar" },
            button("←", "Back to the list of keys", leaveEditor, { class: "oaim-btn oaim-icon" }),
            h("div", { class: "oaim-title", title: `${e.key} (${scopeOf(e.scope).title.toLowerCase()})` },
                "Key ", h("code", {}, e.key), h("span", { class: "oaim-kind" }, e.scope)),
            bar.revert, bar.save, bar.status),
        bar.banner,
        content,
        // append() would write "null" for a null child
        ...(e.kind === "history"
            ? [h("p", { class: "oaim-help" }, "Edits only apply when saved. Images can be removed but not added. A system or developer message is only the conversation instructions when it is the first one.")]
            : []));
    mounted.bar = bar;
    mounted.content = content;
    redrawBody();
}

async function openKey(scope, key, force = false) {  // force: reload the key being edited
    if (force && state.edit && isDirty(state.edit) && !confirm("Discard your unsaved edits and load the current value?")) return;
    let result;
    try {
        result = await request(`${PATH}/value?${new URLSearchParams({ scope, client_id: clientId(), key })}`);
    } catch (error) {
        result = { ok: false, status: 0, data: { error: `The server can not be reached (${error.message})` } };
    }
    if (!result.ok) {
        state.listError = errorText(result);
        state.edit = null;
        redraw();
        refreshList();
        return;
    }
    const { version, kind, value } = result.data;
    const original = JSON.stringify(value);
    state.edit = { scope, key, kind, version, original, draft: JSON.parse(original), status: null, newerVersion: null, saving: false };
    redraw();
}

// ---- the list of keys ------------------------------------------------------

const scopeOf = (id) => SCOPES.find((scope) => scope.id === id) ?? SCOPES[0];

function keyRow(entry) {
    const editable = entry.kind !== "other";
    return h("button", {
        type: "button", class: "oaim-row", disabled: !editable,
        title: editable ? "View and edit this value" : "Only histories and texts can be edited",
        onclick: () => openKey(entry.scope, entry.key),
    }, h("span", { class: "oaim-key" }, entry.key, h("span", { class: "oaim-kind" }, entry.kind)),
    h("span", { class: "oaim-sum" }, entry.summary));
}

function drawList(root) {
    root.append(
        h("div", { class: "oaim-bar" },
            h("div", { class: "oaim-title" }, "Memory"),
            button("↻", "Refresh the list", () => refreshList(), { class: "oaim-btn oaim-icon" })),
        h("p", { class: "oaim-help" },
            "The values stored by the Save Memory nodes, kept in RAM until ComfyUI restarts. Each browser tab has its own memory, and only sees its own: pick a history or a text to view and edit it, and the next run of a Load Memory node on that key, from this tab, gets what you save."));
    if (state.listError) root.append(h("div", { class: "oaim-banner" }, h("span", {}, state.listError)));
    if (state.entries === null) {
        root.append(h("p", { class: "oaim-help" }, "Loading..."));
        return;
    }
    if (!state.entries.length) {
        root.append(h("p", { class: "oaim-help" }, "Nothing is stored for this session yet. Run a workflow with a Save Memory node from this tab, then refresh."));
        return;
    }
    for (const scope of SCOPES) {
        const entries = state.entries.filter((entry) => entry.scope === scope.id);
        if (!entries.length) continue;
        root.append(
            h("div", { class: "oaim-group", title: scope.help }, scope.title),
            h("div", { class: "oaim-list" }, entries.map(keyRow)));
    }
}

async function refreshList() {
    let result;
    try {
        result = await request(`${PATH}?${new URLSearchParams({ client_id: clientId() })}`);
    } catch (error) {
        result = { ok: false, status: 0, data: { error: `The server can not be reached (${error.message})` } };
    }
    if (result.ok) {
        state.entries = result.data.entries;
        state.listError = null;
        const e = state.edit;
        if (e) {
            // Changed by a run (or wiped) while open: warn before the save does
            const entry = state.entries.find((x) => x.scope === e.scope && x.key === e.key);
            e.newerVersion = entry && entry.version !== e.version ? entry.version : e.newerVersion;
            if (!entry && !e.saving) {
                e.status = { text: "Nothing is stored under this key anymore (reset?). Saving is refused.", bad: true };
            }
        }
    } else {
        state.listError = errorText(result);
    }
    if (state.edit) updateBar();
    else redraw();
}

// ---- the tab ---------------------------------------------------------------

function redraw() {
    if (!mounted) return;
    const root = h("div", { class: "oaim" });
    mounted.root.replaceChildren(root);
    mounted.bar = null;
    mounted.content = null;
    if (state.edit) drawEditor(root);
    else drawList(root);
}

function mount(element) {
    ensureStyle();
    mounted = { root: element, bar: null, content: null };
    redraw();
    refreshList();
}

function unmount() {
    mounted = null;
    clearTimeout(touch.timer);
}

app.registerExtension({
    name: "OpenAIAPI.MemoryPanel",
    setup() {
        const manager = app.extensionManager;
        if (!manager?.registerSidebarTab) {
            console.warn("[openai-api] This ComfyUI frontend has no sidebar tabs: the Memory panel is not available");
            return;
        }
        manager.registerSidebarTab({
            id: TAB_ID,
            icon: "pi pi-database",
            title: "Memory",
            tooltip: "OpenAI API: view and edit the values stored by the Save Memory nodes",
            type: "custom",
            render: mount,
            destroy: unmount,
        });
        // A run may have stored (or changed) a value: refresh what is shown
        for (const name of RUN_EVENTS) {
            api.addEventListener(name, () => {
                if (!mounted) return;
                clearTimeout(refreshTimer);
                refreshTimer = setTimeout(refreshList, 300);
            });
        }
    },
});
