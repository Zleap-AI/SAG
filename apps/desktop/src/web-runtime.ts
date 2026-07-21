import path from "node:path";

interface NodeModuleRuntime {
  Module: {
    _initPaths(): void;
  };
}

const webRoot = process.env.SAG_WEB_ROOT;
if (!webRoot) {
  throw new Error("SAG_WEB_ROOT is required for the packaged web runtime.");
}

process.env.NODE_PATH = path.join(webRoot, "runtime_modules");
const nodeModule = require("node:module") as NodeModuleRuntime;
nodeModule.Module._initPaths();
process.chdir(webRoot);
require(path.join(webRoot, "server.js"));
