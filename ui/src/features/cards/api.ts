// Subscriber card reads and writes over the typed client (D-UI-13-01..03). Every card query key
// starts with "cards", so one invalidation refreshes the folder lists, the card page and its tabs.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouteContext } from "@tanstack/react-router";
import type {
  CardContactIn,
  ContractVersion,
  SubscriberCard,
} from "@/api/types";

export const cardKeys = {
  all: ["cards"] as const,
  folder: (folderId: string, includeArchived: boolean) =>
    ["cards", "folder", folderId, { includeArchived }] as const,
  index: (filters: object) => ["cards", "index", filters] as const,
  detail: (id: string) => ["cards", "detail", id] as const,
  subscriptions: (id: string, state: string) =>
    ["cards", "subscriptions", id, state] as const,
  notices: (id: string) => ["cards", "notices", id] as const,
};

export function useApi() {
  return useRouteContext({ from: "__root__" }).api;
}

export function useCard(cardId: string) {
  const api = useApi();
  return useQuery({
    queryKey: cardKeys.detail(cardId),
    queryFn: async (): Promise<SubscriberCard> => {
      const { data } = await api.GET("/api/v1/contracts/cards/{card_id}", {
        params: { path: { card_id: cardId } },
      });
      if (data === undefined) throw new Error("GET /cards/{id} had no body");
      return data;
    },
  });
}

/** Published versions of one contract, highest first (a pin is always a concrete version). */
export function usePublishedVersions(contractId: string | null) {
  const api = useApi();
  return useQuery({
    queryKey: ["contracts", "versions", contractId],
    enabled: contractId !== null,
    queryFn: async (): Promise<ContractVersion[]> => {
      const { data } = await api.GET(
        "/api/v1/contracts/contracts/{contract_id}/versions",
        { params: { path: { contract_id: contractId ?? "" } } },
      );
      if (data === undefined) throw new Error("GET /versions had no body");
      return data.items.filter((version) => version.lifecycle === "published");
    },
  });
}

export type CardFields = {
  name: string;
  description: string;
  contacts: CardContactIn[];
  emailEnabled: boolean;
};

export function useCardWrites() {
  const api = useApi();
  const queryClient = useQueryClient();
  const settle = () =>
    queryClient.invalidateQueries({ queryKey: cardKeys.all });

  const create = useMutation({
    mutationFn: async (input: CardFields & { folderId: string }) => {
      const { data } = await api.POST(
        "/api/v1/contracts/folders/{folder_id}/cards",
        {
          params: { path: { folder_id: input.folderId } },
          body: {
            name: input.name,
            description: input.description,
            contacts: input.contacts,
            email_enabled: input.emailEnabled,
          },
        },
      );
      if (data === undefined) throw new Error("POST /cards had no body");
      return data;
    },
    onSuccess: settle,
  });

  const update = useMutation({
    mutationFn: async (input: {
      cardId: string;
      body: {
        name?: string;
        description?: string;
        contacts?: CardContactIn[];
        email_enabled?: boolean;
        webhook_enabled?: boolean;
        webhook_url?: string;
      };
    }) => {
      const { data } = await api.PATCH("/api/v1/contracts/cards/{card_id}", {
        params: { path: { card_id: input.cardId } },
        body: input.body,
      });
      if (data === undefined) throw new Error("PATCH /cards had no body");
      return data;
    },
    onSuccess: settle,
  });

  const archive = useMutation({
    mutationFn: async (cardId: string) => {
      await api.POST("/api/v1/contracts/cards/{card_id}/archive", {
        params: { path: { card_id: cardId } },
      });
    },
    onSuccess: settle,
  });

  const addMember = useMutation({
    mutationFn: async (input: {
      cardId: string;
      memberType: "local_group" | "entra_group" | "service_account";
      memberId: string;
    }) => {
      await api.POST("/api/v1/contracts/cards/{card_id}/members", {
        params: { path: { card_id: input.cardId } },
        body: { member_type: input.memberType, member_id: input.memberId },
      });
    },
    onSuccess: settle,
  });

  const removeMember = useMutation({
    mutationFn: async (input: {
      cardId: string;
      memberType: "local_group" | "entra_group" | "service_account";
      memberId: string;
    }) => {
      await api.DELETE(
        "/api/v1/contracts/cards/{card_id}/members/{member_type}/{member_id}",
        {
          params: {
            path: {
              card_id: input.cardId,
              member_type: input.memberType,
              member_id: input.memberId,
            },
          },
        },
      );
    },
    onSuccess: settle,
  });

  const subscribe = useMutation({
    mutationFn: async (input: {
      cardId: string;
      contractId: string;
      version: string;
    }) => {
      const { data } = await api.POST(
        "/api/v1/contracts/cards/{card_id}/subscriptions",
        {
          params: { path: { card_id: input.cardId } },
          body: {
            contract_id: input.contractId,
            mode: "pinned",
            version: input.version,
          },
        },
      );
      return data;
    },
    onSuccess: settle,
  });

  const move = useMutation({
    mutationFn: async (input: {
      cardId: string;
      subscriptionId: string;
      version: string;
    }) => {
      await api.POST(
        "/api/v1/contracts/cards/{card_id}/subscriptions/{subscription_id}/move",
        {
          params: {
            path: {
              card_id: input.cardId,
              subscription_id: input.subscriptionId,
            },
          },
          body: { version: input.version },
        },
      );
    },
    onSuccess: settle,
  });

  const unsubscribe = useMutation({
    mutationFn: async (input: { cardId: string; subscriptionId: string }) => {
      await api.POST(
        "/api/v1/contracts/cards/{card_id}/subscriptions/{subscription_id}/unsubscribe",
        {
          params: {
            path: {
              card_id: input.cardId,
              subscription_id: input.subscriptionId,
            },
          },
          body: null,
        },
      );
    },
    onSuccess: settle,
  });

  const rotateSecret = useMutation({
    mutationFn: async (cardId: string) => {
      const { data } = await api.POST(
        "/api/v1/contracts/cards/{card_id}/webhook-secret",
        { params: { path: { card_id: cardId } } },
      );
      if (data === undefined)
        throw new Error("POST /webhook-secret had no body");
      return data;
    },
    // The secret itself never enters the query cache; only the card's flags refresh.
    onSuccess: settle,
  });

  return {
    create,
    update,
    archive,
    addMember,
    removeMember,
    subscribe,
    move,
    unsubscribe,
    rotateSecret,
  };
}

export const memberTypeLabels: Record<string, string> = {
  local_group: "Local group",
  entra_group: "Entra group",
  service_account: "Service account",
};

export const endReasonLabels: Record<string, string> = {
  moved: "Moved",
  unsubscribed: "Unsubscribed",
  removed_by_admin: "Removed by owner",
  card_archived: "Card archived",
};
