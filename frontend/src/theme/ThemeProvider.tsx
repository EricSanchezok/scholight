import { createContext, useContext, useEffect, type ReactNode } from "react";

export const themes = ["light"] as const;
export type AppTheme = (typeof themes)[number];

interface ThemeContextValue {
  theme: AppTheme;
}

const defaultTheme: AppTheme = "light";
const ThemeContext = createContext<ThemeContextValue>({ theme: defaultTheme });

export function ThemeProvider({
  children,
  theme = defaultTheme,
}: {
  children: ReactNode;
  theme?: AppTheme;
}) {
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme;
  }, [theme]);

  return <ThemeContext.Provider value={{ theme }}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeContextValue {
  return useContext(ThemeContext);
}
