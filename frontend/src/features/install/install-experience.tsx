import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import { useI18n } from "../../i18n/I18nProvider";
import { InstallInstructionsDialog } from "./install-instructions-dialog";
import {
  detectInstallEnvironment,
  isStandaloneApp,
  type InstallEnvironment,
} from "./install-environment";
import { ServiceWorkerRegistration } from "./service-worker-registration";

type BeforeInstallPromptEvent = Event & {
  prompt: () => Promise<void>;
  userChoice: Promise<{ outcome: "accepted" | "dismissed"; platform: string }>;
};

export type InstallExperienceContextValue = {
  environment: InstallEnvironment;
  installed: boolean;
  instructionsOpen: boolean;
  openInstallExperience: () => Promise<void>;
  setInstructionsOpen: (open: boolean) => void;
  showInstallEntry: boolean;
};

const InstallExperienceContext = createContext<InstallExperienceContextValue | null>(null);

export function InstallExperienceProvider({ children }: { children: ReactNode }) {
  const { messages } = useI18n();
  const [environment] = useState(() => detectInstallEnvironment());
  const [installed, setInstalled] = useState(environment.standalone || isStandaloneApp());
  const [prompt, setPrompt] = useState<BeforeInstallPromptEvent | null>(null);
  const [instructionsOpen, setInstructionsOpen] = useState(false);

  useEffect(() => {
    const handleBeforeInstallPrompt = (event: Event) => {
      event.preventDefault();
      setPrompt(event as BeforeInstallPromptEvent);
    };
    const handleInstalled = () => {
      setInstalled(true);
      setPrompt(null);
      setInstructionsOpen(false);
    };
    window.addEventListener("beforeinstallprompt", handleBeforeInstallPrompt);
    window.addEventListener("appinstalled", handleInstalled);
    return () => {
      window.removeEventListener("beforeinstallprompt", handleBeforeInstallPrompt);
      window.removeEventListener("appinstalled", handleInstalled);
    };
  }, []);

  const openInstallExperience = useCallback(async () => {
    if (installed) return;
    if (prompt) {
      await prompt.prompt();
      const choice = await prompt.userChoice;
      if (choice.outcome === "accepted") setInstalled(true);
      setPrompt(null);
      return;
    }
    setInstructionsOpen(true);
  }, [installed, prompt]);

  const value = useMemo(
    () => ({
      environment,
      installed,
      instructionsOpen,
      openInstallExperience,
      setInstructionsOpen,
      showInstallEntry: environment.mobile && environment.supported && !installed,
    }),
    [environment, installed, instructionsOpen, openInstallExperience],
  );

  return (
    <InstallExperienceContext.Provider value={value}>
      {children}
      <ServiceWorkerRegistration />
      <InstallInstructionsDialog
        kind={environment.kind}
        messages={messages.installExperience}
        open={instructionsOpen}
        onOpenChange={setInstructionsOpen}
      />
    </InstallExperienceContext.Provider>
  );
}

export function useOptionalInstallExperience(): InstallExperienceContextValue | null {
  return useContext(InstallExperienceContext);
}
