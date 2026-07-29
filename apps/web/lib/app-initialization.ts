import {
  isWorkspaceSection,
  type WorkspaceSection,
} from "./workspace";

export type AppMode = "normal" | "explore";
export type PetPresence = "explore-only" | "always" | "hidden";
export type ThemePreference = "light" | "dark" | "system";

export const APP_INITIALIZATION_DEFAULTS = Object.freeze({
  appMode: "normal" as AppMode,
  workspaceSection: "answer" as WorkspaceSection,
  petPresence: "always" as PetPresence,
  petCollapsed: true,
});

export const APP_INITIALIZATION_STORAGE_KEYS = Object.freeze({
  appMode: "sag:app-mode",
  workspaceSection: "sag:workspace-section",
  petPresence: "sag:pet-presence",
  legacyWorkspacePanel: "sag:workspace-panel",
  legacyWorkspaceMini: "sag:workspace-mini-mode",
  legacyPetEnabled: "sag:pet",
  petCollapsed: "sag:pet-collapsed",
  quickModelSetupDismissed: "sag:onboarding:model-setup-dismissed:v1",
  themeBeforeExplore: "sag:theme-before-workspace-collapse",
});

export interface InitializationStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

export interface RemovableInitializationStorage extends InitializationStorage {
  removeItem(key: string): void;
}

function safelyRead(storage: InitializationStorage | null | undefined, key: string) {
  try {
    return storage?.getItem(key) ?? null;
  } catch {
    return null;
  }
}

function safelyWrite(
  storage: InitializationStorage | null | undefined,
  key: string,
  value: string,
) {
  try {
    storage?.setItem(key, value);
  } catch {
    /* In-memory state still follows the requested preference. */
  }
}

function safelyRemove(
  storage: RemovableInitializationStorage | null | undefined,
  key: string,
) {
  try {
    storage?.removeItem(key);
  } catch {
    /* The in-memory restore state remains available for this session. */
  }
}

function isThemePreference(value: string | null | undefined): value is ThemePreference {
  return value === "light" || value === "dark" || value === "system";
}

export function resolveThemePreference(
  theme: string | undefined,
  resolvedTheme: string | undefined,
): ThemePreference {
  if (isThemePreference(theme)) return theme;
  if (resolvedTheme === "dark" || resolvedTheme === "light") return resolvedTheme;
  return "system";
}

export function rememberThemeBeforeExplore(
  storage: InitializationStorage | null | undefined,
  theme: ThemePreference,
) {
  const saved = safelyRead(
    storage,
    APP_INITIALIZATION_STORAGE_KEYS.themeBeforeExplore,
  );
  if (isThemePreference(saved)) return saved;
  safelyWrite(storage, APP_INITIALIZATION_STORAGE_KEYS.themeBeforeExplore, theme);
  return theme;
}

export function restoreThemeAfterExplore(
  storage: RemovableInitializationStorage | null | undefined,
) {
  const saved = safelyRead(
    storage,
    APP_INITIALIZATION_STORAGE_KEYS.themeBeforeExplore,
  );
  safelyRemove(storage, APP_INITIALIZATION_STORAGE_KEYS.themeBeforeExplore);
  return isThemePreference(saved) ? saved : null;
}

export function readInitialAppState(
  storage: InitializationStorage | null | undefined,
): { mode: AppMode; section: WorkspaceSection } {
  const savedMode = safelyRead(storage, APP_INITIALIZATION_STORAGE_KEYS.appMode);
  let mode: AppMode;
  if (savedMode === "normal" || savedMode === "explore") {
    mode = savedMode;
  } else {
    const legacyPanel = safelyRead(
      storage,
      APP_INITIALIZATION_STORAGE_KEYS.legacyWorkspacePanel,
    );
    mode = legacyPanel === "mini" ? "explore" : APP_INITIALIZATION_DEFAULTS.appMode;
    safelyWrite(storage, APP_INITIALIZATION_STORAGE_KEYS.appMode, mode);
  }

  const savedSection = safelyRead(
    storage,
    APP_INITIALIZATION_STORAGE_KEYS.workspaceSection,
  );
  if (isWorkspaceSection(savedSection)) return { mode, section: savedSection };

  const legacySection = safelyRead(
    storage,
    APP_INITIALIZATION_STORAGE_KEYS.legacyWorkspaceMini,
  );
  return {
    mode,
    section: legacySection === "search" || legacySection === "answer"
      ? legacySection
      : APP_INITIALIZATION_DEFAULTS.workspaceSection,
  };
}

export function persistAppMode(
  storage: InitializationStorage | null | undefined,
  mode: AppMode,
  section?: WorkspaceSection,
) {
  safelyWrite(storage, APP_INITIALIZATION_STORAGE_KEYS.appMode, mode);
  if (section) {
    safelyWrite(storage, APP_INITIALIZATION_STORAGE_KEYS.workspaceSection, section);
  }
}

export function readInitialPetPresence(
  storage: InitializationStorage | null | undefined,
): PetPresence {
  const saved = safelyRead(storage, APP_INITIALIZATION_STORAGE_KEYS.petPresence);
  if (saved === "always" || saved === "explore-only" || saved === "hidden") return saved;
  const legacyEnabled = safelyRead(
    storage,
    APP_INITIALIZATION_STORAGE_KEYS.legacyPetEnabled,
  );
  const presence = legacyEnabled === "off"
    ? "explore-only"
    : APP_INITIALIZATION_DEFAULTS.petPresence;
  safelyWrite(storage, APP_INITIALIZATION_STORAGE_KEYS.petPresence, presence);
  return presence;
}

export function persistPetPresence(
  storage: InitializationStorage | null | undefined,
  presence: PetPresence,
) {
  safelyWrite(storage, APP_INITIALIZATION_STORAGE_KEYS.petPresence, presence);
}

export function shouldShowPet(mode: AppMode, presence: PetPresence) {
  if (presence === "hidden") return false;
  return mode === "explore" || presence === "always";
}

export function readInitialPetCollapsed(
  storage: InitializationStorage | null | undefined,
) {
  const saved = safelyRead(storage, APP_INITIALIZATION_STORAGE_KEYS.petCollapsed);
  if (saved === "true") return true;
  if (saved === "false") return false;
  return APP_INITIALIZATION_DEFAULTS.petCollapsed;
}

export function persistPetCollapsed(
  storage: InitializationStorage | null | undefined,
  collapsed: boolean,
) {
  safelyWrite(storage, APP_INITIALIZATION_STORAGE_KEYS.petCollapsed, String(collapsed));
}

export function shouldShowQuickModelSetup(
  required: boolean,
  storage: InitializationStorage | null | undefined,
) {
  return required
    && safelyRead(
      storage,
      APP_INITIALIZATION_STORAGE_KEYS.quickModelSetupDismissed,
    ) !== "true";
}

export function dismissQuickModelSetup(
  storage: InitializationStorage | null | undefined,
) {
  safelyWrite(
    storage,
    APP_INITIALIZATION_STORAGE_KEYS.quickModelSetupDismissed,
    "true",
  );
}
