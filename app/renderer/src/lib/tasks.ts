// Calls to the Tasks plugin through bobd.
import { call } from "../bob";

export async function tasksTool<T>(tool: string, args: Record<string, unknown>): Promise<T> {
  const data = await call<{ result: T }>("POST", "/v1/tools/call", { plugin: "tasks", tool, arguments: args });
  return data.result;
}
