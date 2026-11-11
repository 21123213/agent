import request from "@/utils/request";

export type FileMetadata = {
  id: number;
  user_id: number;
  unit_id: number;
  knowledge_base_id: number;
  filename: string;
  original_filename: string;
  file_size: number;
  file_type?: string | null;
  status?: string | null;
  chunks_count?: number;
  total_elements?: number;
  total_chunks?: number;
  embedded_chunks?: number;
  error_message?: string | null;
  upload_time?: string;
};

export type UploadBatchResponse = {
  batch_status: "success" | "partial_success" | "all_failed";
  items: Array<{
    original_filename: string;
    file_id?: number | null;
    status: string;
    message: string;
  }>;
};

export type ProcessingStatus = {
  file_id: number;
  filename?: string;
  status?: string;
  current_stage?: string;
  progress?: number;
  message?: string;
  error_message?: string | null;
  partitioning?: {
    total_elements: number;
    element_types: Record<string, number>;
  };
  chunking?: {
    total_elements: number;
    total_chunks: number;
    avg_chunk_size: number;
  };
  summarization?: {
    total_chunks: number;
    processed_chunks: number;
    progress_percentage: number;
  };
};

export const listFiles = (knowledgeBaseId: number, page = 1, size = 200) =>
  request.get<any, { items: FileMetadata[]; total: number }>(
    "/api/files/list",
    { params: { knowledge_base_id: knowledgeBaseId, page, size } },
  );

export const uploadFiles = (knowledgeBaseId: number, files: File[]) => {
  const form = new FormData();
  files.forEach((f) => form.append("files", f));
  return request.post<any, UploadBatchResponse>(
    `/api/files/upload?knowledge_base_id=${knowledgeBaseId}`,
    form,
    { headers: { "Content-Type": "multipart/form-data" }, timeout: 120000 },
  );
};

export const deleteFile = (fileId: number) =>
  request.delete<any, { success: boolean; message: string }>(`/api/files/${fileId}`);

export const getFileStatus = (fileId: number) =>
  request.get<any, ProcessingStatus>(`/api/files/${fileId}/status`);

export const getFileChunks = (fileId: number) =>
  request.get<any, { total: number; items: Array<Record<string, unknown>> }>(
    `/api/files/${fileId}/chunks`,
  );
