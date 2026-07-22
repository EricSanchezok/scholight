import { createContext, useContext, useEffect, type ReactNode } from "react";

import { en, type Messages } from "./en";

const catalogs = { en } as const satisfies Record<string, Messages>;

export type AppLocale = keyof typeof catalogs;

interface I18nContextValue {
  locale: AppLocale;
  messages: Messages;
}

const defaultLocale: AppLocale = "en";
const I18nContext = createContext<I18nContextValue>({
  locale: defaultLocale,
  messages: catalogs[defaultLocale],
});

export function I18nProvider({
  children,
  locale = defaultLocale,
}: {
  children: ReactNode;
  locale?: AppLocale;
}) {
  useEffect(() => {
    document.documentElement.lang = locale;
  }, [locale]);

  return (
    <I18nContext.Provider value={{ locale, messages: catalogs[locale] }}>
      {children}
    </I18nContext.Provider>
  );
}

export function useI18n(): I18nContextValue {
  return useContext(I18nContext);
}
