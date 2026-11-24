import { createContext, useContext, type ReactNode } from "react";

/** Content shown in the right-hand tools (Help) drawer. */
export type HelpContent = { header: string; body: ReactNode };

type HelpCtx = { openHelp: (c: HelpContent) => void };

// Pages call openHelp(content) to populate + open the AppLayout tools drawer.
export const HelpContext = createContext<HelpCtx>({ openHelp: () => {} });
export const useHelp = () => useContext(HelpContext);
