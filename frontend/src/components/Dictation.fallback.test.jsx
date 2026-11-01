// Voice FALLBACK tests: this jsdom world has NO SpeechRecognition, so
// the components must take the recorder path — MediaRecorder answers
// posted to the (mocked) /voice/transcribe endpoint feed the SAME
// walk/correction handlers as the built-in recognizer. Fakes are
// installed BEFORE the component module loads, because support is
// detected at module scope — hence require() below. Silence is never
// posted (transcription models invent words for silence); the walk
// still never submits; the browser note flips to the owner's locked
// Firefox wording only when the mocked /voice/config reports the
// fallback configured.
import React, { useState } from "react";
import { act, fireEvent, render, screen } from "@testing-library/react";
import i18n from "@/lib/i18n";

// Voice-activity control shared by every fake analyser: level 200 is
// clearly speech (RMS ~0.56), 128 is digital silence (RMS 0).
const vad = { level: 200 };
class FakeAnalyser {
  constructor() {
    this.fftSize = 2048;
  }
  getByteTimeDomainData(arr) {
    arr.fill(vad.level);
  }
  connect() {}
}
class FakeAudioContext {
  createMediaStreamSource() {
    return { connect() {} };
  }
  createAnalyser() {
    return new FakeAnalyser();
  }
  close() {}
}
class FakeRecorder {
  constructor(stream) {
    this.stream = stream;
    this.state = "inactive";
    this.mimeType = "audio/webm";
    FakeRecorder.instances.push(this);
  }
  start() {
    this.state = "recording";
  }
  stop() {
    if (this.state === "inactive") return;
    this.state = "inactive";
    if (this.ondataavailable) this.ondataavailable({ data: { size: 4096 } });
    if (this.onstop) this.onstop();
  }
}
FakeRecorder.instances = [];
// NOTE: this repo's jest config (react-scripts) sets resetMocks:true,
// which strips implementations from jest.fn() mocks before every test.
// The fakes the FLOW depends on are therefore plain functions with
// hand-tracked state, not jest.fn()s.
let gumRejectNext = null;
const gumCalls = [];
function gum(constraints) {
  gumCalls.push(constraints);
  if (gumRejectNext) {
    const err = gumRejectNext;
    gumRejectNext = null;
    return Promise.reject(err);
  }
  return Promise.resolve({ getTracks: () => [{ stop() {} }] });
}
window.MediaRecorder = FakeRecorder;
window.AudioContext = FakeAudioContext;
Object.defineProperty(window.navigator, "mediaDevices", {
  value: { getUserMedia: gum },
  configurable: true,
});
window.speechSynthesis = { speak: jest.fn(), cancel: jest.fn(), speaking: false };
window.SpeechSynthesisUtterance = class {
  constructor(text) {
    this.text = text;
    this.lang = "";
  }
};

let fetchCalls = [];
let configFallback = false;
let transcribeScript = [];
// Plain function on window.fetch (see the resetMocks note above):
// module code's bare `fetch` resolves through the window.
window.fetch = async (url, opts = {}) => {
  const u = String(url);
  fetchCalls.push({ url: u, opts });
  if (u.includes("/voice/config")) {
    return { ok: true, json: async () => ({ ok: true, voice: { fallback: configFallback } }) };
  }
  if (u.includes("/voice/transcribe")) {
    const outcome = transcribeScript.length ? transcribeScript.shift() : { text: "Ada Lovelace" };
    if (outcome.fail) {
      return { ok: false, status: 503, json: async () => ({ ok: false, error: "voice_unavailable" }) };
    }
    return { ok: true, json: async () => ({ ok: true, text: outcome.text, provider: "cloudflare", rung: 2 }) };
  }
  return { ok: false, status: 404, json: async () => ({}) };
};
const transcribeCalls = () => fetchCalls.filter((c) => c.url.includes("/voice/transcribe"));

const { DictateButton, GuidedVoiceFill, normalizeEmailDictation, resetVoiceFallbackConfigCache } =
  require("./Dictation");

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function pump(ms) {
  await act(async () => {
    await sleep(ms);
  });
}
async function until(cond, label, timeoutMs = 15000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    if (cond()) return;
    await pump(30);
  }
  throw new Error("timed out waiting for: " + label);
}
const lastRecorder = () => FakeRecorder.instances[FakeRecorder.instances.length - 1];

// Drive one recorded answer: wait for the next un-driven recorder to
// be live (it may already exist — the walk starts the next field's
// recorder while the test is still observing the previous fill), let
// the VAD hear speech briefly, then go quiet so the pause ends it.
let drivenUpTo = 0;
async function answerByRecording(label) {
  await until(
    () => FakeRecorder.instances.length > drivenUpTo && lastRecorder().state === "recording",
    label + ": recorder live"
  );
  drivenUpTo = FakeRecorder.instances.length;
  vad.level = 200;
  await pump(400);
  vad.level = 128;
}

function WalkHarness({ onSubmit }) {
  const [vals, setVals] = useState({ name: "", email: "" });
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        onSubmit();
      }}
    >
      <label>
        Your name
        <input
          id="f-name"
          value={vals.name}
          onChange={(e) => setVals((s) => ({ ...s, name: e.target.value }))}
        />
      </label>
      <label>
        Email
        <input
          id="f-email"
          value={vals.email}
          onChange={(e) => setVals((s) => ({ ...s, email: e.target.value }))}
        />
      </label>
      <GuidedVoiceFill
        fields={[
          { key: "name", id: "f-name", label: "Your name" },
          { key: "email", id: "f-email", label: "Email", transform: normalizeEmailDictation },
        ]}
        onFill={(key, value) => setVals((s) => ({ ...s, [key]: value }))}
      />
      <button type="submit">Create workspace</button>
    </form>
  );
}

beforeEach(async () => {
  FakeRecorder.instances = [];
  drivenUpTo = 0;
  vad.level = 200;
  fetchCalls = [];
  transcribeScript = [];
  configFallback = false;
  gumRejectNext = null;
  gumCalls.length = 0;
  resetVoiceFallbackConfigCache();
  window.speechSynthesis.speak.mockClear();
  window.speechSynthesis.cancel.mockClear();
  await i18n.changeLanguage("en");
});

describe("recorder fallback path (no built-in recognizer)", () => {
  test("controls render, the privacy line names the service path, the note starts honest", async () => {
    render(<WalkHarness onSubmit={jest.fn()} />);
    expect(screen.getByRole("button", { name: /fill it in by talking/i })).toBeTruthy();
    expect(screen.getByText(/sent to our speech service \(Cloudflare\)/i)).toBeTruthy();
    expect(screen.getByText(/isn't available in Firefox/i)).toBeTruthy();
    render(<DictateButton onText={() => {}} />);
    expect(screen.getByTestId("dictate-button")).toBeTruthy();
    await pump(50);
    expect(transcribeCalls().length).toBe(0);
  }, 20000);

  test("the walk fills fields from transcripts through the same pipeline and never submits", async () => {
    transcribeScript = [{ text: "Ada Lovelace" }, { text: "ada at example dot com" }];
    const onSubmit = jest.fn();
    render(<WalkHarness onSubmit={onSubmit} />);
    fireEvent.click(screen.getByRole("button", { name: /fill it in by talking/i }));

    await answerByRecording("name answer");
    await until(() => screen.getByLabelText(/your name/i).value === "Ada Lovelace", "name filled");
    expect(transcribeCalls().length).toBe(1);
    expect(transcribeCalls()[0].url).toMatch(/\/voice\/transcribe\?lang=en-US/);
    expect(transcribeCalls()[0].opts.headers["Content-Type"]).toBe("audio/webm");
    expect(transcribeCalls()[0].opts.headers["X-Audio-Duration-Ms"]).toMatch(/^\d+$/);

    await answerByRecording("email answer");
    await until(() => screen.getByLabelText(/email/i).value === "ada@example.com", "email filled");
    await until(() => screen.queryByText(/all set/i), "walk done");
    expect(transcribeCalls().length).toBe(2);
    expect(onSubmit).not.toHaveBeenCalled();
  }, 30000);

  test("pure silence is never posted — the pause just advances the walk", async () => {
    vad.level = 128;
    render(<WalkHarness onSubmit={jest.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /fill it in by talking/i }));
    await until(() => FakeRecorder.instances.length >= 2, "silence advanced the walk to the next field");
    expect(transcribeCalls().length).toBe(0);
    expect(screen.getByLabelText(/your name/i).value).toBe("");
    await until(() => screen.queryByText(/all set/i), "the walk rides pauses to the end");
    expect(transcribeCalls().length).toBe(0);
  }, 30000);

  test("a transcribe failure ends the walk with the honest error, fields untouched", async () => {
    transcribeScript = [{ fail: true }];
    render(<WalkHarness onSubmit={jest.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /fill it in by talking/i }));
    await answerByRecording("doomed answer");
    await until(() => screen.queryByText(/voice typing stopped/i), "error notice shown");
    expect(screen.getByLabelText(/your name/i).value).toBe("");
  }, 30000);

  test("spoken commands still steer the walk by transcript", async () => {
    transcribeScript = [{ text: "next" }, { text: "stop" }];
    render(<WalkHarness onSubmit={jest.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /fill it in by talking/i }));
    await answerByRecording("next command");
    await until(() => screen.queryByText(/email — speak now/i), "advanced to email");
    expect(screen.getByLabelText(/your name/i).value).toBe("");
    await answerByRecording("stop command");
    await until(() => screen.queryByText(/voice fill stopped/i), "walk stopped");
  }, 30000);

  test("microphone denied ends the walk with the honest blocked notice", async () => {
    gumRejectNext = Object.assign(new Error("denied"), { name: "NotAllowedError" });
    render(<WalkHarness onSubmit={jest.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /fill it in by talking/i }));
    await until(() => screen.queryByText(/microphone access is blocked/i), "denied notice shown");
  }, 20000);

  test("a correction mic dictates through the endpoint", async () => {
    transcribeScript = [{ text: "Fort Worth" }];
    const onText = jest.fn();
    render(<DictateButton onText={onText} />);
    fireEvent.click(screen.getByTestId("dictate-button"));
    await answerByRecording("city dictation");
    await until(() => onText.mock.calls.length > 0, "dictated text landed");
    expect(onText).toHaveBeenCalledWith("Fort Worth");
    expect(transcribeCalls().length).toBe(1);
  }, 30000);
});

describe("note variants keyed to the service configuration", () => {
  test("the note flips to the locked Firefox wording only when the config reports fallback on", async () => {
    configFallback = true;
    const first = render(<DictateButton onText={() => {}} />);
    await until(
      () => screen.queryByText(/Voice typing works in Chrome, Edge, and Firefox/i),
      "note upgraded"
    );
    first.unmount();

    configFallback = false;
    resetVoiceFallbackConfigCache();
    render(<DictateButton onText={() => {}} />);
    await pump(300);
    expect(screen.getByText(/isn't available in Firefox/i)).toBeTruthy();
    expect(screen.queryByText(/Voice typing works in Chrome, Edge, and Firefox/i)).toBeNull();
  }, 20000);
});
