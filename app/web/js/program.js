// Program window for a second screen (PV-07): program.html shows the edit page's picture on its own, without the
// timeline or panels, following the main window's player over a BroadcastChannel (player.js sends the project and
// every clock tick). It stays silent; the sound plays in the main window.
import { createEngine } from "./components/player-engine.js";
import { buildSequence } from "./lib/sequence.js";

const PROGRAM_CHANNEL = "vc-program"; // player.js sends on the same name
const IDLE_CURSOR = 2000; // ms before the pointer hides over the picture

const stage = document.querySelector(".pl-stage");
const engine = createEngine(document.querySelector(".pl-host"), { dragDisabled: () => true, sound: false });
engine.setVolume(0, true);
const channel = new BroadcastChannel(PROGRAM_CHANNEL);

channel.onmessage = ({ data }) => {
  if (data.type === "state") {
    const { project, legacy, res, captions } = data;
    const { width, height, fit } = project.settings;
    stage.style.setProperty("--ar", (width / height).toFixed(5));
    stage.classList.toggle("fit-cover", fit === "cover");
    stage.classList.toggle("show-caps", captions);
    engine.setPreviewScale(res);
    engine.setProject(project, buildSequence(project), legacy);
    document.title = `${project.name}｜節目畫面｜Video Console`;
    document.body.classList.add("is-live");
  } else if (data.type === "clock") {
    engine.render(data.t, data.playing, data.rate);
  } else if (data.type === "closed") {
    engine.render(data.t, false, 1);
    document.body.classList.remove("is-live");
  }
};
channel.postMessage({ type: "hello" });
window.addEventListener("pagehide", () => channel.postMessage({ type: "bye" }));

document.addEventListener("dblclick", () => {
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  else document.documentElement.requestFullscreen().catch(() => {});
});

let idle = 0;
document.addEventListener("pointermove", () => {
  document.body.classList.remove("is-idle");
  clearTimeout(idle);
  idle = setTimeout(() => document.body.classList.add("is-idle"), IDLE_CURSOR);
});
