// A stand-in for the speech helper, for tests and for trying the app without Xcode.
// It speaks the helper's protocol and "hears" the words in BOB_STT_SCRIPT, one more word for each
// 200 ms of audio it is sent. On "stop" it settles everything it heard.
//   BOB_STT_SCRIPT   the words to hear (default: "what is on my list today")
//   BOB_STT_FAIL     "start" to answer the start command with an error
import process from "node:process";

const words = (process.env.BOB_STT_SCRIPT ?? "what is on my list today").split(/\s+/).filter(Boolean);
const FRAME_MS = 40;
const send = (event) => process.stdout.write(`${JSON.stringify(event)}\n`);

let heard = 0;
let frames = 0;
let started = false;
let buffer = Buffer.alloc(0);

function finish(final) {
  if (final && heard > 0) send({ type: "final", text: words.slice(0, heard).join(" ") });
  send({ type: "closed" });
  process.exit(0);
}

function onCommand(command) {
  if (command.cmd === "start") {
    if (process.env.BOB_STT_FAIL === "start") {
      send({ type: "error", code: "failed", message: "the language is not installed" });
      process.exit(1);
    }
    started = true;
    send({ type: "ready", engine: "fake", locale: command.locale });
  } else if (command.cmd === "stop") finish(true);
  else if (command.cmd === "cancel") finish(false);
}

function onAudio() {
  if (!started) return;
  frames += 1;
  if (frames % (200 / FRAME_MS) === 0 && heard < words.length) {
    heard += 1;
    send({ type: "partial", text: words.slice(0, heard).join(" ") });
  }
}

process.stdin.on("data", (chunk) => {
  buffer = Buffer.concat([buffer, chunk]);
  while (buffer.length >= 5) {
    const length = buffer.readUInt32LE(1);
    if (buffer.length < 5 + length) return;
    const kind = buffer[0];
    const payload = buffer.subarray(5, 5 + length);
    buffer = buffer.subarray(5 + length);
    if (kind === 1) onCommand(JSON.parse(payload.toString("utf8")));
    else if (kind === 2) onAudio();
  }
});
process.stdin.on("end", () => finish(false));
