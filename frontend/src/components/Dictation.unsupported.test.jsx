// Unsupported-browser behavior for the voice controls, in its own file
// on purpose: a fresh module registry where no SpeechRecognition mock
// is ever installed is exactly a browser without the Web Speech API —
// and that is the state the components must handle by rendering
// nothing at all (the owner's rule: no dead buttons, no apology text).
import React from "react";
import { render } from "@testing-library/react";
import "@/lib/i18n";
import { DictateButton, GuidedVoiceFill } from "./Dictation";

test("this environment really has no speech recognition", () => {
  expect(window.SpeechRecognition || window.webkitSpeechRecognition).toBeUndefined();
});

test("the guided walk control renders nothing at all", () => {
  const { container } = render(
    <GuidedVoiceFill fields={[{ key: "name", id: "x", label: "Name" }]} onFill={() => {}} />
  );
  expect(container.firstChild).toBeNull();
});

test("the per-field correction mic renders nothing at all", () => {
  const { container } = render(<DictateButton onText={() => {}} />);
  expect(container.firstChild).toBeNull();
});
