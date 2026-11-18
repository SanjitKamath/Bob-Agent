import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { bob } from "./bob";
import "./styles.css";
import { App } from "./views/App";
import { Menu } from "./views/Menu";
import { NewTask } from "./views/NewTask";
import { Panel } from "./views/Panel";
import { Settings } from "./views/Settings";
import { Setup } from "./views/Setup";

const VIEWS = { app: App, panel: Panel, menu: Menu, settings: Settings, setup: Setup, newtask: NewTask };
const View = VIEWS[bob.view];

document.documentElement.dataset.view = bob.view;
// "Reduce transparency" in macOS turns every frosted surface solid.
const solid = (on: boolean): void => void document.documentElement.classList.toggle("solid", on);
void bob.state().then((state) => solid(state.reduceTransparency));
bob.onState((state) => solid(state.reduceTransparency));
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <View />
  </StrictMode>,
);
