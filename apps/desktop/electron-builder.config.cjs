const updateBaseUrl = process.env.SAG_UPDATE_BASE_URL?.replace(/\/+$/, "");
const updateGitHubRepository = process.env.SAG_UPDATE_GITHUB_REPOSITORY?.trim();
const shouldNotarize = process.env.SAG_NOTARIZE === "true";

if (updateBaseUrl && updateGitHubRepository) {
  throw new Error(
    "Configure only one update provider: SAG_UPDATE_BASE_URL or SAG_UPDATE_GITHUB_REPOSITORY.",
  );
}

function githubPublishConfig(repository) {
  if (!repository) return null;
  const parts = repository.split("/");
  if (parts.length !== 2 || parts.some((part) => !part)) {
    throw new Error("SAG_UPDATE_GITHUB_REPOSITORY must use the owner/repository format.");
  }
  return {
    provider: "github",
    owner: parts[0],
    repo: parts[1],
    channel: "latest",
    releaseType: "release",
  };
}

const publish = githubPublishConfig(updateGitHubRepository)
  || (updateBaseUrl ? { provider: "generic", url: updateBaseUrl } : null);

/** @type {import('electron-builder').Configuration} */
module.exports = {
  appId: process.env.SAG_DESKTOP_APP_ID || "ai.zleap.sag",
  productName: "SAG",
  copyright: "Copyright © Zleap AI",
  asar: true,
  compression: "normal",
  npmRebuild: false,
  electronUpdaterCompatibility: ">=2.16",
  directories: {
    output: "release",
    buildResources: "assets"
  },
  files: [
    "dist/**/*",
    "assets/splash.html",
    "assets/icon-master.png",
    "package.json"
  ],
  extraResources: [
    {
      from: "build/resources/web",
      to: "web"
    },
    {
      from: "build/resources/backend",
      to: "backend"
    },
    {
      from: "build/resources/runtime-manifest.json",
      to: "runtime-manifest.json"
    }
  ],
  mac: {
    icon: "assets/icon.icns",
    category: "public.app-category.productivity",
    target: [
      {
        target: "dmg",
        arch: ["arm64"]
      },
      {
        target: "zip",
        arch: ["arm64"]
      }
    ],
    artifactName: "SAG-${version}-mac-${arch}.${ext}",
    hardenedRuntime: true,
    gatekeeperAssess: false,
    notarize: shouldNotarize
  },
  dmg: {
    sign: false
  },
  win: {
    icon: "assets/icon.ico",
    target: [
      {
        target: "nsis",
        arch: ["x64"]
      }
    ],
    artifactName: "SAG-Setup-${version}-win-${arch}.${ext}"
  },
  nsis: {
    oneClick: true,
    perMachine: false,
    allowElevation: true,
    createDesktopShortcut: true,
    createStartMenuShortcut: true,
    shortcutName: "SAG",
    differentialPackage: true,
    deleteAppDataOnUninstall: false
  },
  ...(publish ? { publish } : {})
};
