// Version slot and accessible name of a contract row in the folder tree (D-10).
import type { ContractSummary } from "@/api/types";

export function contractVersionSlot(_contract: ContractSummary): {
  text: string;
  muted: boolean;
} {
  return { text: "", muted: false };
}

export function contractRowLabel(_contract: ContractSummary): string {
  return "";
}
