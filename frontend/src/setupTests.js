// Test-environment polyfills (react-scripts loads this file
// automatically when it exists). jsdom doesn't provide TextEncoder /
// TextDecoder, which react-router v7 touches at import time — without
// these, no component that uses the router can be rendered in a test.
import { TextEncoder, TextDecoder } from "util";

if (typeof global.TextEncoder === "undefined") {
  global.TextEncoder = TextEncoder;
}
if (typeof global.TextDecoder === "undefined") {
  global.TextDecoder = TextDecoder;
}
