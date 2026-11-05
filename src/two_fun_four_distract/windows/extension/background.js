// Reports the domain of each window's active tab to your own 2fun4distract service, and nothing else:
// no path, no query, no page title. The manifest refuses private windows, so they are never listed.

const SERVER = "__SERVER__";
const ALARM = "report";
const REPORT_SECONDS = 30;
// The same limits the agent's samples are held to on the receiver.
const FOCUS_IDLE_SECONDS = 5 * 60;
const VISIBLE_IDLE_SECONDS = 30 * 60;

function domainOf(url) {
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return null;
    return /^[a-z0-9.-]+$/.test(parsed.hostname) ? parsed.hostname : null;
  } catch {
    return null;
  }
}

async function report() {
  const [focusState, visibleState] = await Promise.all([
    chrome.idle.queryState(FOCUS_IDLE_SECONDS),
    chrome.idle.queryState(VISIBLE_IDLE_SECONDS),
  ]);
  if (visibleState !== "active") return;

  const windows = await chrome.windows.getAll({ populate: true, windowTypes: ["normal"] });
  const entries = [];
  for (const window of windows) {
    if (window.state === "minimized") continue;
    const tab = window.tabs.find((candidate) => candidate.active);
    const domain = tab && tab.url ? domainOf(tab.url) : null;
    if (domain) entries.push({ domain, focused: window.focused && focusState === "active", visible: true });
  }
  if (entries.length === 0) return;

  await fetch(`${SERVER}/browser/samples`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ at: new Date().toISOString(), seconds: REPORT_SECONDS, entries }),
  });
}

function schedule() {
  chrome.alarms.create(ALARM, { periodInMinutes: REPORT_SECONDS / 60 });
}

chrome.runtime.onInstalled.addListener(schedule);
chrome.runtime.onStartup.addListener(schedule);
chrome.alarms.onAlarm.addListener((alarm) => {
  // A sample that cannot be sent is lost: while the receiver is away there is nothing to report to,
  // and the computer's own agent keeps the total for the time.
  if (alarm.name === ALARM) report().catch(() => {});
});
