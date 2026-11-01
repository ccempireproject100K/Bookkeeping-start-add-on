// Voice input tests: a mocked SpeechRecognition drives the components
// exactly the way a browser would (result events, then the end event),
// and speech synthesis is stubbed so prompts can be asserted. The mock
// must be installed BEFORE the component module loads, because support
// is detected at module scope — hence require() below instead of an
// import for the module under test.
import React, { useState } from "react";
import { act, fireEvent, render, screen } from "@testing-library/react";
import i18n from "@/lib/i18n";

class MockRecognition {
  constructor() {
    this.lang = "";
    this.continuous = false;
    this.interimResults = false;
    MockRecognition.instances.push(this);
  }
  start() {}
  stop() {}
  abort() {}
}
MockRecognition.instances = [];
window.SpeechRecognition = MockRecognition;
window.speechSynthesis = { speak: jest.fn(), cancel: jest.fn() };
window.SpeechSynthesisUtterance = class {
  constructor(text) {
    this.text = text;
    this.lang = "";
  }
};

const { DictateButton, GuidedVoiceFill, matchVoiceCommand, normalizeEmailDictation, voiceLangFor } =
  require("./Dictation");

const lastRecognizer = () => MockRecognition.instances[MockRecognition.instances.length - 1];

function sayFinal(recognizer, transcript) {
  act(() => {
    recognizer.onresult({
      resultIndex: 0,
      results: [{ 0: { transcript }, isFinal: true, length: 1 }],
    });
  });
  act(() => {
    recognizer.onend();
  });
}

function endSilently(recognizer) {
  act(() => {
    recognizer.onend();
  });
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
  MockRecognition.instances = [];
  window.speechSynthesis.speak.mockClear();
  window.speechSynthesis.cancel.mockClear();
  await i18n.changeLanguage("en");
});

describe("voice helpers", () => {
  test("recognition language follows the page language", () => {
    expect(voiceLangFor("en")).toBe("en-US");
    expect(voiceLangFor("es")).toBe("es-ES");
    expect(voiceLangFor("fr")).toBe("fr-FR");
    expect(voiceLangFor("zh-CN")).toBe("zh-CN");
    expect(voiceLangFor("hi")).toBe("hi-IN");
    expect(voiceLangFor("ar")).toBe("ar-SA");
    expect(voiceLangFor("es-MX")).toBe("es-ES");
    expect(voiceLangFor(undefined)).toBe("en-US");
  });

  test("spoken commands match per language, exactly", () => {
    expect(matchVoiceCommand("next", "en-US")).toBe("next");
    expect(matchVoiceCommand("Skip.", "en-US")).toBe("next");
    expect(matchVoiceCommand("go back", "en-US")).toBe("back");
    expect(matchVoiceCommand("done", "en-US")).toBe("stop");
    expect(matchVoiceCommand("siguiente", "es-ES")).toBe("next");
    expect(matchVoiceCommand("atrás", "es-ES")).toBe("back");
    expect(matchVoiceCommand("listo", "es-ES")).toBe("stop");
    expect(matchVoiceCommand("suivant", "fr-FR")).toBe("next");
    expect(matchVoiceCommand("下一个", "zh-CN")).toBe("next");
    expect(matchVoiceCommand("停止", "zh-CN")).toBe("stop");
    expect(matchVoiceCommand("अगला", "hi-IN")).toBe("next");
    expect(matchVoiceCommand("التالي", "ar-SA")).toBe("next");
    // A real answer that merely contains a command word is an answer.
    expect(matchVoiceCommand("Next Day Air Plumbing", "en-US")).toBe(null);
    expect(matchVoiceCommand("Ada Lovelace", "en-US")).toBe(null);
  });

  test("dictated emails become addresses", () => {
    expect(normalizeEmailDictation("ada at example dot com")).toBe("ada@example.com");
    expect(normalizeEmailDictation("Ada@Example.com")).toBe("ada@example.com");
    expect(normalizeEmailDictation("  bob smith at gmail dot com ")).toBe("bobsmith@gmail.com");
  });
});

describe("DictateButton (per-field correction mic)", () => {
  test("dictates into onText, in the page's language", async () => {
    await i18n.changeLanguage("es");
    const onText = jest.fn();
    render(<DictateButton onText={onText} />);
    fireEvent.click(screen.getByTestId("dictate-button"));
    const rec = lastRecognizer();
    expect(rec.lang).toBe("es-ES");
    sayFinal(rec, "Hola mundo");
    expect(onText).toHaveBeenCalledWith("Hola mundo");
  });

  test("carries the small browser-variation side note (owner's exact text)", () => {
    render(<DictateButton onText={() => {}} />);
    expect(screen.getByText(/Voice typing is offered by your browser/i)).toBeTruthy();
    expect(screen.getByText(/isn't available in Firefox/i)).toBeTruthy();
    expect(screen.getByText(/Typing always works/i)).toBeTruthy();
  });
});

describe("GuidedVoiceFill (the one-button walk)", () => {
  test("fills each field and advances, speaks the prompt, never submits", () => {
    const onSubmit = jest.fn();
    render(<WalkHarness onSubmit={onSubmit} />);
    fireEvent.click(screen.getByRole("button", { name: /fill it in by talking/i }));

    const nameRec = lastRecognizer();
    expect(nameRec.lang).toBe("en-US");
    expect(window.speechSynthesis.speak).toHaveBeenCalled();
    const firstUtterance = window.speechSynthesis.speak.mock.calls[0][0];
    expect(firstUtterance.text).toMatch(/your name/i);
    expect(firstUtterance.lang).toBe("en-US");
    expect(document.getElementById("f-name").classList.contains("voice-walk-highlight")).toBe(true);

    sayFinal(nameRec, "Ada Lovelace");
    expect(screen.getByLabelText(/your name/i).value).toBe("Ada Lovelace");
    // Advanced: a fresh recognizer is listening for the email field.
    const emailRec = lastRecognizer();
    expect(emailRec).not.toBe(nameRec);
    expect(screen.getByText(/email — speak now/i)).toBeTruthy();

    sayFinal(emailRec, "ada at example dot com");
    expect(screen.getByLabelText(/email/i).value).toBe("ada@example.com");
    expect(screen.getByText(/all set/i)).toBeTruthy();
    expect(onSubmit).not.toHaveBeenCalled();
  });

  test("next skips, go back revisits, stop ends the walk", () => {
    render(<WalkHarness onSubmit={jest.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /fill it in by talking/i }));

    sayFinal(lastRecognizer(), "next");
    expect(screen.getByLabelText(/your name/i).value).toBe("");
    expect(screen.getByText(/email — speak now/i)).toBeTruthy();

    sayFinal(lastRecognizer(), "go back");
    expect(screen.getByText(/your name — speak now/i)).toBeTruthy();

    sayFinal(lastRecognizer(), "stop");
    expect(screen.getByText(/voice fill stopped/i)).toBeTruthy();
    expect(screen.queryByText(/— speak now/i)).toBeNull();
  });

  test("a pause (session ends, nothing said) advances gracefully", () => {
    render(<WalkHarness onSubmit={jest.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /fill it in by talking/i }));
    endSilently(lastRecognizer());
    expect(screen.getByText(/email — speak now/i)).toBeTruthy();
    expect(screen.getByLabelText(/your name/i).value).toBe("");
  });

  test("the walk runs in Spanish when the page is in Spanish", async () => {
    await i18n.changeLanguage("es");
    render(<WalkHarness onSubmit={jest.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /complete el formulario hablando/i }));
    const rec = lastRecognizer();
    expect(rec.lang).toBe("es-ES");
    sayFinal(rec, "siguiente");
    expect(screen.getByLabelText(/your name/i).value).toBe("");
    expect(screen.getByText(/hable ahora/i)).toBeTruthy();
  });

  test("the note sits with the walk control, in the owner's exact words", () => {
    render(<WalkHarness onSubmit={jest.fn()} />);
    const walk = screen.getByTestId("guided-voice-fill");
    expect(walk.textContent).toMatch(/Voice typing is offered by your browser/i);
    expect(walk.textContent).toMatch(/works in Chrome and Edge/i);
    expect(walk.textContent).toMatch(/may behave differently in Safari/i);
    expect(walk.textContent).toMatch(/isn't available in Firefox/i);
    expect(walk.textContent).toMatch(/Typing always works/i);
  });

  test("microphone denied ends the walk with the honest notice", () => {
    render(<WalkHarness onSubmit={jest.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /fill it in by talking/i }));
    const rec = lastRecognizer();
    act(() => {
      rec.onerror({ error: "not-allowed" });
    });
    act(() => {
      rec.onend();
    });
    expect(screen.getByText(/microphone access is blocked/i)).toBeTruthy();
    expect(screen.queryByText(/— speak now/i)).toBeNull();
  });
});


