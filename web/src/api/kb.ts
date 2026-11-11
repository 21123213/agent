import request from "@/utils/request";

export type KnowledgeBase = {
  id: number;
  unit_id: number;
  owner_user_id: number;
  name: string;
  description?: string | null;
  is_default: boolean;
  status: string;
  file_count?: number | null;
};

export type KbCapabilities = {
  is_admin: boolean;
  can_create_kb: boolean;
  can_delete_kb: boolean;
  can_upload_files: boolean;
  can_delete_files: boolean;
  role_id?: number;
  unit_id?: number;
};

export const listKnowledgeBases = () =>
  request.get<any, KnowledgeBase[]>("/api/kb");

export const getKbCapabilities = () =>
  request.get<any, KbCapabilities>("/api/kb/capabilities");

export const createKnowledgeBase = (data: { name: string; description?: string }) =>
  request.post<any, KnowledgeBase>("/api/kb", data);

export const deleteKnowledgeBase = (kbId: number) =>
  request.delete<any, { success: boolean; message: string }>(`/api/kb/${kbId}`);
