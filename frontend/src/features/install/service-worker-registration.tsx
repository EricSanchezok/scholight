import { useEffect } from "react";

export function ServiceWorkerRegistration() {
  useEffect(() => {
    if (!("serviceWorker" in navigator)) return undefined;
    const register = () => {
      const scriptUrl = new URL("sw.js", document.baseURI);
      const scope = new URL("./", document.baseURI).pathname;
      void navigator.serviceWorker.register(scriptUrl, { scope }).catch(() => undefined);
    };
    if (document.readyState === "complete") register();
    else window.addEventListener("load", register, { once: true });
    return () => window.removeEventListener("load", register);
  }, []);
  return null;
}
