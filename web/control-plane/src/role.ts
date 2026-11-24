import { createContext, useContext } from "react";

// The signed-in admin's control-plane role, from GET /me (threat model M14).
// The API enforces it; the UI only disables the edit controls a viewer can't use.
type RoleCtx = { canWrite: boolean };

export const RoleContext = createContext<RoleCtx>({ canWrite: false });
export const useRole = () => useContext(RoleContext);
